"""Interactive Brokers PAPER accounts through IBKR's Client Portal Gateway (Web API v1). Endpoints and fields follow
the official Web API docs (https://www.interactivebrokers.com/docs/web-api/, checked 2026-10-27).

How the gateway works and what it means for this adapter:
- The user downloads and runs IBKR's Java Client Portal Gateway on this machine and logs in through a browser at
  https://localhost:5000 (IBKR does not support automating that login; it must be repeated daily). The gateway then
  proxies requests; there is no API key or secret.
- IBKR has no read-only key or session-permission endpoint. Its "read-only session" is only the non-/iserver session
  tier, not a restriction on trading. A live login therefore cannot be confirmed read-only, so LIVE is not offered at
  all (`environments=(TESTNET,)`): TESTNET here means an IBKR paper account.
- Paper is detected from the gateway itself, never from what the user typed: `GET /iserver/accounts` -> `isPaper`
  must be true and must list the account, and `GET /sso/validate` must report the same username (and, when present,
  `LOGIN_TYPE` 2 = paper). This is re-checked before every snapshot and every order request, because the one gateway
  session can be logged out and logged back in with a live username at any time.

Credential fields (the existing Credential type, no interface change):
- `api_key`    = the IBKR paper account number (e.g. DU1234567, "Paper Trading Account Number" in Client Portal).
- `api_secret` = the IBKR paper username used to log in to the gateway ("Paper Trading Username").
Neither is secret; IBKR holds the password. They bind the connection to one paper login so a gateway that is now
logged into another (or a live) user is refused.

Host and TLS: the gateway URL is fixed to https://localhost:5000/v1/api and never comes from user input. The gateway
ships a self-signed certificate, so certificate verification is disabled for this one loopback URL only, through an
SSL context private to this module. Risk: any local process listening on port 5000 could impersonate the gateway.
Traffic between the gateway and IBKR stays verified TLS.

Orders (paper only): `cOID` is our client order ID (unique for 24 h, <= 64 chars) and is reported back as
`order_ref`, so unknown outcomes are reconciled by looking it up. IBKR may answer an order with a confirmation
question ("order reply message", e.g. price-constraint warnings). This adapter NEVER answers it
(`/iserver/reply/{id}` is never called): the question is raised as an ExchangeError with its text, and per IBKR the
order does not work until confirmed. A future UI step could show the question and, on an explicit user click, send
`POST /iserver/reply/{id} {"confirmed": true}` immediately (IBKR rejects late replies).
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from decimal import Decimal
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from ..credentials import Credential
from ..models import (
    AccountSnapshot, AccountTotals, Balance, Environment, Fill, KeyPermissions, Order, OrderStatus, Position,
    WalletType,
)
from .base import (
    MAX_RESPONSE_BYTES, USER_AGENT, AuthError, ExchangeAdapter, ExchangeError, ExchangeInfo, ExchangeUnavailable,
    HttpResponse, NotSupported, OrderRequest, OutcomeUnknown, SymbolRules, Transport, _NoRedirect,
    decimal_or_none as D, register,
)

BASE_URL = "https://localhost:5000/v1/api"  # fixed loopback gateway; never user-configurable
MARKETS = ("stock",)
TIMEOUT_S = 15.0
CONID_TTL_S = 3600.0
MAX_POSITION_PAGES = 20
ACCOUNT_ID = re.compile(r"^[A-Z]{1,4}[0-9]{4,10}$")
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PAPER_LOGIN_TYPE = 2  # /sso/validate: 1 = live, 2 = paper

# IBKR order_status -> normalized. PendingSubmit (sent, not yet acknowledged), Inactive (rejected OR not active yet)
# and WarnState are ambiguous, so they stay UNKNOWN and must be reconciled.
STATUS = {"PreSubmitted": OrderStatus.NEW, "Submitted": OrderStatus.NEW, "Filled": OrderStatus.FILLED,
          "PendingCancel": OrderStatus.PENDING_CANCEL, "Cancelled": OrderStatus.CANCELED}
WORKING = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
POSITION_MARKETS = {"STK": "stock", "OPT": "option", "FUT": "futures"}
ORDER_TYPES = {"market": "MKT", "limit": "LMT"}
TIFS = {"GTC": "GTC", "IOC": "IOC"}  # IBKR has no FOK for these orders; DAY is used when none is given

_conids: dict[str, tuple[float, int]] = {}
_cache_lock = threading.Lock()


def _gateway_ssl_context() -> ssl.SSLContext:
    """Unverified TLS for the fixed loopback gateway only (it ships a self-signed certificate)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_GATEWAY_OPENER = build_opener(_NoRedirect, ProxyHandler({}), HTTPSHandler(context=_gateway_ssl_context()))


def gateway_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                      timeout_s: float) -> HttpResponse:
    """Same contract as base.urllib_transport, but refuses any URL outside the fixed gateway."""
    if not url.startswith(BASE_URL + "/"):
        raise NotSupported("the IBKR adapter only talks to the local Client Portal Gateway")
    req = Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **headers})
    sent = False
    try:
        with _GATEWAY_OPENER.open(req, timeout=timeout_s) as resp:
            sent = True
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ExchangeUnavailable("gateway response too large")
            return HttpResponse(resp.status, data, {k.lower(): v for k, v in resp.headers.items()})
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.read(65_536) if exc.fp else b"",
                            {k.lower(): v for k, v in (exc.headers or {}).items()})
    except TimeoutError as exc:
        raise OutcomeUnknown("the IBKR gateway did not answer in time; the request may or may not have taken effect") from exc
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if sent or isinstance(reason, TimeoutError):
            raise OutcomeUnknown("connection to the IBKR gateway lost after sending; the request may or may not "
                                 "have taken effect") from exc
        raise ExchangeUnavailable("could not reach the IBKR Client Portal Gateway at https://localhost:5000; "
                                  "start it and log in with your paper username") from exc


@register
class IBKR(ExchangeAdapter):
    INFO = ExchangeInfo(
        id="ibkr", name="Interactive Brokers (paper)", kind="broker", environments=(Environment.TESTNET,),
        needs_passphrase=False, markets=MARKETS, oauth="not_offered",
        key_label="Paper account number", secret_label="Paper username",
        key_setup_url="https://www.interactivebrokers.com/docs/web-api/authentication/paper",
        notes=("Paper accounts only. Run IBKR's Client Portal Gateway on this computer and log in at "
               "https://localhost:5000 with your PAPER username (Client Portal > Settings > Paper Trading Account).",
               "Enter your paper account number (e.g. DU1234567) as the key and your paper username as the secret. "
               "Neither is a password; the app checks both against the gateway.",
               "Live IBKR accounts are not supported: IBKR offers no way to confirm a session is read-only.",
               "The gateway uses a self-signed certificate; the app accepts it only for https://localhost:5000.",
               "IBKR may ask to confirm an order (e.g. a price warning). The app never confirms for you; "
               "such orders are not placed."))

    def __init__(self, environment: Environment, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(environment, transport=transport or gateway_transport, clock=clock)

    # ---- transport ----
    def _call(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> HttpResponse:
        url = BASE_URL + path + (f"?{urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        return self.transport(method, url, headers, data, TIMEOUT_S)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.check(self._call("GET", path, params))

    def _require_testnet(self) -> None:
        if self.environment is not Environment.TESTNET:
            raise NotSupported("IBKR orders are available only on paper accounts in this app (AGENTS.md §2)")

    # ---- paper-session verification ----
    @staticmethod
    def _ids(cred: Credential) -> tuple[str, str]:
        account, username = cred.api_key.reveal().strip().upper(), cred.api_secret.reveal().strip()
        if not ACCOUNT_ID.match(account):
            raise AuthError("enter your IBKR paper account number (e.g. DU1234567) as the key")
        if not USERNAME.match(username):
            raise AuthError("enter your IBKR paper username as the secret")
        return account, username

    def _paper_session(self, cred: Credential) -> tuple[str, dict[str, Any]]:
        """Proves the gateway is logged in with this paper username and can trade this paper account."""
        account, username = self._ids(cred)
        sso = self._get("/sso/validate")
        if not isinstance(sso, dict) or sso.get("RESULT") is False:
            raise AuthError("the IBKR gateway session is not valid; log in again at https://localhost:5000")
        login_type = sso.get("LOGIN_TYPE", sso.get("loginType"))
        if login_type is not None and login_type != PAPER_LOGIN_TYPE:
            raise AuthError("the IBKR gateway is logged in with a LIVE username; this app supports paper accounts "
                            "only. Log out and log in with your paper username.")
        if str(sso.get("USER_NAME", "")).lower() != username.lower():
            raise AuthError("the IBKR gateway is logged in with a different username than this connection")
        accs = self._get("/iserver/accounts")
        if not isinstance(accs, dict) or not accs.get("accounts"):
            raise AuthError("the IBKR brokerage session is not ready; log in again at https://localhost:5000")
        if accs.get("isPaper") is not True:
            raise AuthError("IBKR does not report this gateway session as paper trading; live accounts are not "
                            "supported in this app")
        if account not in accs.get("accounts", []):
            raise AuthError("this paper account is not tradable by the logged-in IBKR username")
        return account, accs

    def _portfolio_account(self, account: str) -> dict[str, Any]:
        rows = self._get("/portfolio/accounts")  # IBKR: must be called before other /portfolio endpoints
        row = next((r for r in rows or [] if isinstance(r, dict) and r.get("accountId", r.get("id")) == account), None)
        if row is None:
            raise AuthError("IBKR does not list this account for the logged-in username")
        return row

    # ---- permissions ----
    def permissions(self, cred: Credential) -> KeyPermissions:
        if self.environment is not Environment.TESTNET:
            # Unreachable while INFO offers only TESTNET; kept so a future LIVE entry is refused by check_key_policy.
            return KeyPermissions(read=None, spot_trade=None, derivatives_trade=None, margin_trade=None,
                                  withdraw=None, transfer=None, source="ibkr: no read-only confirmation exists")
        status = self.check(self._call("POST", "/iserver/auth/status"))
        if not isinstance(status, dict) or status.get("authenticated") is not True:
            raise AuthError("the IBKR gateway is not logged in; open https://localhost:5000 and log in with your "
                            "paper username")
        account, _ = self._paper_session(cred)
        row = self._portfolio_account(account)
        # Listed in /iserver/accounts = the username has trading access; noClientTrading can still forbid it.
        trade = row.get("noClientTrading") is not True
        return KeyPermissions(read=True, spot_trade=trade, derivatives_trade=None, margin_trade=None,
                              withdraw=None, transfer=None, source="ibkr paper session (/iserver/accounts isPaper)")

    # ---- account snapshot ----
    def snapshot(self, cred: Credential, connection_id: str) -> AccountSnapshot:
        account, _ = self._paper_session(cred)
        row = self._portfolio_account(account)
        base_ccy = str(row.get("currency") or "")
        warnings: list[str] = []
        unavailable: list[str] = []
        ledger = self._get(f"/portfolio/{account}/ledger")
        ledger = ledger if isinstance(ledger, dict) else {}
        summary = self._get(f"/portfolio/{account}/summary")
        summary = summary if isinstance(summary, dict) else {}
        base = ledger.get("BASE", {}) if isinstance(ledger.get("BASE"), dict) else {}
        totals = [AccountTotals(
            wallet=WalletType.SECURITIES, equity=_amount(summary, "netliquidation"),
            wallet_balance=_amount(summary, "totalcashvalue"), available_balance=_amount(summary, "availablefunds"),
            initial_margin=_amount(summary, "initmarginreq"), maintenance_margin=_amount(summary, "maintmarginreq"),
            unrealized_pnl=D(base.get("unrealizedpnl")), quote_asset=base_ccy or "BASE")]
        if not base_ccy:
            warnings.append("IBKR did not report the account base currency; totals are in the account base currency")
        balances = [Balance(asset=ccy, wallet=WalletType.SECURITIES, total=D(v.get("cashbalance")))
                    for ccy, v in ledger.items() if ccy != "BASE" and isinstance(v, dict)]
        positions, skipped = self._positions(account)
        if skipped:
            warnings.append("positions in asset classes this app does not show yet: " + ", ".join(sorted(skipped)))
        orders = [self._order(o) for o in self._session_orders(account) if o.get("status") in WORKING]
        warnings.append("IBKR lists only orders that are open or were handled in the current gateway session")
        return AccountSnapshot(connection_id=connection_id, exchange="ibkr", environment=self.environment,
                               as_of_ms=self.now_ms(), totals=totals, balances=balances, positions=positions,
                               open_orders=orders, unavailable=unavailable, warnings=warnings)

    def _positions(self, account: str) -> tuple[list[Position], set[str]]:
        out: list[Position] = []
        skipped: set[str] = set()
        for page in range(MAX_POSITION_PAGES):
            rows = self._get(f"/portfolio/{account}/positions/{page}")
            rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            for p in rows:
                qty = D(p.get("position"))
                if not qty:
                    continue
                market = POSITION_MARKETS.get(str(p.get("assetClass", "")))
                if market is None:
                    skipped.add(str(p.get("assetClass") or "unknown"))
                    continue
                out.append(Position(
                    symbol=str(p.get("ticker") or p.get("contractDesc") or p.get("conid")), market=market,
                    side="long" if qty > 0 else "short", quantity=abs(qty), entry_price=D(p.get("avgPrice")),
                    mark_price=D(p.get("mktPrice")), unrealized_pnl=D(p.get("unrealizedPnl")),
                    notional=D(p.get("mktValue"))))
            page_size = int(rows[0].get("pageSize") or 100) if rows else 100
            if len(rows) < page_size:
                break
        return out, skipped

    # ---- orders and trades (current brokerage session) ----
    def _session_orders(self, account: str) -> list[dict[str, Any]]:
        data = self._get("/iserver/account/orders", {"accountId": account})
        rows = data.get("orders", []) if isinstance(data, dict) else []
        return [o for o in rows if isinstance(o, dict) and account in (o.get("acct"), o.get("account"))]

    def _order(self, o: dict[str, Any]) -> Order:
        filled, total = D(o.get("filledQuantity")), D(o.get("totalSize"))
        status = STATUS.get(str(o.get("status")), OrderStatus.UNKNOWN)
        if status is OrderStatus.NEW and filled:
            status = OrderStatus.PARTIALLY_FILLED
        updated = o.get("lastExecutionTime_r")
        return Order(order_id=str(o.get("orderId")), client_order_id=o.get("order_ref") or None,
                     symbol=str(o.get("ticker") or o.get("description1") or ""), market="stock",
                     side=str(o.get("side", "")).lower(), type=str(o.get("origOrderType") or o.get("orderType") or "").lower(),
                     status=status, price=D(o.get("price")), quantity=total, filled_quantity=filled,
                     average_price=D(o.get("avgPrice")),
                     updated_ms=int(updated) if str(updated or "").isdigit() else None)

    def order_history(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Order]:
        self._market(market)
        account, _ = self._paper_session(cred)
        return [self._order(o) for o in self._session_orders(account) if o.get("ticker") == symbol][:limit]

    def fills(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Fill]:
        self._market(market)
        account, _ = self._paper_session(cred)
        rows = self._get("/iserver/account/trades", {"days": 7})
        out = []
        for t in rows if isinstance(rows, list) else []:
            if not isinstance(t, dict) or t.get("symbol") != symbol or t.get("sec_type") not in (None, "STK"):
                continue
            if account not in (t.get("account"), t.get("accountCode")) or t.get("side") not in ("B", "S"):
                continue
            price, size = D(t.get("price")), D(t.get("size"))
            if price is None or size is None:
                continue
            out.append(Fill(trade_id=str(t.get("execution_id")), order_id=str(t.get("order_id")), symbol=symbol,
                            market="stock", side="buy" if t["side"] == "B" else "sell", price=price, quantity=size,
                            fee=D(t.get("commission")), time_ms=int(t.get("trade_time_r") or 0)))
        return sorted(out, key=lambda f: f.time_ms, reverse=True)[:limit]

    # ---- contracts and market data ----
    @staticmethod
    def _market(market: str) -> None:
        if market not in MARKETS:
            raise NotSupported(f"IBKR in this app supports only stocks, not {market!r}")

    def conid(self, symbol: str) -> int:
        """US stock symbol -> IBKR contract ID via GET /trsrv/stocks. Ambiguous or missing symbols are refused."""
        with _cache_lock:
            hit = _conids.get(symbol)
        if hit and time.monotonic() - hit[0] < CONID_TTL_S:
            return hit[1]
        data = self._get("/trsrv/stocks", {"symbols": symbol})
        entries = data.get(symbol, []) if isinstance(data, dict) else []
        us = {int(c["conid"]) for e in entries if isinstance(e, dict) and e.get("assetClass") == "STK"
              for c in e.get("contracts", []) if isinstance(c, dict) and c.get("isUS") is True and c.get("conid")}
        if len(us) != 1:
            raise ExchangeError(f"IBKR has {'no' if not us else 'more than one'} US stock listing for {symbol}; "
                                "this app needs exactly one")
        conid = us.pop()
        with _cache_lock:
            _conids[symbol] = (time.monotonic(), conid)
        return conid

    def symbol_rules(self, symbol: str, market: str) -> SymbolRules:
        self._market(market)
        conid = self.conid(symbol)
        r = self.check(self._call("POST", "/iserver/contract/rules", body={"conid": conid, "isBuy": True}))
        if not isinstance(r, dict) or r.get("error"):
            raise ExchangeError(f"IBKR returned no trading rules for {symbol}")
        types = tuple(t for t in ("limit", "market") if t in (r.get("orderTypes") or []))
        # IBKR reports no lot size or minimums here; quantity rules are left to IBKR (None = not reported).
        return SymbolRules(symbol=symbol, market=market, base_asset=symbol, quote_asset=str(r.get("cashCcy") or "USD"),
                           status="TRADING" if types else "UNKNOWN", price_tick=D(r.get("increment")),
                           order_types=types)

    def reference_price(self, symbol: str, market: str) -> Decimal:
        self._market(market)
        conid = self.conid(symbol)
        row: dict[str, Any] = {}
        for _ in range(2):  # IBKR: the first ("pre-flight") snapshot request for a conid returns no data
            data = self._get("/iserver/marketdata/snapshot", {"conids": conid, "fields": "31"})
            row = next((x for x in data if isinstance(x, dict) and str(x.get("conid")) == str(conid)), {}) \
                if isinstance(data, list) else {}
            if row.get("31") not in (None, ""):
                break
        last = str(row.get("31") or "").replace(",", "")
        if not last:
            raise ExchangeError(f"IBKR has no last price for {symbol} yet (market data starting or not subscribed); "
                                "try again in a few seconds")
        if last[0] in "CH":  # C = previous close, H = trading halted
            raise ExchangeError(f"IBKR reports no current trade price for {symbol} "
                                f"({'previous close only' if last[0] == 'C' else 'trading halted'})")
        price = D(last)
        if not price or price <= 0:
            raise ExchangeError(f"IBKR returned no usable price for {symbol}")
        return price

    # ---- paper-only order management ----
    def place_order(self, cred: Credential, order: OrderRequest) -> Order:
        self._require_testnet()
        self._market(order.market)
        if order.reduce_only:
            raise NotSupported("reduce-only does not apply to IBKR stock orders")
        if order.time_in_force is not None and order.time_in_force not in TIFS:
            raise NotSupported(f"IBKR stock orders in this app support GTC, IOC or day orders, not {order.time_in_force}")
        if len(order.client_order_id) > 64:
            raise NotSupported("IBKR client order IDs (cOID) are limited to 64 characters")
        account, _ = self._paper_session(cred)  # also the /iserver/accounts call IBKR requires before orders
        ticket: dict[str, Any] = {"acctId": account, "conid": self.conid(order.symbol), "secType": "STK",
                                  "cOID": order.client_order_id, "orderType": ORDER_TYPES[order.type],
                                  "side": order.side.upper(), "quantity": float(order.quantity),
                                  "tif": TIFS.get(order.time_in_force or "", "DAY")}
        if order.type == "limit":
            ticket["price"] = float(order.price)
        if Decimal(str(ticket["quantity"])) != order.quantity or (
                order.type == "limit" and Decimal(str(ticket["price"])) != order.price):
            raise NotSupported("the quantity or price cannot be sent to IBKR without rounding")  # JSON numbers only
        resp = self._call("POST", f"/iserver/account/{account}/orders", body={"orders": [ticket]})
        data = self._order_write_result(resp, "order")
        return Order(order_id=data["order_id"], client_order_id=order.client_order_id, symbol=order.symbol,
                     market="stock", side=order.side, type=order.type,
                     status=STATUS.get(data["order_status"], OrderStatus.UNKNOWN), price=order.price, quantity=order.quantity,
                     created_ms=self.now_ms())

    def _order_write_result(self, resp: HttpResponse, what: str) -> dict[str, str]:
        """Maps an order-write answer. Reply questions and prompts are surfaced, never answered."""
        if resp.status >= 500:
            raise OutcomeUnknown(f"IBKR answered HTTP {resp.status} to the {what}: its outcome is unknown")
        if resp.status in (401, 403, 429) or (resp.status >= 400 and not resp.body):
            self.check(resp)
        try:
            data = json.loads(resp.body or b"null")
        except ValueError:
            raise OutcomeUnknown(f"IBKR sent an unreadable answer to the {what}: its outcome is unknown") from None
        items = data if isinstance(data, list) else [data]
        first = items[0] if items and isinstance(items[0], dict) else {}
        if first.get("id") and first.get("message"):
            text = " ".join(str(m) for m in first.get("message", []) if m)
            raise ExchangeError(f"IBKR asks you to confirm this {what} and the app never confirms on your behalf, "
                                f"so it was NOT placed. IBKR's question: {_clean(text)}")
        if first.get("prompt") or first.get("options"):
            raise ExchangeError(f"IBKR rejected the {what} with a prompt the app does not answer: "
                                f"{_clean(str(first.get('text', '')))}")
        if first.get("error"):
            raise ExchangeError(f"IBKR refused the {what}: {_clean(str(first['error']))}")
        if resp.status >= 400:
            raise ExchangeError(f"IBKR refused the {what} (HTTP {resp.status})")
        if str(first.get("order_id", "")).isdigit():
            return {"order_id": str(first["order_id"]), "order_status": str(first.get("order_status", ""))}
        raise OutcomeUnknown(f"IBKR's answer to the {what} has no order ID: its outcome is unknown")

    def query_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order | None:
        """Finds the order by cOID (reported as order_ref) among the session's orders. None = not listed."""
        self._market(market)
        account, _ = self._paper_session(cred)
        row = next((o for o in self._session_orders(account) if o.get("order_ref") == client_order_id), None)
        return self._order(row) if row else None

    def cancel_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order:
        self._require_testnet()
        current = self.query_order(cred, symbol, market, client_order_id=client_order_id)
        if current is None:
            raise ExchangeError("IBKR does not list this order in the current gateway session; it cannot be cancelled here")
        if not current.order_id.isdigit():
            raise ExchangeError("IBKR returned an invalid order ID")
        account = self._ids(cred)[0]
        resp = self._call("DELETE", f"/iserver/account/{account}/order/{current.order_id}")
        if resp.status >= 500:
            raise OutcomeUnknown(f"IBKR answered HTTP {resp.status} to the cancel: its outcome is unknown")
        data = self.check(resp)
        if not isinstance(data, dict) or data.get("error") or data.get("msg") != "Request was submitted":
            raise ExchangeError("IBKR did not accept the cancel request"
                                + (f": {_clean(str(data.get('error')))}" if isinstance(data, dict) and data.get("error") else ""))
        # IBKR acknowledges the request only; the cancel itself is confirmed later by the order status.
        return current.model_copy(update={"status": OrderStatus.PENDING_CANCEL})


def _amount(summary: dict[str, Any], key: str) -> Decimal | None:
    v = summary.get(key)
    if not isinstance(v, dict) or v.get("isNull") is True:
        return None
    return D(v.get("amount"))


def _clean(text: str) -> str:
    return " ".join(text.split())[:400]
