"""Testnet-only Trading Mode: preview -> explicit confirmation -> journal -> send -> reconcile (AGENTS.md §2).

Rules enforced here (adapters enforce testnet-only a second time):
- Trading Mode is off by default, per connection, needs explicit consent, and works only on testnet
  connections whose key can trade.
- Nothing is sent without a preview the user confirmed. The confirmation token is single-use, bound to the
  exact previewed order, and expires after 60 s; the server sends the stored order, not what the client resends.
- Invalid prices/quantities are refused with the nearest valid values; nothing is silently rounded.
- The order is written to the journal BEFORE it is sent. An unknown outcome (timeout, HTTP 5xx) is reconciled by
  client order ID and never resent. An acknowledgement is not a fill: status comes from the exchange.
- There is no code path from the Copilot to submit(): AI can only produce a draft for preview.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .adapters.base import ExchangeError, ExchangeUnavailable, OrderRequest, OutcomeUnknown, SymbolRules, plain
from .credentials import ConnectionMeta, CredentialExpired, CredentialNotFound
from .models import Environment, Order, OrderStatus, Position
from .service import AccountService, PolicyRefused

CONFIRM_TTL_S = 60.0
MISS_SPACING_MS = 20_000  # a "not listed" lookup counts as a new miss only this long after the previous one
MAX_PENDING_PREVIEWS = 50
CONSENT_TEXT = ("I understand Trading Mode sends real orders to this connection's TESTNET/demo account after I confirm "
                "each one, that the app never trades on its own, and that testnet results do not predict live results.")


class OrderDraft(BaseModel):
    """What a user (or an AI-written draft) asks for. Never executable by itself."""
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.]{0,29}$")  # dots for share classes such as BRK.B
    market: Literal["spot", "usdm_perpetual", "stock"]
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    quantity: Decimal = Field(gt=0, max_digits=30, decimal_places=18)
    price: Decimal | None = Field(default=None, gt=0, max_digits=30, decimal_places=18)
    time_in_force: Literal["GTC", "IOC", "FOK"] | None = None
    reduce_only: bool = False

    @field_validator("price")
    @classmethod
    def _finite(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not v.is_finite():
            raise ValueError("price must be a finite number")
        return v


class TradingRefused(Exception):
    """Order or mode request refused before anything was sent. Safe to show."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Preview:
    preview_id: str
    connection_id: str
    user: str
    exchange: str
    request: OrderRequest
    checks: list[Check]
    estimates: dict[str, Any]
    warnings: list[str]
    expires_at: float
    token_digest: bytes = field(repr=False, default=b"")
    mode_gen: int = 0  # Trading Mode generation at creation; any mode change invalidates the preview

    def public(self) -> dict[str, Any]:
        req = asdict(self.request)
        return {"preview_id": self.preview_id, "connection_id": self.connection_id, "exchange": self.exchange,
                "environment": "testnet",
                "order": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in req.items()},
                "checks": [asdict(c) for c in self.checks], "estimates": self.estimates, "warnings": self.warnings,
                "expires_in_s": round(self.expires_at - time.monotonic(), 1),
                "confirm": "Send the confirm_token with POST /api/v1/trading/orders within 60 s to place this exact order."}


def _on_step(value: Decimal, step: Decimal | None) -> bool:
    return not step or (value / step) == (value / step).to_integral_value()


def _nearest(value: Decimal, step: Decimal) -> tuple[Decimal, Decimal]:
    down = (value / step).to_integral_value(ROUND_DOWN) * step
    up = (value / step).to_integral_value(ROUND_UP) * step
    return Decimal(plain(down)), Decimal(plain(up))


def validate_against_rules(draft: OrderDraft, rules: SymbolRules, price: Decimal) -> list[Check]:
    """Exchange-rule checks for a preview. `price` is the limit price or the reference price for market orders."""
    checks = [Check("symbol_trading", rules.status.upper() == "TRADING", f"{rules.symbol} status is {rules.status}")]
    if rules.order_types:
        checks.append(Check("order_type", draft.type in rules.order_types,
                            f"{draft.type} orders {'are' if draft.type in rules.order_types else 'are not'} allowed"))
    if draft.type == "limit" and rules.price_tick:
        ok = _on_step(price, rules.price_tick)
        lo, hi = _nearest(price, rules.price_tick)
        checks.append(Check("price_tick", ok, f"price must be a multiple of {plain(rules.price_tick)}"
                            + ("" if ok else f"; nearest valid: {lo} or {hi}")))
    if rules.quantity_step:
        ok = _on_step(draft.quantity, rules.quantity_step)
        lo, hi = _nearest(draft.quantity, rules.quantity_step)
        checks.append(Check("quantity_step", ok, f"quantity must be a multiple of {plain(rules.quantity_step)}"
                            + ("" if ok else f"; nearest valid: {lo} or {hi}")))
    if rules.min_quantity is not None:
        checks.append(Check("min_quantity", draft.quantity >= rules.min_quantity,
                            f"minimum quantity {plain(rules.min_quantity)}"))
    if rules.max_quantity:
        checks.append(Check("max_quantity", draft.quantity <= rules.max_quantity,
                            f"maximum quantity {plain(rules.max_quantity)}"))
    if rules.min_notional is not None:
        notional = draft.quantity * price
        checks.append(Check("min_notional", notional >= rules.min_notional,
                            f"order value {plain(notional)} {rules.quote_asset}; minimum {plain(rules.min_notional)}"))
    return checks


class TradingService:
    def __init__(self, accounts: AccountService, journal: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.accounts, self.journal, self._clock, self._wall_ms = accounts, journal, clock, wall_ms
        self._previews: dict[str, Preview] = {}
        self._mode_gen: dict[str, int] = {}  # connection id -> bumped on every Trading Mode change
        self._sending: set[str] = set()  # client order IDs whose send has not returned yet
        self._lock = threading.Lock()

    # ---- Trading Mode ----
    def set_mode(self, connection_id: str, *, user: str, enabled: bool, consent: str | None) -> ConnectionMeta:
        def change(meta: ConnectionMeta) -> ConnectionMeta:
            if enabled:
                if meta.environment is not Environment.TESTNET:
                    raise TradingRefused("Trading Mode is available only on testnet/demo connections (AGENTS.md §2)")
                if not meta.permissions.can_trade:
                    raise TradingRefused("this key has no trading permission at the exchange")
                if consent != CONSENT_TEXT:
                    raise TradingRefused("Trading Mode needs your explicit consent to the exact consent text")
            return meta.model_copy(update={"trading_enabled": enabled})
        meta, _ = self.accounts.credentials.get(connection_id, user=user)  # expiry enforced before any change
        change(meta)  # a refused change leaves pending previews alone; update_meta re-checks it atomically
        with self._lock:  # bump first: a preview being built right now can no longer be stored or submitted
            self._mode_gen[connection_id] = self._mode_gen.get(connection_id, 0) + 1
            for pid in [p for p, v in self._previews.items() if v.connection_id == connection_id]:
                del self._previews[pid]
        return self.accounts.credentials.update_meta(connection_id, user=user, mutate=change)

    def _tradable(self, connection_id: str, user: str):
        meta, cred, adapter = self.accounts.open_connection(connection_id, user)  # expiry checked on every use
        if meta.environment is not Environment.TESTNET:
            raise TradingRefused("orders are sent only to testnet/demo connections")
        if not meta.trading_enabled:
            raise TradingRefused("Trading Mode is off for this connection")
        return meta, cred, adapter

    # ---- preview ----
    def preview(self, connection_id: str, draft: OrderDraft, *, user: str) -> tuple[Preview, str]:
        with self._lock:
            gen = self._mode_gen.get(connection_id, 0)
        meta, cred, adapter = self._tradable(connection_id, user)
        if draft.type == "limit" and draft.price is None:
            raise TradingRefused("a limit order needs a price")
        if draft.type == "market" and draft.price is not None:
            raise TradingRefused("a market order takes no price")
        if draft.reduce_only and draft.market != "usdm_perpetual":
            raise TradingRefused("reduce-only applies to futures orders")
        rules = adapter.symbol_rules(draft.symbol, draft.market)
        reference = adapter.reference_price(draft.symbol, draft.market)
        price = draft.price if draft.type == "limit" else reference
        checks = validate_against_rules(draft, rules, price)
        if not all(c.ok for c in checks):  # before fees, balances and leverage: no signed calls for a refusal (SIMP-2)
            failed = "; ".join(c.detail for c in checks if not c.ok)
            raise TradingRefused(f"the order breaks exchange rules: {failed}")
        notional = draft.quantity * price
        estimates: dict[str, Any] = {
            "reference_price": {"value": str(reference), "provenance": "exchange_reported", "note": "last trade price"},
            "notional": {"value": plain(notional), "asset": rules.quote_asset, "provenance": "derived"},
        }
        warnings: list[str] = []
        fees = adapter.fee_rates(cred, draft.symbol, draft.market)
        if fees:
            rate = fees[1] if draft.type == "market" else fees[0]
            estimates["fee"] = {"value": plain((notional * rate)), "asset": rules.quote_asset,
                                "rate": str(rate), "provenance": "estimate",
                                "note": "taker rate for market orders, maker rate for limit orders that rest"}
        else:
            warnings.append("the exchange did not report fee rates for this key; fees are unknown")
        snap = self.accounts.snapshot(connection_id, user=user)
        if draft.market == "usdm_perpetual":
            lev = adapter.leverage(cred, draft.symbol, draft.market)
            if lev:
                margin = notional / lev
                estimates["initial_margin"] = {"value": plain(margin.quantize(Decimal("0.00000001"))),
                                               "asset": rules.quote_asset, "leverage": str(lev), "provenance": "estimate"}
                margin_wallets = ("futures_usdm", "unified")  # unified: Bybit UTA, OKX trading account
                avail = next((t.available_balance for t in snap.totals if t.wallet in margin_wallets), None)
                if avail is not None and margin > avail:
                    warnings.append(f"estimated initial margin {margin:.2f} exceeds available balance {avail}")
            estimates["liquidation_price"] = {"value": None, "provenance": "estimate",
                                              "note": "not estimated here; the exchange calculates it after the fill"}
            existing = [p for p in snap.positions if p.symbol == draft.symbol]
            estimates["position_after"] = _position_after(existing, draft)
        elif draft.side == "buy":
            cash_wallets = ("spot", "securities", "unified")
            free = next((b.free for b in snap.balances if b.wallet in cash_wallets and b.asset == rules.quote_asset), None)
            if free is not None and notional > free:
                warnings.append(f"order value {plain(notional)} exceeds free {rules.quote_asset} balance {free}")
        else:
            held = next((p.quantity for p in snap.positions if p.symbol == draft.symbol and p.market == "stock"), None) \
                if draft.market == "stock" else \
                next((b.free for b in snap.balances if b.wallet in ("spot", "unified") and b.asset == rules.base_asset), None)
            if held is not None and draft.quantity > held:
                warnings.append(f"sell quantity exceeds the {held} {rules.base_asset} you hold")
        if draft.type == "market":
            warnings.append("market orders fill at the prices available when they arrive, which can differ from the reference price")

        preview_id = secrets.token_hex(8)
        request = OrderRequest(symbol=draft.symbol, market=draft.market, side=draft.side, type=draft.type,
                               quantity=draft.quantity, price=draft.price,
                               time_in_force=draft.time_in_force or ("GTC" if draft.type == "limit" else None),
                               reduce_only=draft.reduce_only, client_order_id=f"mat{preview_id}")
        token = secrets.token_urlsafe(24)
        preview = Preview(preview_id=preview_id, connection_id=connection_id, user=user, exchange=meta.exchange,
                          request=request, checks=checks, estimates=estimates, warnings=warnings,
                          expires_at=self._clock() + CONFIRM_TTL_S,
                          token_digest=hashlib.sha256(token.encode()).digest(), mode_gen=gen)
        with self._lock:
            if self._mode_gen.get(connection_id, 0) != gen:
                raise TradingRefused("Trading Mode changed while the preview was being built; preview again")
            self._expire_locked()
            if len(self._previews) >= MAX_PENDING_PREVIEWS:
                raise TradingRefused("too many unconfirmed previews; confirm or wait for them to expire")
            self._previews[preview_id] = preview
        return preview, token

    def _expire_locked(self) -> None:
        now = self._clock()
        for pid in [p for p, v in self._previews.items() if v.expires_at <= now]:
            del self._previews[pid]

    # ---- submit ----
    def submit(self, preview_id: str, confirm_token: str, *, user: str) -> dict[str, Any]:
        digest = hashlib.sha256(confirm_token.encode()).digest()
        with self._lock:  # single use: the preview leaves the table before anything is sent
            preview = self._previews.get(preview_id)
            if preview is None or preview.user != user:  # someone else's preview is not touched
                raise TradingRefused("unknown or already used preview; create a new preview")
            if not hmac.compare_digest(digest, preview.token_digest):  # a wrong token does not burn the preview
                raise TradingRefused("the confirmation token does not match this preview; create a new preview")
            del self._previews[preview_id]
            if self._mode_gen.get(preview.connection_id, 0) != preview.mode_gen:
                raise TradingRefused("Trading Mode changed after this preview; create a new preview")
        if preview.expires_at <= self._clock():
            raise TradingRefused("the confirmation expired; create a new preview")
        meta, cred, adapter = self._tradable(preview.connection_id, user)  # mode and expiry re-checked now
        req = preview.request
        with self._lock:  # marked before the SENDING row exists, so resolve() can never see it unmarked
            self._sending.add(req.client_order_id)
        try:
            return self._journal_and_send(preview, meta, cred, adapter, user)
        finally:
            with self._lock:
                self._sending.discard(req.client_order_id)

    def _journal_and_send(self, preview: Preview, meta, cred, adapter, user: str) -> dict[str, Any]:
        req = preview.request
        self.journal.journal_insert({"client_order_id": req.client_order_id, "user_id": user,
                                     "connection_id": meta.connection_id, "exchange": meta.exchange,
                                     "environment": str(meta.environment), "state": "SENDING",
                                     "key_hint": meta.key_hint, "key_fingerprint": meta.key_fingerprint, "request_json": json.dumps(asdict(req), default=str)})
        with self._lock:  # last check right before sending: Trading Mode not switched since the preview
            switched = self._mode_gen.get(preview.connection_id, 0) != preview.mode_gen
        if switched:
            self.journal.journal_update(req.client_order_id, state="REJECTED",
                                        message="not sent: Trading Mode changed before sending")
            raise TradingRefused("Trading Mode changed before sending; nothing was sent")
        return self._send(meta, cred, adapter, req)

    def _send(self, meta, cred, adapter, req: OrderRequest) -> dict[str, Any]:
        started = self._wall_ms()
        try:
            order = adapter.place_order(cred, req)
        except (OutcomeUnknown, ExchangeUnavailable) as exc:  # not a definite rejection: look it up, never resend
            # Only from SENDING: a concurrent status check may already have found the order.
            self.journal.journal_update(req.client_order_id, state="UNKNOWN", message=str(exc), only_from="SENDING")
            self.accounts.clear_cache(meta.connection_id)
            return self._reconcile(adapter, cred, req.client_order_id, req.symbol, req.market,
                                   note="the send outcome was unknown, so the order was looked up instead of resent")
        except ExchangeError as exc:
            self.journal.journal_update(req.client_order_id, state="REJECTED", message=str(exc), only_from="SENDING")
            raise TradingRefused(f"the exchange rejected the order: {exc}") from None
        stored = self._seen(req.client_order_id, order, started)
        self.accounts.clear_cache(meta.connection_id)
        return {"client_order_id": req.client_order_id, "state": stored, "order": order.model_dump(mode="json"),
                "note": "Accepted by the exchange. Check its status: acceptance does not mean it filled."}

    def _seen(self, client_order_id: str, order: Order, started_ms: int) -> str | None:
        """The exchange answered with this order (acknowledgement or lookup): it exists at the exchange."""
        return self.journal.journal_seen(client_order_id, state=order.status.value, order_json=order.model_dump_json(),
                                         exchange_order_id=order.order_id or None, now_ms=max(self._wall_ms(), started_ms))

    # ---- reconcile / cancel / resolve ----
    def reconcile(self, client_order_id: str, *, user: str) -> dict[str, Any]:
        row = self._row(client_order_id, user)
        req = json.loads(row["request_json"])
        _, cred, adapter, note = self._open_for_row(row, user)
        return self._reconcile(adapter, cred, client_order_id, req["symbol"], req["market"], note=note)

    def _reconcile(self, adapter, cred, client_order_id: str, symbol: str, market: str, note: str = "") -> dict[str, Any]:
        prefix = note + "; " if note else ""
        started = self._wall_ms()
        try:
            order = adapter.query_order(cred, symbol, market, client_order_id=client_order_id)
        except ExchangeError as exc:
            self.journal.journal_miss(client_order_id, now_ms=self._wall_ms(), started_ms=started,
                                      min_spacing_ms=MISS_SPACING_MS, verification="lookup_failed")
            row = self.journal.journal_row(client_order_id) or {}
            return {"client_order_id": client_order_id, "state": row.get("state", "UNKNOWN"),
                    "verification": row.get("verification") or "lookup_failed",
                    "note": f"{prefix}lookup failed ({exc}); check again later. Do not resend."}
        if order is not None:
            stored = self._seen(client_order_id, order, started)
            return {"client_order_id": client_order_id, "state": stored, "verification": "confirmed",
                    "order": order.model_dump(mode="json"),
                    "note": prefix + ("status from the exchange" if stored == order.status.value else "final state kept")}
        # Not listed. That is never proof the order was not executed: the state is left as it was.
        misses = self.journal.journal_miss(client_order_id, now_ms=self._wall_ms(), started_ms=started,
                                           min_spacing_ms=MISS_SPACING_MS)
        row = self.journal.journal_row(client_order_id) or {}
        state = row.get("state", "UNKNOWN")
        if row.get("acknowledged"):
            msg = (f"the exchange confirmed this order before (last confirmed state {state}) but does not list it now. "
                   "Exchanges delay and purge order listings, so this does not mean it was never executed. "
                   "Do not place it again; check your order history at the exchange.")
        else:
            msg = (f"the exchange does not list this order (miss {misses}). That does not prove it was never "
                   "executed: listings can lag, and old orders get purged. Do not place it again until you have "
                   "checked the exchange's order history; then you can mark it resolved.")
        # A miss that arrived after a newer confirmation was ignored: report what is stored.
        return {"client_order_id": client_order_id, "state": state, "verification": row.get("verification") or "not_listed",
                "misses": misses,
                "note": prefix + msg}

    def cancel(self, client_order_id: str, *, user: str) -> dict[str, Any]:
        row = self._row(client_order_id, user)
        req = json.loads(row["request_json"])
        meta, cred, adapter, note = self._open_for_row(row, user)  # expiry still enforced
        if meta.environment is not Environment.TESTNET:
            raise TradingRefused("orders exist only on testnet/demo connections")
        # Cancelling only lowers risk, so it stays available after Trading Mode is turned off, and for orders whose
        # state is uncertain (UNKNOWN / not listed).
        self.accounts.clear_cache(meta.connection_id)
        started = self._wall_ms()
        try:
            order = adapter.cancel_order(cred, req["symbol"], req["market"], client_order_id=client_order_id,
                                         exchange_order_id=row.get("exchange_order_id"))
        except OutcomeUnknown as exc:
            return self._reconcile(adapter, cred, client_order_id, req["symbol"], req["market"],
                                   note=f"cancel outcome unknown ({exc}); looked the order up")
        except ExchangeError as exc:
            raise TradingRefused(f"the exchange refused the cancel: {exc}") from None
        stored = self._seen(client_order_id, order, started)
        return {"client_order_id": client_order_id, "state": stored, "order": order.model_dump(mode="json"),
                **({"note": note} if note else {})}

    def resolve(self, client_order_id: str, *, user: str) -> dict[str, Any]:
        """The user checked the exchange's order history and asks to stop tracking an unsettled, never-acknowledged
        order. Acknowledged orders are never resolved this way: their state comes from the exchange."""
        row = self._row(client_order_id, user)
        if row.get("acknowledged"):
            raise TradingRefused("the exchange acknowledged this order; its state comes from the exchange, not from here")
        with self._lock:
            if client_order_id in self._sending:
                raise TradingRefused("this order is being sent right now; wait for the exchange's answer")
        stored = self.journal.journal_resolve(client_order_id, note="marked resolved by the user after checking the exchange")
        if stored != "USER_RESOLVED":  # also when the exchange acknowledged it after the check above
            raise TradingRefused(f"this order is {stored}; only never-acknowledged SENDING, UNKNOWN or NOT_FOUND "
                                 "orders can be resolved")
        return {"client_order_id": client_order_id, "state": stored,
                "note": "Marked resolved by you. If the exchange later shows this order, check it there."}

    def _open_for_row(self, row: dict[str, Any], user: str):
        """The connection that sent the order, or (if it expired, was disconnected or ended with the last session)
        a current connection with the same exchange, environment and key, so an unsettled order is never orphaned.
        Another key may belong to another account, where the order would look missing."""
        try:
            return (*self.accounts.open_connection(row["connection_id"], user), "")
        except (CredentialExpired, CredentialNotFound):
            for meta in self.accounts.connections(user=user):
                if (meta.exchange == row["exchange"] and str(meta.environment) == row["environment"]
                        and row.get("key_fingerprint") and meta.key_fingerprint == row["key_fingerprint"]):
                    return (*self.accounts.open_connection(meta.connection_id, user),
                            f"checked through your current {meta.exchange} {meta.environment} connection")
            key = f" (the key ending {row['key_hint']})" if row.get("key_hint") else ""
            raise TradingRefused(f"the connection that sent this order is gone. Connect the same {row['exchange']} "
                                 f"{row['environment']} key{key} to check or cancel it; the order record is kept.") from None

    def _row(self, client_order_id: str, user: str) -> dict[str, Any]:
        rows = self.journal.journal(user, client_order_id=client_order_id)
        if not rows:
            raise TradingRefused("no order with this client order ID in the journal")
        return rows[0]

    def needs_reconcile(self, *, user: str) -> list[dict[str, Any]]:
        """Orders not settled yet: outcome never confirmed (e.g. the app stopped while sending), or acknowledged
        but not final."""
        return self.journal.journal_open(user)


def _position_after(existing: list[Position], draft: OrderDraft) -> dict[str, Any]:
    signed = sum((p.quantity if p.side == "long" else -p.quantity) for p in existing) if existing else Decimal(0)
    delta = draft.quantity if draft.side == "buy" else -draft.quantity
    if not draft.reduce_only:
        after = signed + delta
    elif signed == 0 or (signed > 0) == (delta > 0):
        after = signed  # reduce-only cannot open or grow a position; the exchange would reject it
    else:
        after = signed + delta if abs(delta) < abs(signed) else Decimal(0)  # and cannot flip past zero
    side = "flat" if after == 0 else ("long" if after > 0 else "short")
    return {"quantity": plain(abs(after)), "side": side, "provenance": "derived",
            "note": "if the whole order fills; one-way position mode assumed"}
