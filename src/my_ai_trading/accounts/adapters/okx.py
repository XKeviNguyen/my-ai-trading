"""OKX V5: spot + USDT/USDC-margined linear perpetual swaps in the unified trading account. Endpoints and fields
follow the official V5 docs (https://www.okx.com/docs-v5/en/, checked 2026-10-27) and the official python-okx SDK
(okx/utils.py signing, okx/consts.py paths):

- Host: https://openapi.okx.com for both environments (#overview-production-trading-services,
  #overview-demo-trading-services). Demo trading = the same host plus the header `x-simulated-trading: 1`, with
  demo-only keys (Trade -> Demo Trading -> Personal Center -> Demo Trading API). The header is sent only on
  TESTNET; live requests never carry it. The SDK still defaults to www.okx.com; the current docs name openapi.okx.com.
  US/AU (us.okx.com) and EEA (eea.okx.com) accounts need other domains and are not offered here.
- Signing (#overview-rest-authentication): Base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body)).
  The GET query is part of requestPath; body is the exact JSON string sent (empty for GET). Timestamp is ISO 8601
  UTC with milliseconds (2020-12-08T09:08:57.715Z), kept within 30 s of GET /api/v5/public/time
  (#public-data-rest-api-get-system-time). Headers OK-ACCESS-KEY / -SIGN / -TIMESTAMP / -PASSPHRASE.
- Key permissions: GET /api/v5/account/config `perm` = comma list of read_only / trade / withdraw
  (#trading-account-rest-api-get-account-configuration). OKX documents that Trade covers "place and cancel orders,
  funding transfer", so `trade` also means transfer rights; there is no separate transfer flag. An unknown token
  leaves transfer unconfirmed (None), which the live policy refuses.
- Account: GET /api/v5/account/balance (#trading-account-rest-api-get-balance), GET /api/v5/account/positions
  (#trading-account-rest-api-get-positions), GET /api/v5/trade/orders-pending (#order-book-trading-trade-get-order-list),
  GET /api/v5/trade/orders-history (#order-book-trading-trade-get-order-history-last-7-days), GET /api/v5/trade/fills
  (#order-book-trading-trade-get-transaction-details-last-3-days), GET /api/v5/account/bills
  (#trading-account-rest-api-get-bills-details-last-7-days), live only: GET /api/v5/asset/deposit-history and
  /api/v5/asset/withdrawal-history (#funding-account-rest-api-get-deposit-history, ...-get-withdrawal-history; the
  docs say demo trading has no deposits or withdrawals).
- Rules/prices: GET /api/v5/public/instruments (#public-data-rest-api-get-instruments), GET /api/v5/market/ticker
  (#order-book-trading-market-data-get-ticker), GET /api/v5/account/trade-fee (#trading-account-rest-api-get-fee-rates;
  OKX signs rates negative for commission, so they are negated here), GET /api/v5/account/leverage-info
  (#trading-account-rest-api-get-leverage).
- Orders (testnet only): POST /api/v5/trade/order, POST /api/v5/trade/cancel-order, GET /api/v5/trade/order
  (#order-book-trading-trade-post-place-order, ...-post-cancel-order, ...-get-order-details). Errors (#error-code-rest-api):
  50004 and 51149 are documented as "does not mean that the request was successful or failed", so they, HTTP 5xx and
  "systems busy" (50001/50013/50026) on an order write are OutcomeUnknown; 51603 "Order does not exist" -> None.
- Quantities: OKX SWAP sizes (sz, accFillSz, fillSz, pos, lotSz, minSz) are in CONTRACTS; this app's quantities are
  always in the base asset. Base quantity = contracts x ctVal (linear swaps: ctVal is in the base currency). Symbol
  rules are converted the same way and an order whose base quantity is not a whole multiple of lotSz x ctVal is
  refused before sending. Converted records are marked provenance `derived`.
- Symbols: instId BTC-USDT <-> ("BTCUSDT", "spot"); BTC-USDT-SWAP <-> ("BTCUSDT", "usdm_perpetual"). Only quote
  currencies in SPOT_QUOTES (spot) / SWAP_QUOTES (swaps) map, so the mapping is reversible; other instruments
  (inverse swaps, futures, options, margin) are listed under `warnings` and skipped.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from ..credentials import Credential
from ..models import (
    AccountSnapshot, AccountTotals, Balance, Cashflow, CashflowKind, Environment, Fill, KeyPermissions, Order,
    OrderStatus, Position, Provenance, WalletType,
)
from .base import (
    AuthError, ExchangeAdapter, ExchangeError, ExchangeInfo, ExchangeUnavailable, HttpResponse, NotSupported,
    OrderRequest, OutcomeUnknown, RateLimited, SymbolRules, decimal_or_none as D, plain, register,
)

HOST = "https://openapi.okx.com"  # same REST host for live and demo trading (OKX docs)
DEMO_HEADER = ("x-simulated-trading", "1")
MARKETS = ("spot", "usdm_perpetual")
INST_TYPES = {"spot": "SPOT", "usdm_perpetual": "SWAP"}
SPOT_QUOTES = ("USDT", "USDC", "USDG", "EUR", "USD", "BTC", "ETH")  # longest/most specific first
SWAP_QUOTES = ("USDT", "USDC")  # linear swaps settled in a USD stablecoin = this app's usdm_perpetual
TIMEOUT_S = 10.0
INFO_TTL_S = 600.0
PAGE_MAX = 100  # documented maximum `limit` for orders, fills, bills, deposits and withdrawals
CL_ORD_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")  # documented clOrdId rule: alphanumerics, up to 32 characters

STATUS = {"live": OrderStatus.NEW, "partially_filled": OrderStatus.PARTIALLY_FILLED, "filled": OrderStatus.FILLED,
          "canceled": OrderStatus.CANCELED, "mmp_canceled": OrderStatus.CANCELED}
AUTH_CODES = {"50100", "50101", "50102", "50103", "50104", "50105", "50106", "50107", "50108", "50109", "50110",
              "50111", "50112", "50113", "50114", "50119", "50120", "50121", "50123"}
RATE_CODES = {"50011", "50040", "50061"}
BUSY_CODES = {"50001", "50013", "50026"}  # unavailable / systems busy / system error
UNKNOWN_OUTCOME_CODES = {"50004", "51149"}  # documented: request may or may not have taken effect
ORDER_MISSING = "51603"
TIME_PATH = "/api/v5/public/time"

_offsets: dict[str, tuple[float, int]] = {}  # host -> (fetched at, server - local ms)
_info_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}  # "env|instType" -> instId -> instrument
_cache_lock = threading.Lock()


def to_inst_id(symbol: str, market: str) -> str:
    """("BTCUSDT", "spot") -> "BTC-USDT"; ("BTCUSDT", "usdm_perpetual") -> "BTC-USDT-SWAP"."""
    quotes = SPOT_QUOTES if market == "spot" else SWAP_QUOTES if market == "usdm_perpetual" else ()
    for quote in quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            base_ccy = symbol[:-len(quote)]
            return f"{base_ccy}-{quote}" + ("-SWAP" if market == "usdm_perpetual" else "")
    raise NotSupported(f"OKX: {symbol} on {market} is not a supported spot pair or USDT/USDC perpetual")


def from_inst_id(inst_id: str) -> tuple[str, str] | None:
    """Inverse of to_inst_id; None for instruments this app does not map (inverse swaps, futures, options)."""
    parts = inst_id.split("-")
    if len(parts) == 2 and parts[1] in SPOT_QUOTES:
        return parts[0] + parts[1], "spot"
    if len(parts) == 3 and parts[2] == "SWAP" and parts[1] in SWAP_QUOTES:
        return parts[0] + parts[1], "usdm_perpetual"
    return None


def iso_timestamp(ms: int) -> str:
    """Epoch milliseconds -> the documented OK-ACCESS-TIMESTAMP format, e.g. 2020-12-08T09:08:57.715Z."""
    secs, millis = divmod(ms, 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{millis:03d}Z"


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    mac = hmac.new(secret.encode(), (timestamp + method.upper() + request_path + body).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


@register
class OKX(ExchangeAdapter):
    INFO = ExchangeInfo(
        id="okx", name="OKX", kind="crypto_exchange", environments=(Environment.LIVE, Environment.TESTNET),
        needs_passphrase=True, markets=MARKETS, key_setup_url="https://www.okx.com/account/my-api",
        notes=("Live: create a key with only the 'Read' permission. Keys with 'Trade' (which at OKX also allows "
               "funding transfers) or 'Withdraw' are refused. Enter the key's passphrase too.",
               "Testnet = OKX Demo Trading: create a Demo Trading API key under Trade > Demo Trading > Personal Center.",
               "Accounts registered on app.okx.com (US/AU) or my.okx.com (EEA) use other API domains and are not "
               "supported yet.",
               "Perpetual quantities are shown in the base asset (contracts x contract value). Demo orders on "
               "perpetuals need net (one-way) position mode.",
               "Binding the key to your IP address at OKX is recommended."))

    # ---- transport ----
    def _offset_ms(self) -> int:
        with _cache_lock:
            hit = _offsets.get(HOST)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        before = self.now_ms()
        rows = self._unwrap(self.transport("GET", HOST + TIME_PATH, self._env_headers(), None, TIMEOUT_S))
        after = self.now_ms()
        ts = rows[0].get("ts") if rows and isinstance(rows[0], dict) else None
        offset = int(ts) - (before + after) // 2 if str(ts or "").isdigit() else 0
        with _cache_lock:
            _offsets[HOST] = (time.monotonic(), offset)
        return offset

    def _env_headers(self) -> dict[str, str]:
        return dict([DEMO_HEADER]) if self.environment is Environment.TESTNET else {}

    def _signed(self, cred: Credential, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None, *, order_write: bool = False) -> list[Any]:
        if cred.passphrase is None:
            raise AuthError("OKX keys need their passphrase")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        request_path = path + (f"?{urlencode(query)}" if query else "")
        text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = iso_timestamp(self.now_ms() + self._offset_ms())
        headers = {"Content-Type": "application/json", "OK-ACCESS-KEY": cred.api_key.reveal(),
                   "OK-ACCESS-SIGN": sign(cred.api_secret.reveal(), ts, method, request_path, text),
                   "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": cred.passphrase.reveal(), **self._env_headers()}
        resp = self.transport(method, HOST + request_path, headers, text.encode() if body is not None else None,
                              TIMEOUT_S)
        return self._unwrap(resp, order_write=order_write)

    def _public(self, path: str, params: dict[str, Any]) -> list[Any]:
        url = f"{HOST}{path}?{urlencode(params)}"
        return self._unwrap(self.transport("GET", url, self._env_headers(), None, TIMEOUT_S))

    def _unwrap(self, resp: HttpResponse, *, order_write: bool = False) -> list[Any]:
        """OKX answers most errors with HTTP 200 and a non-"0" `code`; per-order results carry `sCode`/`sMsg`."""
        if order_write and resp.status >= 500:
            raise OutcomeUnknown(f"OKX answered HTTP {resp.status}: the order's execution status is unknown")
        try:
            data = json.loads(resp.body or b"null")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if resp.status == 429:
                raise RateLimited("OKX rate limit reached", 2.0)
            if resp.status in (401, 403):
                raise AuthError(f"OKX rejected the key (HTTP {resp.status})")
            if resp.status >= 500:
                raise ExchangeUnavailable(f"OKX is unavailable (HTTP {resp.status})")
            raise ExchangeUnavailable("OKX returned a response that is not JSON")
        code, msg = str(data.get("code", "")), str(data.get("msg", ""))[:160]
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        # Place/cancel report the real reason per row (sCode/sMsg); OKX then sets code "1".
        row = next((r for r in rows if isinstance(r, dict) and str(r.get("sCode", "0")) != "0"), None)
        if code == "0" and resp.status < 400 and row is None:
            return rows
        if row is not None:
            code, msg = str(row.get("sCode")), str(row.get("sMsg", ""))[:160]
        detail = f"code {code}: {msg}" if code or msg else f"HTTP {resp.status}"
        if order_write and (code in UNKNOWN_OUTCOME_CODES or code in BUSY_CODES):
            raise OutcomeUnknown(f"OKX could not confirm the order ({detail}): its execution status is unknown")
        if code in RATE_CODES or resp.status == 429:
            raise RateLimited(f"OKX rate limit reached ({detail})", 2.0)
        if code == "50101":
            raise AuthError("OKX: this key belongs to the other environment (demo trading keys and live keys are "
                            "separate)")
        if code in AUTH_CODES or resp.status in (401, 403):
            raise AuthError(f"OKX rejected the key ({detail})")
        if code in BUSY_CODES or code in UNKNOWN_OUTCOME_CODES or resp.status >= 500:
            raise ExchangeUnavailable(f"OKX is unavailable ({detail})")
        raise ExchangeError(f"OKX refused the request ({detail})")

    def _require_testnet(self) -> None:
        if self.environment is not Environment.TESTNET:
            raise NotSupported("live order endpoints are disabled in this app (AGENTS.md §2); use a testnet connection")

    def _config(self, cred: Credential) -> dict[str, Any]:
        rows = self._signed(cred, "GET", "/api/v5/account/config")
        if not rows or not isinstance(rows[0], dict):
            raise ExchangeUnavailable("OKX returned no account configuration")
        return rows[0]

    # ---- instruments ----
    def _instruments(self, inst_type: str) -> dict[str, dict[str, Any]]:
        key = f"{self.environment}|{inst_type}"  # demo and live instrument lists can differ
        with _cache_lock:
            hit = _info_cache.get(key)
        if hit is None or time.monotonic() - hit[0] > INFO_TTL_S:
            rows = self._public("/api/v5/public/instruments", {"instType": inst_type})
            hit = (time.monotonic(), {r["instId"]: r for r in rows if isinstance(r, dict) and "instId" in r})
            with _cache_lock:
                _info_cache[key] = hit
        return hit[1]

    def _contract_value(self, inst_id: str) -> Decimal | None:
        """Base units per contract for a linear swap; None when OKX does not report it."""
        inst = self._instruments("SWAP").get(inst_id, {})
        ct_val = D(inst.get("ctVal"))
        return ct_val if inst.get("ctType") == "linear" and ct_val and ct_val > 0 else None

    def _base_qty(self, value: Any, market: str, ct_val: Decimal | None) -> Decimal | None:
        qty = D(value)
        if market == "spot" or qty is None:
            return qty
        return qty * ct_val if ct_val is not None else None

    # ---- permissions ----
    def permissions(self, cred: Credential) -> KeyPermissions:
        cfg = self._config(cred)  # also proves key, secret and passphrase work in this environment
        perm = cfg.get("perm")
        if not isinstance(perm, str):
            return KeyPermissions(source="okx account/config (perm missing)")
        tokens = {t.strip() for t in perm.split(",") if t.strip()}
        trade, unknown = "trade" in tokens, bool(tokens - {"read_only", "trade", "withdraw"})
        ip = cfg.get("ip")
        return KeyPermissions(
            read=True if "read_only" in tokens else None, spot_trade=trade, derivatives_trade=trade,
            margin_trade=trade, withdraw="withdraw" in tokens,
            transfer=True if trade else (None if unknown else False),  # OKX: Trade includes funding transfer
            ip_restricted=bool(ip) if isinstance(ip, str) else None, source="okx account/config perm")

    # ---- account snapshot ----
    def snapshot(self, cred: Credential, connection_id: str) -> AccountSnapshot:
        warnings: list[str] = []
        acc = next(iter(self._signed(cred, "GET", "/api/v5/account/balance")), {}) or {}
        totals = [AccountTotals(wallet=WalletType.UNIFIED, equity=D(acc.get("totalEq")),
                                available_balance=D(acc.get("availEq")), initial_margin=D(acc.get("imr")),
                                maintenance_margin=D(acc.get("mmr")), unrealized_pnl=D(acc.get("upl")),
                                margin_ratio=D(acc.get("mgnRatio")), quote_asset="USD")] if acc else []
        balances = [Balance(asset=d["ccy"], wallet=WalletType.UNIFIED, free=D(d.get("availBal")),
                            locked=D(d.get("frozenBal")), total=D(d.get("eq")), usd_value=D(d.get("eqUsd")),
                            usd_value_provenance=Provenance.EXCHANGE_REPORTED if D(d.get("eqUsd")) is not None else None)
                    for d in acc.get("details", []) if isinstance(d, dict) and d.get("ccy")]
        positions: list[Position] = []
        for p in self._signed(cred, "GET", "/api/v5/account/positions", {"instType": "SWAP"}):
            pos = self._position(p)
            if pos is None:
                if D(p.get("pos")):
                    warnings.append(f"position {p.get('instId')} is not shown (unsupported instrument or contract "
                                    "value unknown)")
                continue
            positions.append(pos)
        orders: list[Order] = []
        for market in MARKETS:
            for o in self._signed(cred, "GET", "/api/v5/trade/orders-pending", {"instType": INST_TYPES[market]}):
                order = self._order(o)
                if order is None:
                    warnings.append(f"open order {o.get('instId')} is not shown (unsupported instrument)")
                else:
                    orders.append(order)
        return AccountSnapshot(connection_id=connection_id, exchange="okx", environment=self.environment,
                               as_of_ms=self.now_ms(), totals=totals, balances=balances, positions=positions,
                               open_orders=orders, warnings=warnings)

    def _position(self, p: dict[str, Any]) -> Position | None:
        mapped = from_inst_id(str(p.get("instId", "")))
        amount = D(p.get("pos"))
        if mapped is None or mapped[1] != "usdm_perpetual" or not amount:
            return None
        ct_val = self._contract_value(p["instId"])
        if ct_val is None:
            return None
        side_field = p.get("posSide", "net")
        side = side_field if side_field in ("long", "short") else ("long" if amount > 0 else "short")
        liq = D(p.get("liqPx"))
        return Position(
            symbol=mapped[0], market="usdm_perpetual", side=side, quantity=abs(amount) * ct_val,
            entry_price=D(p.get("avgPx")), mark_price=D(p.get("markPx")), leverage=D(p.get("lever")),
            margin_mode=p.get("mgnMode") if p.get("mgnMode") in ("cross", "isolated") else None,
            liquidation_price=liq if liq else None,  # OKX reports "" when no liquidation price applies
            unrealized_pnl=D(p.get("upl")), notional=D(p.get("notionalUsd")),
            provenance=Provenance.DERIVED)  # quantity = contracts x ctVal

    def _order(self, o: dict[str, Any]) -> Order | None:
        mapped = from_inst_id(str(o.get("instId", "")))
        if mapped is None or o.get("side") not in ("buy", "sell"):
            return None
        symbol, market = mapped
        ct_val = self._contract_value(o["instId"]) if market == "usdm_perpetual" else None
        quantity = self._base_qty(o.get("sz"), market, ct_val)
        if market == "spot" and o.get("ordType") == "market" and o.get("tgtCcy") == "quote_ccy":
            quantity = None  # sz is an amount of the quote currency, not a base quantity
        reduce = {"true": True, "false": False}.get(str(o.get("reduceOnly", "")).lower())
        return Order(order_id=str(o.get("ordId", "")), client_order_id=o.get("clOrdId") or None, symbol=symbol,
                     market=market, side=o["side"], type=str(o.get("ordType", "")),
                     status=STATUS.get(str(o.get("state")), OrderStatus.UNKNOWN), price=D(o.get("px")) or None,
                     quantity=quantity, filled_quantity=self._base_qty(o.get("accFillSz"), market, ct_val),
                     average_price=D(o.get("avgPx")) or None, reduce_only=reduce,
                     created_ms=_ms(o.get("cTime")), updated_ms=_ms(o.get("uTime")),
                     provenance=Provenance.DERIVED if market == "usdm_perpetual" else Provenance.EXCHANGE_REPORTED)

    # ---- history ----
    def order_history(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Order]:
        """Open orders plus completed orders of the last 7 days (OKX keeps older ones in a separate archive)."""
        inst_id = to_inst_id(symbol, market)
        params = {"instType": INST_TYPES[market], "instId": inst_id, "limit": min(limit, PAGE_MAX)}
        rows = self._signed(cred, "GET", "/api/v5/trade/orders-pending", params)
        rows += self._signed(cred, "GET", "/api/v5/trade/orders-history", params)
        orders = [o for o in (self._order(r) for r in rows) if o is not None]
        return sorted(orders, key=lambda o: o.created_ms or 0, reverse=True)[:limit]

    def fills(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Fill]:
        inst_id = to_inst_id(symbol, market)
        rows = self._signed(cred, "GET", "/api/v5/trade/fills",
                            {"instType": INST_TYPES[market], "instId": inst_id, "limit": min(limit, PAGE_MAX)})
        ct_val = self._contract_value(inst_id) if market == "usdm_perpetual" else None
        out = []
        for t in rows:
            qty, fee = self._base_qty(t.get("fillSz"), market, ct_val), D(t.get("fee"))
            if qty is None or D(t.get("fillPx")) is None or t.get("side") not in ("buy", "sell"):
                continue
            out.append(Fill(trade_id=str(t.get("tradeId")), order_id=str(t.get("ordId", "")), symbol=symbol,
                            market=market, side=t["side"], price=D(t["fillPx"]),
                            quantity=qty, fee=-fee if fee is not None else None,  # OKX: negative = fee paid
                            fee_asset=t.get("feeCcy") or None,
                            realized_pnl=D(t.get("fillPnl")) if market == "usdm_perpetual" else None,
                            is_maker={"M": True, "T": False}.get(t.get("execType")),
                            time_ms=_ms(t.get("fillTime")) or _ms(t.get("ts")) or 0))
        return out

    def cashflows(self, cred: Credential, *, limit: int = 100) -> list[Cashflow]:
        out: list[Cashflow] = []
        for b in self._signed(cred, "GET", "/api/v5/account/bills", {"limit": min(limit, PAGE_MAX)}):
            out += _bill_cashflows(b)
        if self.environment is Environment.LIVE:  # demo trading has no deposits or withdrawals (OKX docs)
            for r in self._signed(cred, "GET", "/api/v5/asset/deposit-history", {"limit": min(limit, PAGE_MAX)}):
                out.append(Cashflow(kind=CashflowKind.DEPOSIT, asset=r.get("ccy", ""), amount=D(r.get("amt")) or Decimal(0),
                                    time_ms=_ms(r.get("ts")) or 0, status=str(r.get("state")),
                                    reference=r.get("txId") or r.get("depId") or None))
            for r in self._signed(cred, "GET", "/api/v5/asset/withdrawal-history", {"limit": min(limit, PAGE_MAX)}):
                out.append(Cashflow(kind=CashflowKind.WITHDRAWAL, asset=r.get("ccy", ""),
                                    amount=-(D(r.get("amt")) or Decimal(0)), time_ms=_ms(r.get("ts")) or 0,
                                    status=str(r.get("state")), reference=r.get("txId") or r.get("wdId") or None))
        return sorted(out, key=lambda c: c.time_ms, reverse=True)[:limit]

    # ---- public symbol rules ----
    def symbol_rules(self, symbol: str, market: str) -> SymbolRules:
        inst_id = to_inst_id(symbol, market)
        inst = self._instruments(INST_TYPES[market]).get(inst_id)
        if inst is None:
            raise ExchangeError(f"OKX does not list {inst_id}")
        state = str(inst.get("state", "unknown"))
        status = "TRADING" if state == "live" else state.upper()  # the preview checks for TRADING
        if market == "spot":
            mult, base_ccy, quote_ccy = Decimal(1), inst.get("baseCcy", ""), inst.get("quoteCcy", "")
        else:
            mult = self._contract_value(inst_id)
            if mult is None:
                raise ExchangeError(f"OKX reports no linear contract value for {inst_id}")
            base_ccy, quote_ccy = inst.get("ctValCcy", ""), inst.get("settleCcy", "")

        def scaled(field: str) -> Decimal | None:
            v = D(inst.get(field))
            return Decimal(plain(v * mult)) if v is not None else None

        # min_notional: OKX documents no minimum order value for these instruments, so it stays None.
        return SymbolRules(symbol=symbol, market=market, base_asset=base_ccy, quote_asset=quote_ccy, status=status,
                           price_tick=D(inst.get("tickSz")), quantity_step=scaled("lotSz"),
                           min_quantity=scaled("minSz"), max_quantity=scaled("maxLmtSz"), min_notional=None,
                           order_types=("limit", "market"))

    def reference_price(self, symbol: str, market: str) -> Decimal:
        rows = self._public("/api/v5/market/ticker", {"instId": to_inst_id(symbol, market)})
        price = D(rows[0].get("last")) if rows and isinstance(rows[0], dict) else None
        if not price:
            raise ExchangeError(f"OKX returned no price for {symbol}")
        return price

    def fee_rates(self, cred: Credential, symbol: str, market: str) -> tuple[Decimal, Decimal] | None:
        inst_id = to_inst_id(symbol, market)
        params = {"instType": INST_TYPES[market]}
        params.update({"instId": inst_id} if market == "spot" else {"instFamily": inst_id.removesuffix("-SWAP")})
        rows = self._signed(cred, "GET", "/api/v5/account/trade-fee", params)
        groups = rows[0].get("feeGroup", []) if rows and isinstance(rows[0], dict) else []
        group_id = str(self._instruments(INST_TYPES[market]).get(inst_id, {}).get("groupId", ""))
        group = next((g for g in groups if str(g.get("groupId")) == group_id), groups[0] if len(groups) == 1 else None)
        maker, taker = (D(group.get("maker")), D(group.get("taker"))) if group else (None, None)
        # OKX: negative rate = commission, positive = rebate. This app uses positive = cost.
        return (-maker, -taker) if maker is not None and taker is not None else None

    def leverage(self, cred: Credential, symbol: str, market: str) -> Decimal | None:
        if market != "usdm_perpetual":
            return None
        inst_id = to_inst_id(symbol, market)
        rows = self._signed(cred, "GET", "/api/v5/account/leverage-info", {"instId": inst_id, "mgnMode": "cross"})
        return next((D(r.get("lever")) for r in rows if isinstance(r, dict) and r.get("instId") == inst_id), None)

    # ---- testnet-only order management ----
    def place_order(self, cred: Credential, order: OrderRequest) -> Order:
        self._require_testnet()
        if not CL_ORD_ID.match(order.client_order_id):
            raise ExchangeError("OKX client order IDs must be 1-32 letters or digits")
        inst_id = to_inst_id(order.symbol, order.market)
        cfg = self._config(cred)
        body: dict[str, Any] = {"instId": inst_id, "side": order.side, "clOrdId": order.client_order_id}
        if order.market == "spot":
            # Spot mode / Futures mode trade spot as "cash"; Multi-currency and Portfolio margin use "cross".
            mode = {"1": "cash", "2": "cash", "3": "cross", "4": "cross"}.get(str(cfg.get("acctLv")))
            if mode is None:
                raise ExchangeError("OKX did not report the account mode; cannot choose the spot trade mode")
            body.update(tdMode=mode, sz=plain(order.quantity))
            if order.type == "market":
                body["tgtCcy"] = "base_ccy"  # otherwise a market buy's sz would be an amount of USDT
        else:
            if cfg.get("posMode") != "net_mode":
                raise NotSupported("OKX perpetual orders need net (one-way) position mode on the demo account")
            ct_val = self._contract_value(inst_id)
            lot = D(self._instruments("SWAP").get(inst_id, {}).get("lotSz"))
            if ct_val is None or not lot:
                raise ExchangeError(f"OKX reports no contract value or lot size for {inst_id}")
            contracts = order.quantity / ct_val
            if contracts % lot != 0:
                raise ExchangeError(f"quantity must be a multiple of {plain(lot * ct_val)} (OKX lot size "
                                    f"{plain(lot)} contract(s) x contract value {plain(ct_val)})")
            body.update(tdMode="cross", sz=plain(contracts))
            if order.reduce_only:
                body["reduceOnly"] = True
        if order.type == "limit":
            body.update(ordType={"GTC": "limit", "IOC": "ioc", "FOK": "fok"}[order.time_in_force or "GTC"],
                        px=plain(order.price))
        else:
            body["ordType"] = "market"
        ack = next(iter(self._signed(cred, "POST", "/api/v5/trade/order", body=body, order_write=True)), {}) or {}
        try:
            found = self._lookup(cred, inst_id, order.client_order_id)
        except ExchangeError:
            found = None
        if found is not None:
            return found
        # Accepted, but the follow-up lookup did not answer: report the acknowledgement with an unconfirmed status.
        return Order(order_id=str(ack.get("ordId", "")), client_order_id=order.client_order_id, symbol=order.symbol,
                     market=order.market, side=order.side, type=body["ordType"], status=OrderStatus.UNKNOWN,
                     price=order.price, quantity=order.quantity, created_ms=_ms(ack.get("ts")))

    def query_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order | None:
        self._require_testnet()
        return self._lookup(cred, to_inst_id(symbol, market), client_order_id)

    def _lookup(self, cred: Credential, inst_id: str, client_order_id: str) -> Order | None:
        try:
            rows = self._signed(cred, "GET", "/api/v5/trade/order", {"instId": inst_id, "clOrdId": client_order_id})
        except ExchangeError as exc:
            if f"code {ORDER_MISSING}:" in str(exc):  # order does not exist
                return None
            raise
        return self._order(rows[0]) if rows and isinstance(rows[0], dict) else None

    def cancel_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order:
        self._require_testnet()
        inst_id = to_inst_id(symbol, market)
        self._signed(cred, "POST", "/api/v5/trade/cancel-order", body={"instId": inst_id, "clOrdId": client_order_id},
                     order_write=True)
        try:
            found = self._lookup(cred, inst_id, client_order_id)
        except ExchangeError as exc:
            raise OutcomeUnknown(f"OKX accepted the cancel request but the order lookup failed ({exc})") from exc
        if found is None:
            raise OutcomeUnknown("OKX accepted the cancel request but the order could not be found afterwards")
        return found


def _bill_cashflows(b: dict[str, Any]) -> list[Cashflow]:
    """One bill can carry a fee and a realized PnL. Subtypes per the bills docs: 173/174 funding fee expense/income,
    1-6 trades (buy, sell, open/close long/short); bill type 1 = transfer (GET /api/v5/account/subtypes example)."""
    sub, kind_type = str(b.get("subType", "")), str(b.get("type", ""))
    common = {"asset": b.get("ccy", ""), "time_ms": _ms(b.get("ts")) or 0, "reference": b.get("billId") or None}
    symbol = (from_inst_id(str(b.get("instId", ""))) or (None,))[0]
    if sub in ("173", "174"):
        amount = D(b.get("pnl")) if D(b.get("pnl")) is not None else D(b.get("balChg"))  # docs: refer to pnl
        return [Cashflow(kind=CashflowKind.FUNDING_FEE, amount=amount or Decimal(0), symbol=symbol, **common)]
    if sub in ("1", "2", "3", "4", "5", "6"):
        out = []
        if D(b.get("fee")):
            out.append(Cashflow(kind=CashflowKind.COMMISSION, amount=D(b["fee"]), symbol=symbol, **common))
        if D(b.get("pnl")):
            out.append(Cashflow(kind=CashflowKind.REALIZED_PNL, amount=D(b["pnl"]), symbol=symbol, **common))
        return out
    if kind_type == "1":
        return [Cashflow(kind=CashflowKind.INTERNAL_TRANSFER, amount=D(b.get("balChg")) or Decimal(0), **common)]
    return [Cashflow(kind=CashflowKind.OTHER, amount=D(b.get("balChg")) or Decimal(0), symbol=symbol, **common)]


def _ms(value: Any) -> int | None:
    return int(value) if str(value or "").isdigit() else None
