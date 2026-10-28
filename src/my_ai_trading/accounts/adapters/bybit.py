"""Bybit V5 (Unified Trading Account): spot + USDT perpetual (category `linear`). Endpoints and fields follow the
official V5 docs at https://bybit-exchange.github.io/docs/v5/ (source: github.com/bybit-exchange/docs) and the
official pybit SDK (checked 2026-10-27):

- Hosts (/v5/guide): live https://api.bybit.com, testnet https://api-testnet.bybit.com (keys from testnet.bybit.com).
  Demo trading (api-demo.bybit.com, /v5/demo) is a separate environment without key-info or fee-rate endpoints and
  is not used.
- Signing (/v5/guide#create-a-request): HMAC-SHA256 hex of timestamp + api key + recv window + (query string | JSON
  body), sent in X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-RECV-WINDOW / X-BAPI-SIGN. Server time: /v5/market/time.
- Key permissions: GET /v5/user/query-api (/v5/user/apikey-info). `readOnly` 1 = read only, 0 = read and write.
- Account: GET /v5/account/wallet-balance (/v5/account/wallet-balance), GET /v5/account/info (margin mode,
  /v5/account/account-info), GET /v5/position/list (/v5/position), GET /v5/order/realtime (/v5/order/open-order).
- History: GET /v5/order/history (/v5/order/order-list), GET /v5/execution/list (/v5/order/execution),
  GET /v5/account/transaction-log (/v5/account/transaction-log), live only: GET /v5/asset/deposit/query-record
  (/v5/asset/deposit/deposit-record), GET /v5/asset/withdraw/query-record (/v5/asset/withdraw/withdraw-record).
- Public: GET /v5/market/instruments-info (/v5/market/instrument), GET /v5/market/tickers (/v5/market/tickers);
  fees GET /v5/account/fee-rate (/v5/account/fee-rate).
- Orders (testnet only): POST /v5/order/create (/v5/order/create-order), POST /v5/order/cancel
  (/v5/order/cancel-order). Both answer with an acknowledgement only ("this request is asynchronous"), so the
  status is looked up afterwards and stays UNKNOWN until Bybit reports it.
- Errors come as HTTP 200 with retCode != 0 (/v5/error). Server timeout / server error / system busy / "submitted
  for processing" / order timeout codes on an order write are OutcomeUnknown: reconcile, never resend.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable
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

HOSTS = {Environment.LIVE: "https://api.bybit.com", Environment.TESTNET: "https://api-testnet.bybit.com"}
CATEGORY = {"spot": "spot", "usdm_perpetual": "linear"}
MARKETS = ("spot", "usdm_perpetual")
SETTLE = "USDT"  # usdm_perpetual = USDT-settled linear contracts
RECV_WINDOW_MS = 5000
TIMEOUT_S = 10.0
SNAPSHOT_ROWS = 500
WEEK_MS = 7 * 24 * 3600 * 1000

STATUS = {"New": OrderStatus.NEW, "PartiallyFilled": OrderStatus.PARTIALLY_FILLED, "Untriggered": OrderStatus.NEW,
          "Triggered": OrderStatus.NEW, "Filled": OrderStatus.FILLED, "Cancelled": OrderStatus.CANCELED,
          "PartiallyFilledCanceled": OrderStatus.CANCELED, "Deactivated": OrderStatus.CANCELED,
          "Rejected": OrderStatus.REJECTED}
MARGIN_MODES = {"REGULAR_MARGIN": "cross", "ISOLATED_MARGIN": "isolated"}  # PORTFOLIO_MARGIN is neither: None

AUTH_CODES = {10002, 10003, 10004, 10005, 10007, 10010, 33004, -2015}
RATE_CODES = {10006, 10018}
# On an order write these mean Bybit may have accepted it: timeouts, server errors, system busy (429),
# "submitted to the system for processing" (170035), duplicate orderLinkId (an order with our ID exists).
UNKNOWN_WRITE_CODES = {10000, 10016, 429, 170001, 170007, 170032, 170035, 170146, 170147, 110079, 10014, 110072}
SERVER_CODES = {10000, 10016, 429}

# /v5/user/query-api permission groups. Anything else non-empty on a read-write key is treated as unknown.
TRADE_GROUPS = {"Spot": "spot", "Exchange": "spot", "ContractTrade": "derivatives", "Options": "derivatives",
                "Derivatives": "derivatives", "BlockTrade": "derivatives", "CopyTrading": "derivatives"}
PAYOUT_GROUPS = {"FiatP2P", "FiatBitPay", "FiatConvertBroker", "FiatGlobalPay", "BitCard"}  # funds can leave
HARMLESS_GROUPS = {"ByXPost", "Affiliate", "NFT"}

_offsets: dict[str, tuple[float, int]] = {}  # base URL -> (fetched at, server - local ms)
_cache_lock = threading.Lock()


@register
class Bybit(ExchangeAdapter):
    INFO = ExchangeInfo(
        id="bybit", name="Bybit", kind="crypto_exchange", environments=(Environment.LIVE, Environment.TESTNET),
        needs_passphrase=False, markets=MARKETS, key_setup_url="https://www.bybit.com/app/user/api-management",
        notes=("Live: create a 'Read-Only' API key. Keys that can trade, withdraw or transfer are refused.",
               "Testnet keys come from testnet.bybit.com/app/user/api-management (a separate testnet account). "
               "Demo-trading keys from bybit.com do not work here.",
               "Requires a Unified Trading Account. Restricting the key to your IP address at Bybit is recommended."))

    # ---- transport ----
    @property
    def _base(self) -> str:
        return HOSTS[self.environment]

    def _offset_ms(self) -> int:
        with _cache_lock:
            hit = _offsets.get(self._base)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        before = self.now_ms()
        r = self._result(self.transport("GET", self._base + "/v5/market/time", {}, None, TIMEOUT_S))
        after = self.now_ms()
        server = int(r["timeNano"]) // 1_000_000 if r.get("timeNano") else int(r.get("timeSecond", 0)) * 1000
        offset = server - (before + after) // 2 if server else 0
        with _cache_lock:
            _offsets[self._base] = (time.monotonic(), offset)
        return offset

    def _signed(self, cred: Credential, method: str, path: str, params: dict[str, Any] | None = None, *,
                order_write: bool = False) -> Any:
        ts = str(self.now_ms() + self._offset_ms())
        key = cred.api_key.reveal()
        headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS)}
        if method == "GET":
            payload = urlencode(params or {}, safe="%")  # cursors arrive already percent-encoded
            url, body = self._base + path + (f"?{payload}" if payload else ""), None
        else:
            payload = json.dumps(params or {}, separators=(",", ":"))
            url, body = self._base + path, payload.encode()
            headers["Content-Type"] = "application/json"
        headers["X-BAPI-SIGN"] = hmac.new(cred.api_secret.reveal().encode(), (ts + key + str(RECV_WINDOW_MS) + payload)
                                          .encode(), hashlib.sha256).hexdigest()
        return self._result(self.transport(method, url, headers, body, TIMEOUT_S), order_write=order_write)

    def _public(self, path: str, params: dict[str, Any]) -> Any:
        return self._result(self.transport("GET", f"{self._base}{path}?{urlencode(params)}", {}, None, TIMEOUT_S))

    def _result(self, resp: HttpResponse, *, order_write: bool = False) -> Any:
        if order_write and resp.status >= 500:
            raise OutcomeUnknown(f"Bybit answered HTTP {resp.status}: the order's outcome is unknown")
        if resp.status == 403:  # /v5/error: IP rate limit breached (wait 10 minutes) or a restricted region
            raise RateLimited("Bybit refused access (HTTP 403): IP rate limit or region restriction", 600.0)
        try:
            data = self.check(resp)
        except ExchangeUnavailable as exc:
            if order_write:
                raise OutcomeUnknown("Bybit's answer could not be read: the order's outcome is unknown") from exc
            raise
        if not isinstance(data, dict) or "retCode" not in data:
            if order_write:
                raise OutcomeUnknown("Bybit's answer had no retCode: the order's outcome is unknown")
            raise ExchangeUnavailable("Bybit returned an unexpected response")
        code = data.get("retCode")
        if code == 0:
            return data.get("result") or {}
        msg = f"code {code}: {str(data.get('retMsg', ''))[:160]}"
        if order_write and code in UNKNOWN_WRITE_CODES:
            raise OutcomeUnknown(f"Bybit could not confirm the order ({msg}): its outcome is unknown")
        if code in AUTH_CODES:
            if code == 10002:  # timestamp outside the recv window: resync the clock next time
                with _cache_lock:
                    _offsets.pop(self._base, None)
            raise AuthError(f"Bybit rejected the key ({msg})")
        if code in RATE_CODES or code == 429:
            reset = resp.headers.get("x-bapi-limit-reset-timestamp", "")
            wait = (int(reset) - self.now_ms()) / 1000 if reset.isdigit() else 1.0
            raise RateLimited("Bybit rate limit reached", max(wait, 1.0))
        if code in SERVER_CODES:
            raise ExchangeUnavailable(f"Bybit is unavailable ({msg})")
        raise ExchangeError(f"Bybit refused the request ({msg})")

    def _pages(self, cred: Credential, path: str, params: dict[str, Any], limit: int, page_max: int,
               key: str = "list") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while len(rows) < limit:
            query = {**params, "limit": min(page_max, limit - len(rows))}
            if cursor:
                query["cursor"] = cursor
            r = self._signed(cred, "GET", path, query)
            page = r.get(key) or []
            rows += page
            cursor = r.get("nextPageCursor") or ""
            if not page or not cursor:
                break
        return rows[:limit]

    def _require_testnet(self) -> None:
        if self.environment is not Environment.TESTNET:
            raise NotSupported("live order endpoints are disabled in this app (AGENTS.md §2); use a testnet connection")

    # ---- permissions ----
    def permissions(self, cred: Credential) -> KeyPermissions:
        r = self._signed(cred, "GET", "/v5/user/query-api")
        groups = {k: v for k, v in (r.get("permissions") or {}).items() if v}
        wallet = set(groups.get("Wallet", []))
        ips = r.get("ips")
        common = {"read": True, "ip_restricted": bool(ips) and "*" not in ips if isinstance(ips, list) else None,
                  "expires_ms": _iso_ms(r.get("expiredAt")), "source": "bybit query-api"}
        read_only = r.get("readOnly")
        if read_only == 1:  # permission groups on a read-only key are read scopes; a withdraw scope is contradictory
            return KeyPermissions(spot_trade=False, derivatives_trade=False, margin_trade=False,
                                  withdraw=None if "Withdraw" in wallet else False, transfer=False, **common)
        if read_only != 0:
            return KeyPermissions(**common)  # unknown flag: every write permission stays unknown (refused live)
        kinds = {TRADE_GROUPS[g] for g in groups if g in TRADE_GROUPS}
        unknown = set(groups) - set(TRADE_GROUPS) - PAYOUT_GROUPS - HARMLESS_GROUPS - {"Wallet", "Earn"}
        transfer = bool(wallet - {"Withdraw"}) or "Earn" in groups
        return KeyPermissions(spot_trade="spot" in kinds, derivatives_trade="derivatives" in kinds,
                              margin_trade="spot" in kinds,  # spot margin trades through the spot permission
                              withdraw="Withdraw" in wallet or bool(PAYOUT_GROUPS & set(groups)),
                              transfer=True if transfer else (None if unknown else False), **common)

    # ---- account snapshot ----
    def snapshot(self, cred: Credential, connection_id: str) -> AccountSnapshot:
        totals: list[AccountTotals] = []
        balances: list[Balance] = []
        positions: list[Position] = []
        orders: list[Order] = []
        unavailable: list[str] = []
        warnings: list[str] = []
        errors: list[ExchangeError] = []

        def section(name: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except (RateLimited, ExchangeUnavailable, OutcomeUnknown):
                raise
            except ExchangeError as exc:  # a definite refusal: this key/account cannot provide the section
                errors.append(exc)
                unavailable.append(f"{name}: {exc}")

        def wallet() -> None:
            for acc in self._signed(cred, "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}).get("list", []):
                totals.append(AccountTotals(
                    wallet=WalletType.UNIFIED, equity=D(acc.get("totalEquity")),
                    wallet_balance=D(acc.get("totalWalletBalance")), available_balance=D(acc.get("totalAvailableBalance")),
                    initial_margin=D(acc.get("totalInitialMargin")), maintenance_margin=D(acc.get("totalMaintenanceMargin")),
                    unrealized_pnl=D(acc.get("totalPerpUPL")), margin_ratio=D(acc.get("accountMMRate")), quote_asset="USD"))
                for c in acc.get("coin", []):
                    usd = D(c.get("usdValue"))
                    balances.append(Balance(asset=c["coin"], wallet=WalletType.UNIFIED, locked=D(c.get("locked")),
                                            total=D(c.get("walletBalance")), usd_value=usd,
                                            usd_value_provenance=Provenance.EXCHANGE_REPORTED if usd is not None else None))

        def linear_positions() -> None:
            margin = None
            try:
                mode = self._signed(cred, "GET", "/v5/account/info").get("marginMode")
                margin = MARGIN_MODES.get(str(mode))
            except (RateLimited, ExchangeUnavailable, OutcomeUnknown):
                raise
            except ExchangeError:
                warnings.append("margin mode could not be read from Bybit")
            rows = self._pages(cred, "/v5/position/list", {"category": "linear", "settleCoin": SETTLE}, SNAPSHOT_ROWS, 200)
            for p in rows:
                size = D(p.get("size")) or Decimal(0)
                side = {"Buy": "long", "Sell": "short"}.get(p.get("side", ""))
                if size == 0 or side is None:
                    continue
                positions.append(Position(
                    symbol=p["symbol"], market="usdm_perpetual", side=side, quantity=size,
                    entry_price=D(p.get("avgPrice")), mark_price=D(p.get("markPrice")), leverage=D(p.get("leverage")),
                    margin_mode=margin, liquidation_price=D(p.get("liqPrice")) or None,  # "" when none applies
                    unrealized_pnl=D(p.get("unrealisedPnl")), notional=D(p.get("positionValue"))))

        def open_orders(market: str) -> Callable[[], None]:
            def run() -> None:
                params = {"category": CATEGORY[market], **({"settleCoin": SETTLE} if market == "usdm_perpetual" else {})}
                orders.extend(self._order(o, market)
                              for o in self._pages(cred, "/v5/order/realtime", params, SNAPSHOT_ROWS, 50))
            return run

        section("unified account balances", wallet)
        section("usdm_perpetual positions", linear_positions)
        for market in MARKETS:
            section(f"{market} open orders", open_orders(market))
        if len(unavailable) == 2 + len(MARKETS):
            raise errors[0]
        return AccountSnapshot(connection_id=connection_id, exchange="bybit", environment=self.environment,
                               as_of_ms=self.now_ms(), totals=totals, balances=balances, positions=positions,
                               open_orders=orders, unavailable=unavailable, warnings=warnings)

    def _order(self, o: dict[str, Any], market: str) -> Order:
        return Order(order_id=str(o.get("orderId")), client_order_id=o.get("orderLinkId") or None, symbol=o["symbol"],
                     market=market, side=str(o.get("side", "")).lower(), type=str(o.get("orderType", "")).lower(),
                     status=STATUS.get(str(o.get("orderStatus")), OrderStatus.UNKNOWN),
                     price=D(o.get("price")) or None, stop_price=D(o.get("triggerPrice")) or None,
                     quantity=D(o.get("qty")), filled_quantity=D(o.get("cumExecQty")),
                     average_price=D(o.get("avgPrice")) or None, reduce_only=o.get("reduceOnly"),
                     created_ms=_int(o.get("createdTime")), updated_ms=_int(o.get("updatedTime")))

    # ---- history ----
    def order_history(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Order]:
        rows = self._pages(cred, "/v5/order/history", {"category": CATEGORY[market], "symbol": symbol}, limit, 50)
        return [self._order(o, market) for o in rows]

    def fills(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Fill]:
        rows = self._pages(cred, "/v5/execution/list", {"category": CATEGORY[market], "symbol": symbol}, limit, 100)
        return [Fill(trade_id=str(t["execId"]), order_id=str(t["orderId"]), symbol=t["symbol"], market=market,
                     side=str(t["side"]).lower(), price=D(t["execPrice"]), quantity=D(t["execQty"]),
                     fee=D(t.get("execFee")), fee_asset=t.get("feeCurrency") or None, is_maker=t.get("isMaker"),
                     time_ms=int(t["execTime"]))
                for t in rows if t.get("execType") != "Funding"]  # funding is a cash flow, not a trade

    def cashflows(self, cred: Credential, *, limit: int = 100) -> list[Cashflow]:
        end = self.now_ms() + self._offset_ms()
        out: list[Cashflow] = []
        for r in self._pages(cred, "/v5/account/transaction-log",
                             {"accountType": "UNIFIED", "startTime": end - WEEK_MS, "endTime": end}, limit, 50):
            out += _translog(r)
        if self.environment is Environment.LIVE:  # deposit/withdrawal records exist only for real accounts
            try:
                for r in self._pages(cred, "/v5/asset/deposit/query-record", {}, limit, 50, key="rows"):
                    out.append(Cashflow(kind=CashflowKind.DEPOSIT, asset=r.get("coin", ""), amount=D(r.get("amount")) or Decimal(0),
                                        time_ms=_int(r.get("successAt")) or 0, status=str(r.get("status")),
                                        reference=r.get("txID") or r.get("id") or None))
                for r in self._pages(cred, "/v5/asset/withdraw/query-record", {"withdrawType": 2}, limit, 50, key="rows"):
                    out.append(Cashflow(kind=CashflowKind.WITHDRAWAL, asset=r.get("coin", ""),
                                        amount=-(D(r.get("amount")) or Decimal(0)), time_ms=_int(r.get("createTime")) or 0,
                                        status=str(r.get("status")), reference=r.get("txID") or r.get("withdrawId") or None))
            except AuthError:
                pass  # e.g. withdrawal records are master-account only
        return sorted(out, key=lambda c: c.time_ms, reverse=True)[:limit]

    # ---- public symbol rules ----
    def symbol_rules(self, symbol: str, market: str) -> SymbolRules:
        rows = self._public("/v5/market/instruments-info", {"category": CATEGORY[market], "symbol": symbol}).get("list", [])
        s = next((x for x in rows if x.get("symbol") == symbol), None)
        if s is None:
            raise ExchangeError(f"Bybit does not list {symbol} on {market}")
        lot = s.get("lotSizeFilter") or {}
        if market == "spot":  # minOrderQty is no longer checked by Bybit; minOrderAmt is the rule
            step, min_qty, notional = D(lot.get("basePrecision")), None, D(lot.get("minOrderAmt"))
            maxes = [D(lot.get("maxLimitOrderQty")), D(lot.get("maxMarketOrderQty"))]
        else:
            step, min_qty, notional = D(lot.get("qtyStep")), D(lot.get("minOrderQty")), D(lot.get("minNotionalValue"))
            maxes = [D(lot.get("maxOrderQty")), D(lot.get("maxMktOrderQty"))]
        maxes = [m for m in maxes if m]
        return SymbolRules(symbol=symbol, market=market, base_asset=s.get("baseCoin", ""), quote_asset=s.get("quoteCoin", ""),
                           status=s.get("status", "UNKNOWN"), price_tick=D((s.get("priceFilter") or {}).get("tickSize")),
                           quantity_step=step, min_quantity=min_qty,
                           max_quantity=min(maxes) if maxes else None,  # one limit for both types: the stricter one
                           min_notional=notional, order_types=("limit", "market"))

    def reference_price(self, symbol: str, market: str) -> Decimal:
        rows = self._public("/v5/market/tickers", {"category": CATEGORY[market], "symbol": symbol}).get("list", [])
        price = next((D(t.get("lastPrice")) for t in rows if t.get("symbol") == symbol), None)
        if not price:
            raise ExchangeError(f"Bybit returned no price for {symbol}")
        return price

    def fee_rates(self, cred: Credential, symbol: str, market: str) -> tuple[Decimal, Decimal] | None:
        rows = self._signed(cred, "GET", "/v5/account/fee-rate", {"category": CATEGORY[market], "symbol": symbol}).get("list", [])
        r = next((x for x in rows if x.get("symbol") == symbol), None) or {}
        maker, taker = D(r.get("makerFeeRate")), D(r.get("takerFeeRate"))
        return (maker, taker) if maker is not None and taker is not None else None

    def leverage(self, cred: Credential, symbol: str, market: str) -> Decimal | None:
        if market != "usdm_perpetual":
            return None
        rows = self._signed(cred, "GET", "/v5/position/list", {"category": "linear", "symbol": symbol}).get("list", [])
        return next((D(p.get("leverage")) for p in rows if p.get("symbol") == symbol), None)

    # ---- testnet-only order management ----
    def place_order(self, cred: Credential, order: OrderRequest) -> Order:
        self._require_testnet()
        body: dict[str, Any] = {"category": CATEGORY[order.market], "symbol": order.symbol, "side": order.side.capitalize(),
                                "orderType": order.type.capitalize(), "qty": plain(order.quantity),
                                "orderLinkId": order.client_order_id}
        if order.type == "limit":
            body["price"] = plain(order.price)
            body["timeInForce"] = order.time_in_force or "GTC"
        elif order.market == "spot":
            body["marketUnit"] = "baseCoin"  # spot market orders are by quote value unless told otherwise
        if order.market == "usdm_perpetual" and order.reduce_only:
            body["reduceOnly"] = True
        ack = self._signed(cred, "POST", "/v5/order/create", body, order_write=True)
        found = self._lookup_after_ack(cred, order.symbol, order.market, order.client_order_id)
        if found is not None:
            return found
        # Accepted, status not reported yet: UNKNOWN makes the journal reconcile it instead of trusting the ack.
        return Order(order_id=str(ack.get("orderId", "")), client_order_id=order.client_order_id, symbol=order.symbol,
                     market=order.market, side=order.side, type=order.type, status=OrderStatus.UNKNOWN,
                     price=order.price, quantity=order.quantity, reduce_only=order.reduce_only,
                     provenance=Provenance.DERIVED)

    def query_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order | None:
        self._require_testnet()
        params = {"category": CATEGORY[market], "symbol": symbol, "orderLinkId": client_order_id}
        for path in ("/v5/order/realtime", "/v5/order/history"):  # realtime also holds the 500 latest closed orders
            rows = [o for o in self._signed(cred, "GET", path, params).get("list", []) if o.get("orderLinkId") == client_order_id]
            if rows:
                return self._order(rows[0], market)
        return None

    def cancel_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str,
                     exchange_order_id: str | None = None) -> Order:
        self._require_testnet()
        self._signed(cred, "POST", "/v5/order/cancel",
                     {"category": CATEGORY[market], "symbol": symbol, "orderLinkId": client_order_id}, order_write=True)
        found = self._lookup_after_ack(cred, symbol, market, client_order_id)
        if found is None:
            raise OutcomeUnknown("Bybit acknowledged the cancel but has not reported the order's status yet")
        return found

    def _lookup_after_ack(self, cred: Credential, symbol: str, market: str, client_order_id: str) -> Order | None:
        """Bybit's acknowledgement is not a status, so look the order up once."""
        try:
            return self.query_order(cred, symbol, market, client_order_id=client_order_id)
        except ExchangeError:
            return None


def _translog(r: dict[str, Any]) -> list[Cashflow]:
    """One transaction-log row -> cash flows. Signs follow the docs: funding > 0 received, fee > 0 paid."""
    base = {"asset": r.get("currency", ""), "time_ms": _int(r.get("transactionTime")) or 0,
            "reference": r.get("tradeId") or r.get("id") or None, "symbol": r.get("symbol") or None}
    kind = r.get("type")
    out: list[tuple[CashflowKind, Decimal | None]] = []
    if kind == "SETTLEMENT":
        out.append((CashflowKind.FUNDING_FEE, D(r.get("funding"))))
    elif kind == "TRADE":
        fee = D(r.get("fee"))
        out.append((CashflowKind.COMMISSION, -fee if fee is not None else None))
        if r.get("category") in ("linear", "inverse"):  # cashFlow on a contract trade is the realised PnL
            out.append((CashflowKind.REALIZED_PNL, D(r.get("cashFlow"))))
    elif kind in ("TRANSFER_IN", "TRANSFER_OUT"):
        out.append((CashflowKind.INTERNAL_TRANSFER, D(r.get("change"))))
    else:
        out.append((CashflowKind.OTHER, D(r.get("change"))))
    return [Cashflow(kind=k, amount=a, **base) for k, a in out if a]


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iso_ms(value: Any) -> int | None:
    """query-api dates are ISO-8601 UTC; the epoch ("1970-01-01T00:00:00Z") means no expiry."""
    try:
        ms = int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
    return ms if ms > 0 else None
