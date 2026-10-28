"""Binance spot + USD-M futures. Endpoints and fields follow the official API docs and the official
binance-connector-js types (checked 2026-10-27):

- Live: spot https://api.binance.com, USD-M futures https://fapi.binance.com, key permissions
  GET /sapi/v1/account/apiRestrictions (live only).
- Testnet: spot https://testnet.binance.vision (keys from that site); USD-M futures demo trading
  https://demo-fapi.binance.com (separate keys from demo.binance.com). A testnet connection works with
  whichever of the two the key belongs to; the other is reported as unavailable.
- Signing: HMAC-SHA256 of the query string, key in X-MBX-APIKEY, timestamp + recvWindow.
- HTTP 5xx and error -1007 on an order mean "execution status unknown" (Binance docs): reported as
  OutcomeUnknown so the order is reconciled, never resent.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
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
    STATUS_MAP, AuthError, ExchangeAdapter, ExchangeError, ExchangeInfo, ExchangeUnavailable, HttpResponse,
    NotSupported, OrderRequest,
    OutcomeUnknown, SymbolRules, decimal_or_none as D, plain, register,
)

HOSTS = {
    Environment.LIVE: {"spot": "https://api.binance.com", "usdm_perpetual": "https://fapi.binance.com"},
    Environment.TESTNET: {"spot": "https://testnet.binance.vision", "usdm_perpetual": "https://demo-fapi.binance.com"},
}
PATHS = {
    "spot": {"time": "/api/v3/time", "account": "/api/v3/account", "open": "/api/v3/openOrders",
             "all": "/api/v3/allOrders", "trades": "/api/v3/myTrades", "order": "/api/v3/order",
             "info": "/api/v3/exchangeInfo", "price": "/api/v3/ticker/price", "fees": "/api/v3/account/commission"},
    "usdm_perpetual": {"time": "/fapi/v1/time", "account": "/fapi/v3/account", "positions": "/fapi/v3/positionRisk",
                       "config": "/fapi/v1/symbolConfig", "open": "/fapi/v1/openOrders", "all": "/fapi/v1/allOrders",
                       "trades": "/fapi/v1/userTrades", "order": "/fapi/v1/order", "income": "/fapi/v1/income",
                       "info": "/fapi/v1/exchangeInfo", "price": "/fapi/v1/ticker/price",
                       "fees": "/fapi/v1/commissionRate"},
}
MARKETS = ("spot", "usdm_perpetual")
RECV_WINDOW_MS = 5000
TIMEOUT_S = 10.0
INFO_TTL_S = 600.0
MARGIN_MODES = {"CROSSED": "cross", "ISOLATED": "isolated"}
INCOME_KINDS = {"FUNDING_FEE": CashflowKind.FUNDING_FEE, "COMMISSION": CashflowKind.COMMISSION,
                "REALIZED_PNL": CashflowKind.REALIZED_PNL, "TRANSFER": CashflowKind.INTERNAL_TRANSFER}

_offsets: dict[str, tuple[float, int]] = {}  # base URL -> (fetched at, server - local ms)
_info_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


@register
class Binance(ExchangeAdapter):
    INFO = ExchangeInfo(
        id="binance", name="Binance", kind="crypto_exchange", environments=(Environment.LIVE, Environment.TESTNET),
        needs_passphrase=False, markets=MARKETS, key_setup_url="https://www.binance.com/en/my/settings/api-management",
        notes=("Live: create a key with only 'Enable Reading' on. Keys that can trade, withdraw or transfer are refused.",
               "Testnet spot keys come from testnet.binance.vision; USD-M futures demo keys come from demo.binance.com. "
               "Connect each key as its own connection.",
               "Restricting the key to your IP address at Binance is recommended."))

    # ---- transport ----
    def _base(self, market: str) -> str:
        return HOSTS[self.environment][market]

    def _offset_ms(self, market: str) -> int:
        base = self._base(market)
        with _cache_lock:
            hit = _offsets.get(base)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        before = self.now_ms()
        data = self.check(self.transport("GET", base + PATHS[market]["time"], {}, None, TIMEOUT_S))
        after = self.now_ms()
        offset = int(data["serverTime"]) - (before + after) // 2 if isinstance(data, dict) and "serverTime" in data else 0
        with _cache_lock:
            _offsets[base] = (time.monotonic(), offset)
        return offset

    def _signed(self, cred: Credential, method: str, market: str, path: str, params: dict[str, Any] | None = None,
                *, base: str | None = None, order_write: bool = False) -> Any:
        query = dict(params or {})
        query["timestamp"] = self.now_ms() + self._offset_ms(market)
        query["recvWindow"] = RECV_WINDOW_MS
        qs = urlencode(query)
        sig = hmac.new(cred.api_secret.reveal().encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{base or self._base(market)}{path}?{qs}&signature={sig}"
        resp = self.transport(method, url, {"X-MBX-APIKEY": cred.api_key.reveal()}, None, TIMEOUT_S)
        if order_write:
            self._check_order_write(resp)
        return self.check(resp)

    def _public(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        qs = f"?{urlencode(params)}" if params else ""
        return self.check(self.transport("GET", f"{self._base(market)}{path}{qs}", {}, None, TIMEOUT_S))

    def _check_order_write(self, resp: HttpResponse) -> None:
        # Binance docs: HTTP 5xx, HTTP 408 and codes -1006/-1007 all mean "execution status unknown".
        if resp.status >= 500 or resp.status == 408 or 300 <= resp.status < 400:
            raise OutcomeUnknown("Binance answered HTTP %d: the order's execution status is unknown" % resp.status)
        if resp.status >= 400:
            try:
                data = resp.json() if resp.body else {}
            except ExchangeError:
                data = {}
            if isinstance(data, dict) and data.get("code") in (-1006, -1007):
                raise OutcomeUnknown(f"Binance reported code {data['code']}: the order's execution status is unknown")

    def _require_testnet(self) -> None:
        if self.environment is not Environment.TESTNET:
            raise NotSupported("live order endpoints are disabled in this app (AGENTS.md §2); use a testnet connection")

    # ---- permissions ----
    def permissions(self, cred: Credential) -> KeyPermissions:
        if self.environment is Environment.LIVE:
            r = self._signed(cred, "GET", "spot", "/sapi/v1/account/apiRestrictions")
            if not isinstance(r, dict):
                raise ExchangeUnavailable("Binance returned an unexpected key-permission response")
            expires = r.get("tradingAuthorityExpirationTime")
            return KeyPermissions(read=_flag(r, "enableReading"),
                                  spot_trade=_any_flag(r, "enableSpotAndMarginTrading", "enableFixApiTrade"),
                                  derivatives_trade=_any_flag(r, "enableFutures", "enableVanillaOptions",
                                                              "enablePortfolioMarginTrading"),
                                  margin_trade=_any_flag(r, "enableMargin", "enableSpotAndMarginTrading"),
                                  withdraw=_flag(r, "enableWithdrawals"),
                                  transfer=_any_flag(r, "enableInternalTransfer", "permitsUniversalTransfer"),
                                  ip_restricted=_flag(r, "ipRestrict"),
                                  expires_ms=int(expires) if isinstance(expires, int) and expires > 0 else None,
                                  source="binance apiRestrictions")
        # Testnet has no /sapi: prove the key works on at least one market and read the account-level trade flag.
        works: dict[str, bool] = {}
        for market in MARKETS:
            try:
                acc = self._signed(cred, "GET", market, PATHS[market]["account"])
                works[market] = bool(acc.get("canTrade", True)) if isinstance(acc, dict) else True
            except AuthError:
                continue
        if not works:
            raise AuthError("Binance testnet rejected the key on both spot (testnet.binance.vision) and USD-M futures "
                            "demo (demo-fapi.binance.com)")
        return KeyPermissions(read=True, spot_trade=works.get("spot", False), derivatives_trade=works.get("usdm_perpetual", False),
                              margin_trade=False, withdraw=False, transfer=False, source="binance testnet account")

    # ---- account snapshot ----
    def snapshot(self, cred: Credential, connection_id: str) -> AccountSnapshot:
        totals: list[AccountTotals] = []
        balances: list[Balance] = []
        positions: list[Position] = []
        orders: list[Order] = []
        unavailable: list[str] = []
        for market in MARKETS:
            try:
                if market == "spot":
                    acc = self._signed(cred, "GET", "spot", PATHS["spot"]["account"], {"omitZeroBalances": "true"})
                    balances += [Balance(asset=b["asset"], wallet=WalletType.SPOT, free=D(b.get("free")),
                                         locked=D(b.get("locked")))
                                 for b in acc.get("balances", []) if (D(b.get("free")) or 0) or (D(b.get("locked")) or 0)]
                else:
                    acc = self._signed(cred, "GET", market, PATHS[market]["account"])
                    totals.append(AccountTotals(
                        wallet=WalletType.FUTURES_USDM, equity=D(acc.get("totalMarginBalance")),
                        wallet_balance=D(acc.get("totalWalletBalance")), available_balance=D(acc.get("availableBalance")),
                        initial_margin=D(acc.get("totalInitialMargin")), maintenance_margin=D(acc.get("totalMaintMargin")),
                        unrealized_pnl=D(acc.get("totalUnrealizedProfit")), quote_asset="USD"))
                    balances += [Balance(asset=a["asset"], wallet=WalletType.FUTURES_USDM, free=D(a.get("availableBalance")),
                                         total=D(a.get("walletBalance")))
                                 for a in acc.get("assets", []) if D(a.get("walletBalance"))]
                    positions += self._futures_positions(cred)
                orders += [self._order(o, market) for o in self._signed(cred, "GET", market, PATHS[market]["open"])]
            except AuthError:
                unavailable.append(f"{market}: this key does not work on {self._base(market)}")
            except NotSupported as exc:
                unavailable.append(f"{market}: {exc}")
        if len(unavailable) == len(MARKETS):
            raise AuthError("Binance rejected the key on every market")
        return AccountSnapshot(connection_id=connection_id, exchange="binance", environment=self.environment,
                               as_of_ms=self.now_ms(), totals=totals, balances=balances, positions=positions,
                               open_orders=orders, unavailable=unavailable)

    def _futures_positions(self, cred: Credential) -> list[Position]:
        rows = self._signed(cred, "GET", "usdm_perpetual", PATHS["usdm_perpetual"]["positions"])
        config = {c["symbol"]: c for c in self._signed(cred, "GET", "usdm_perpetual", PATHS["usdm_perpetual"]["config"])
                  if isinstance(c, dict) and "symbol" in c}
        out = []
        for p in rows:
            amount = D(p.get("positionAmt")) or Decimal(0)
            if amount == 0:
                continue
            side_field = p.get("positionSide", "BOTH")
            side = "long" if side_field == "LONG" or (side_field == "BOTH" and amount > 0) else "short"
            cfg = config.get(p.get("symbol"), {})
            margin = MARGIN_MODES.get(str(cfg.get("marginType", "")).upper())  # symbolConfig: CROSSED / ISOLATED
            liq = D(p.get("liquidationPrice"))
            out.append(Position(
                symbol=p["symbol"], market="usdm_perpetual", side=side, quantity=abs(amount),
                entry_price=D(p.get("entryPrice")), mark_price=D(p.get("markPrice")),
                leverage=D(cfg.get("leverage")), margin_mode=margin,
                liquidation_price=liq if liq else None,  # Binance reports "0" when no liquidation price applies
                unrealized_pnl=D(p.get("unRealizedProfit")), notional=D(p.get("notional"))))
        return out

    def _order(self, o: dict[str, Any], market: str) -> Order:
        status = STATUS_MAP.get(str(o.get("status")), OrderStatus.UNKNOWN)
        executed = D(o.get("executedQty"))
        avg = D(o.get("avgPrice"))
        if avg is None and executed:
            quote = D(o.get("cummulativeQuoteQty"))
            avg = (quote / executed) if quote is not None else None
        return Order(order_id=str(o.get("orderId")), client_order_id=o.get("clientOrderId"), symbol=o["symbol"],
                     market=market, side=str(o.get("side", "")).lower(), type=str(o.get("type", "")).lower(),
                     status=status, price=D(o.get("price")) or None, stop_price=D(o.get("stopPrice")) or None,
                     quantity=D(o.get("origQty")), filled_quantity=executed, average_price=avg or None,
                     reduce_only=o.get("reduceOnly"), created_ms=o.get("time") or o.get("transactTime"),
                     updated_ms=o.get("updateTime") or o.get("workingTime"))

    # ---- history ----
    def order_history(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Order]:
        rows = self._signed(cred, "GET", market, PATHS[market]["all"], {"symbol": symbol, "limit": min(limit, 1000)})
        return [self._order(o, market) for o in rows]

    def fills(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Fill]:
        rows = self._signed(cred, "GET", market, PATHS[market]["trades"], {"symbol": symbol, "limit": min(limit, 1000)})
        out = []
        for t in rows:
            buyer = t.get("isBuyer", t.get("buyer"))
            out.append(Fill(trade_id=str(t["id"]), order_id=str(t["orderId"]), symbol=t["symbol"], market=market,
                            side="buy" if buyer else "sell", price=D(t["price"]), quantity=D(t["qty"]),
                            fee=D(t.get("commission")), fee_asset=t.get("commissionAsset"),
                            realized_pnl=D(t.get("realizedPnl")), is_maker=t.get("isMaker", t.get("maker")),
                            time_ms=int(t["time"])))
        return out

    def cashflows(self, cred: Credential, *, limit: int = 100) -> list[Cashflow]:
        out: list[Cashflow] = []
        try:
            for r in self._signed(cred, "GET", "usdm_perpetual", PATHS["usdm_perpetual"]["income"], {"limit": min(limit, 1000)}):
                out.append(Cashflow(kind=INCOME_KINDS.get(r.get("incomeType"), CashflowKind.OTHER), asset=r.get("asset", ""),
                                    amount=D(r.get("income")) or Decimal(0), time_ms=int(r.get("time", 0)),
                                    reference=str(r.get("tranId", "")) or None, symbol=r.get("symbol") or None))
        except AuthError:
            pass  # key not valid for futures: no futures income
        if self.environment is Environment.LIVE:  # deposit/withdrawal history exists only on live /sapi
            for r in self._signed(cred, "GET", "spot", "/sapi/v1/capital/deposit/hisrec", {"limit": min(limit, 1000)}):
                out.append(Cashflow(kind=CashflowKind.DEPOSIT, asset=r.get("coin", ""), amount=D(r.get("amount")) or Decimal(0),
                                    time_ms=int(r.get("insertTime", 0)), status=str(r.get("status")), reference=r.get("txId")))
            for r in self._signed(cred, "GET", "spot", "/sapi/v1/capital/withdraw/history", {"limit": min(limit, 1000)}):
                out.append(Cashflow(kind=CashflowKind.WITHDRAWAL, asset=r.get("coin", ""),
                                    amount=-(D(r.get("amount")) or Decimal(0)), time_ms=_withdraw_time(r.get("applyTime")),
                                    status=str(r.get("status")), reference=r.get("txId")))
        return sorted(out, key=lambda c: c.time_ms, reverse=True)[:limit]

    # ---- public symbol rules ----
    def symbol_rules(self, symbol: str, market: str) -> SymbolRules:
        key = f"{self._base(market)}|{market}"
        with _cache_lock:
            hit = _info_cache.get(key)
        if hit is None or time.monotonic() - hit[0] > INFO_TTL_S:
            info = self._public(market, PATHS[market]["info"])
            with _cache_lock:
                _info_cache[key] = hit = (time.monotonic(), info)
        for s in hit[1].get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {x.get("filterType"): x for x in s.get("filters", [])}
            notional = f.get("NOTIONAL", {}).get("minNotional") or f.get("MIN_NOTIONAL", {}).get("minNotional") \
                or f.get("MIN_NOTIONAL", {}).get("notional")
            return SymbolRules(symbol=symbol, market=market, base_asset=s.get("baseAsset", ""),
                               quote_asset=s.get("quoteAsset", ""), status=s.get("status", "UNKNOWN"),
                               price_tick=D(f.get("PRICE_FILTER", {}).get("tickSize")),
                               quantity_step=D(f.get("LOT_SIZE", {}).get("stepSize")),
                               min_quantity=D(f.get("LOT_SIZE", {}).get("minQty")),
                               max_quantity=D(f.get("LOT_SIZE", {}).get("maxQty")), min_notional=D(notional),
                               order_types=tuple(str(t).lower() for t in s.get("orderTypes", [])))
        raise ExchangeError(f"Binance does not list {symbol} on {market}")

    def reference_price(self, symbol: str, market: str) -> Decimal:
        price = D(self._public(market, PATHS[market]["price"], {"symbol": symbol}).get("price"))
        if not price:
            raise ExchangeError(f"Binance returned no price for {symbol}")
        return price

    def fee_rates(self, cred: Credential, symbol: str, market: str) -> tuple[Decimal, Decimal] | None:
        r = self._signed(cred, "GET", market, PATHS[market]["fees"], {"symbol": symbol})
        if market == "spot":
            std = r.get("standardCommission") or {}
            maker, taker = D(std.get("maker")), D(std.get("taker"))
        else:
            maker, taker = D(r.get("makerCommissionRate")), D(r.get("takerCommissionRate"))
        return (maker, taker) if maker is not None and taker is not None else None

    def leverage(self, cred: Credential, symbol: str, market: str) -> Decimal | None:
        if market != "usdm_perpetual":
            return None
        rows = self._signed(cred, "GET", market, PATHS[market]["config"], {"symbol": symbol})
        return next((D(r.get("leverage")) for r in rows if isinstance(r, dict) and r.get("symbol") == symbol), None)

    # ---- testnet-only order management ----
    def place_order(self, cred: Credential, order: OrderRequest) -> Order:
        self._require_testnet()
        params: dict[str, Any] = {"symbol": order.symbol, "side": order.side.upper(), "type": order.type.upper(),
                                  "quantity": plain(order.quantity), "newClientOrderId": order.client_order_id,
                                  "newOrderRespType": "RESULT"}
        if order.type == "limit":
            params["price"] = plain(order.price)
            params["timeInForce"] = order.time_in_force or "GTC"
        if order.market == "usdm_perpetual" and order.reduce_only:
            params["reduceOnly"] = "true"
        data = self._signed(cred, "POST", order.market, PATHS[order.market]["order"], params, order_write=True)
        return self._order(data, order.market)

    def query_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order | None:
        try:
            data = self._signed(cred, "GET", market, PATHS[market]["order"],
                                {"symbol": symbol, "origClientOrderId": client_order_id})
        except ExchangeError as exc:
            if "-2013" in str(exc):  # order does not exist
                return None
            raise
        return self._order(data, market)

    def cancel_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str,
                     exchange_order_id: str | None = None) -> Order:
        self._require_testnet()
        data = self._signed(cred, "DELETE", market, PATHS[market]["order"],
                            {"symbol": symbol, "origClientOrderId": client_order_id}, order_write=True)
        return self._order(data, market)


def _withdraw_time(value: Any) -> int:
    """Binance returns withdrawal applyTime as 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%d %H:%M:%S")) * 1000
    except (ValueError, OverflowError):
        return 0


def _flag(r: dict[str, Any], key: str) -> bool | None:
    """A permission flag as reported; a missing or non-boolean field is unknown (None), never "off"."""
    v = r.get(key)
    return v if isinstance(v, bool) else None


def _any_flag(r: dict[str, Any], *keys: str) -> bool | None:
    flags = [_flag(r, k) for k in keys]
    if any(f is True for f in flags):
        return True
    return False if all(f is False for f in flags) else None
