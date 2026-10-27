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
from .credentials import ConnectionMeta
from .models import Environment, Order, OrderStatus, Position
from .service import AccountService, PolicyRefused

CONFIRM_TTL_S = 60.0
NOT_FOUND_AFTER_LOOKUPS, NOT_FOUND_AFTER_MS = 3, 60_000  # exchanges can list a new order seconds late
TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})
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
        self._lock = threading.Lock()

    # ---- Trading Mode ----
    def set_mode(self, connection_id: str, *, user: str, enabled: bool, consent: str | None) -> ConnectionMeta:
        meta, _ = self.accounts.credentials.get(connection_id, user=user)
        if enabled:
            if meta.environment is not Environment.TESTNET:
                raise TradingRefused("Trading Mode is available only on testnet/demo connections (AGENTS.md §2)")
            if not meta.permissions.can_trade:
                raise TradingRefused("this key has no trading permission at the exchange")
            if consent != CONSENT_TEXT:
                raise TradingRefused("Trading Mode needs your explicit consent to the exact consent text")
        updated = meta.model_copy(update={"trading_enabled": enabled})
        self.accounts.credentials.update_meta(updated)
        if not enabled:
            self._drop_previews(connection_id)
        return updated

    def _drop_previews(self, connection_id: str) -> None:
        with self._lock:
            for pid in [p for p, v in self._previews.items() if v.connection_id == connection_id]:
                del self._previews[pid]

    def _tradable(self, connection_id: str, user: str):
        meta, cred, adapter = self.accounts.open_connection(connection_id, user)  # expiry checked on every use
        if meta.environment is not Environment.TESTNET:
            raise TradingRefused("orders are sent only to testnet/demo connections")
        if not meta.trading_enabled:
            raise TradingRefused("Trading Mode is off for this connection")
        return meta, cred, adapter

    # ---- preview ----
    def preview(self, connection_id: str, draft: OrderDraft, *, user: str) -> tuple[Preview, str]:
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
        if not all(c.ok for c in checks):
            failed = "; ".join(c.detail for c in checks if not c.ok)
            raise TradingRefused(f"the order breaks exchange rules: {failed}")

        preview_id = secrets.token_hex(8)
        request = OrderRequest(symbol=draft.symbol, market=draft.market, side=draft.side, type=draft.type,
                               quantity=draft.quantity, price=draft.price,
                               time_in_force=draft.time_in_force or ("GTC" if draft.type == "limit" else None),
                               reduce_only=draft.reduce_only, client_order_id=f"mat{preview_id}")
        token = secrets.token_urlsafe(24)
        preview = Preview(preview_id=preview_id, connection_id=connection_id, user=user, exchange=meta.exchange,
                          request=request, checks=checks, estimates=estimates, warnings=warnings,
                          expires_at=self._clock() + CONFIRM_TTL_S,
                          token_digest=hashlib.sha256(token.encode()).digest())
        with self._lock:
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
        with self._lock:  # single use: the preview leaves the table before anything is sent
            preview = self._previews.pop(preview_id, None)
        if preview is None or preview.user != user:
            raise TradingRefused("unknown or already used preview; create a new preview")
        if preview.expires_at <= self._clock():
            raise TradingRefused("the confirmation expired; create a new preview")
        if not hmac.compare_digest(hashlib.sha256(confirm_token.encode()).digest(), preview.token_digest):
            raise TradingRefused("the confirmation token does not match this preview; create a new preview")
        meta, cred, adapter = self._tradable(preview.connection_id, user)  # mode and expiry re-checked now
        req = preview.request
        self.journal.journal_insert({"client_order_id": req.client_order_id, "user_id": user,
                                     "connection_id": meta.connection_id, "exchange": meta.exchange,
                                     "environment": str(meta.environment), "state": "SENDING",
                                     "request_json": json.dumps(asdict(req), default=str)})
        try:
            order = adapter.place_order(cred, req)
        except (OutcomeUnknown, ExchangeUnavailable) as exc:  # not a definite rejection: look it up, never resend
            self.journal.journal_update(req.client_order_id, state="UNKNOWN", message=str(exc))
            self.accounts.clear_cache(meta.connection_id)
            return self._reconcile(adapter, cred, req.client_order_id, req.symbol, req.market,
                                   note="the send outcome was unknown, so the order was looked up instead of resent")
        except ExchangeError as exc:
            self.journal.journal_update(req.client_order_id, state="REJECTED", message=str(exc))
            raise TradingRefused(f"the exchange rejected the order: {exc}") from None
        self.journal.journal_update(req.client_order_id, state=order.status.value, order_json=order.model_dump_json())
        self.accounts.clear_cache(meta.connection_id)
        return {"client_order_id": req.client_order_id, "state": order.status.value, "order": order.model_dump(mode="json"),
                "note": "Accepted by the exchange. Check its status: acceptance does not mean it filled."}

    # ---- reconcile / cancel ----
    def reconcile(self, client_order_id: str, *, user: str) -> dict[str, Any]:
        row = self._row(client_order_id, user)
        req = json.loads(row["request_json"])
        meta, cred, adapter = self.accounts.open_connection(row["connection_id"], user)
        return self._reconcile(adapter, cred, client_order_id, req["symbol"], req["market"])

    def _reconcile(self, adapter, cred, client_order_id: str, symbol: str, market: str, note: str = "") -> dict[str, Any]:
        try:
            order = adapter.query_order(cred, symbol, market, client_order_id=client_order_id)
        except ExchangeError as exc:
            return {"client_order_id": client_order_id, "state": "UNKNOWN",
                    "note": f"{note + '; ' if note else ''}lookup failed ({exc}); reconcile again later. Do not resend."}
        row = (self.journal.journal_row(client_order_id) or {})
        prefix = note + "; " if note else ""
        if order is None:
            if row.get("state") in TERMINAL:  # old orders get purged by exchanges: keep the last known final state
                return {"client_order_id": client_order_id, "state": row["state"],
                        "note": prefix + "the exchange no longer lists this order; its last known state is kept"}
            lookups = self.journal.journal_lookup(client_order_id)
            age_ms = self._wall_ms() - int(row.get("created_at") or 0)
            if lookups >= NOT_FOUND_AFTER_LOOKUPS and age_ms >= NOT_FOUND_AFTER_MS:
                self.journal.journal_update(client_order_id, state="NOT_FOUND",
                                            message=f"not found in {lookups} lookups over {age_ms // 1000} s")
                return {"client_order_id": client_order_id, "state": "NOT_FOUND",
                        "note": prefix + "the exchange has no such order after repeated lookups; it most likely never "
                                "arrived. You may place a new order."}
            self.journal.journal_update(client_order_id, state="UNKNOWN",
                                        message=f"not listed yet (lookup {lookups}); exchanges can show new orders late")
            return {"client_order_id": client_order_id, "state": "UNKNOWN",
                    "note": prefix + "the exchange does not list this order yet. Check again in a few seconds; "
                            "do not resend."}
        if row.get("state") in TERMINAL and order.status.value not in TERMINAL:
            return {"client_order_id": client_order_id, "state": row["state"], "note": prefix + "final state kept"}
        self.journal.journal_update(client_order_id, state=order.status.value, order_json=order.model_dump_json())
        return {"client_order_id": client_order_id, "state": order.status.value, "order": order.model_dump(mode="json"),
                "note": note or "status from the exchange"}

    def cancel(self, client_order_id: str, *, user: str) -> dict[str, Any]:
        row = self._row(client_order_id, user)
        req = json.loads(row["request_json"])
        meta, cred, adapter = self.accounts.open_connection(row["connection_id"], user)  # expiry still enforced
        if meta.environment is not Environment.TESTNET:
            raise TradingRefused("orders exist only on testnet/demo connections")
        # Cancelling only lowers risk, so it stays available after Trading Mode is turned off.
        self.accounts.clear_cache(meta.connection_id)
        try:
            order = adapter.cancel_order(cred, req["symbol"], req["market"], client_order_id=client_order_id)
        except OutcomeUnknown as exc:
            return self._reconcile(adapter, cred, client_order_id, req["symbol"], req["market"],
                                   note=f"cancel outcome unknown ({exc}); looked the order up")
        except ExchangeError as exc:
            raise TradingRefused(f"the exchange refused the cancel: {exc}") from None
        self.journal.journal_update(client_order_id, state=order.status.value, order_json=order.model_dump_json())
        return {"client_order_id": client_order_id, "state": order.status.value, "order": order.model_dump(mode="json")}

    def _row(self, client_order_id: str, user: str) -> dict[str, Any]:
        rows = self.journal.journal(user, client_order_id=client_order_id)
        if not rows:
            raise TradingRefused("no order with this client order ID in the journal")
        return rows[0]

    def needs_reconcile(self, *, user: str) -> list[dict[str, Any]]:
        """Orders whose outcome was never confirmed (e.g. the app stopped while sending)."""
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
