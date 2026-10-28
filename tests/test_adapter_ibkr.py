"""IBKR paper adapter against payload shapes from the official Web API docs (interactivebrokers.com/docs/web-api).
No network: a recording transport answers by (method, path)."""
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from my_ai_trading.accounts.adapters import ibkr as I
from my_ai_trading.accounts.adapters.base import (
    REGISTRY, AuthError, ExchangeError, HttpResponse, NotSupported, OrderRequest, OutcomeUnknown, adapter_for,
)
from my_ai_trading.accounts.credentials import Credential, Secret
from my_ai_trading.accounts.models import Environment, KeyPermissions, OrderStatus, WalletType
from my_ai_trading.accounts.service import PolicyRefused, check_key_policy

ACCT = "DU123456"
CRED = Credential(Secret(ACCT), Secret("paperuser1234"))
NOW = 1_800_000_000.0


def ok(data, status=200):
    return HttpResponse(status, json.dumps(data).encode(), {})


class Recorder:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.calls.append((method, parts.scheme + "://" + parts.netloc, parts.path, parse_qs(parts.query),
                           json.loads(body) if body else None))
        handler = self.routes.get((method, parts.path))
        if handler is None:
            return HttpResponse(404, b'{"error":"no route in test"}', {})
        return handler(parts) if callable(handler) else handler


P = "/v1/api"
# Shapes from the docs: /sso/validate (LOGIN_TYPE 2 = paper), /iserver/accounts (isPaper), /portfolio/accounts.
SSO = {"RESULT": True, "USER_NAME": "paperuser1234", "LOGIN_TYPE": 2, "PAPER_USER_NAME": "paperuser1234"}
ACCOUNTS = {"accounts": [ACCT], "selectedAccount": ACCT, "isPaper": True, "isFT": False}
PORTFOLIO_ACCOUNTS = [{"id": ACCT, "accountId": ACCT, "currency": "USD", "type": "DEMO", "noClientTrading": False}]
SESSION = {("GET", P + "/sso/validate"): ok(SSO), ("GET", P + "/iserver/accounts"): ok(ACCOUNTS),
           ("GET", P + "/portfolio/accounts"): ok(PORTFOLIO_ACCOUNTS),
           ("GET", P + "/trsrv/stocks"): ok({"AAPL": [
               {"name": "APPLE INC", "assetClass": "STK", "contracts": [
                   {"conid": 265598, "exchange": "NASDAQ", "isUS": True},
                   {"conid": 38708077, "exchange": "MEXI", "isUS": False}]},
               {"name": "LS 1X AAPL", "assetClass": "STK", "contracts": [
                   {"conid": 493546048, "exchange": "LSEETF", "isUS": False}]}]})}


def adapter(routes=None, env=Environment.TESTNET):
    I._conids.clear()
    rec = Recorder({**SESSION, **(routes or {})})
    return I.IBKR(env, transport=rec, clock=lambda: NOW), rec


def order_request(**kw):
    base = dict(symbol="AAPL", market="stock", side="buy", type="limit", quantity=Decimal("100"),
                price=Decimal("165"), time_in_force="GTC", client_order_id="mat0123456789abcdef")
    return OrderRequest(**{**base, **kw})


def test_registered_as_paper_only_broker_on_fixed_loopback_gateway():
    assert REGISTRY["ibkr"] is I.IBKR
    assert I.IBKR.INFO.kind == "broker" and I.IBKR.INFO.environments == (Environment.TESTNET,)
    assert I.BASE_URL == "https://localhost:5000/v1/api"
    with pytest.raises(NotSupported):
        adapter_for("ibkr", Environment.LIVE)  # refused before any request: no live entry exists
    with pytest.raises(NotSupported):  # the real transport refuses any other host
        I.gateway_transport("GET", "https://evil.example/v1/api/iserver/accounts", {}, None, 1.0)


def test_paper_session_is_verified_and_grants_trading_on_testnet_only():
    a, rec = adapter({("POST", P + "/iserver/auth/status"): ok({"authenticated": True, "connected": True,
                                                                 "competing": False, "message": ""})})
    perms = a.permissions(CRED)
    assert perms.read is True and perms.can_trade is True
    check_key_policy(Environment.TESTNET, perms)
    assert all(c[1] == "https://localhost:5000" for c in rec.calls)
    # The same "can trade, transfers unknown" answer would be refused for a live connection.
    with pytest.raises(PolicyRefused):
        check_key_policy(Environment.LIVE, perms)
    with pytest.raises(PolicyRefused):  # what permissions() reports for LIVE: nothing confirmed
        check_key_policy(Environment.LIVE, KeyPermissions(source="ibkr: no read-only confirmation exists"))


@pytest.mark.parametrize("routes, why", [
    ({("GET", P + "/iserver/accounts"): ok({**ACCOUNTS, "isPaper": False})}, "paper"),
    ({("GET", P + "/sso/validate"): ok({**SSO, "LOGIN_TYPE": 1})}, "LIVE"),
    ({("GET", P + "/sso/validate"): ok({**SSO, "USER_NAME": "someoneelse"})}, "different username"),
    ({("GET", P + "/iserver/accounts"): ok({**ACCOUNTS, "accounts": ["DU999999"]})}, "not tradable"),
])
def test_live_or_mismatched_gateway_session_is_refused(routes, why):
    a, _ = adapter(routes)
    with pytest.raises(AuthError, match=why):
        a.snapshot(CRED, "c1")


def test_order_methods_refuse_live_and_non_stock_before_any_request():
    a, rec = adapter()
    a.environment = Environment.LIVE  # simulate a future misconfiguration: the adapter still refuses
    with pytest.raises(NotSupported):
        a.place_order(CRED, order_request())
    with pytest.raises(NotSupported):
        a.cancel_order(CRED, "AAPL", "stock", client_order_id="mat0123456789abcdef")
    b, rec_b = adapter()
    with pytest.raises(NotSupported):
        b.place_order(CRED, order_request(market="spot"))
    with pytest.raises(NotSupported):
        b.place_order(CRED, order_request(time_in_force="FOK"))
    assert rec.calls == [] and rec_b.calls == []


def test_snapshot_maps_summary_ledger_positions_with_none_for_missing_values():
    summary = {"netliquidation": {"amount": 206415536, "isNull": False},
               "availablefunds": {"amount": 205266944, "isNull": False},
               "initmarginreq": {"amount": 268478.1875, "isNull": False},
               "maintmarginreq": {"amount": None, "isNull": True}}  # totalcashvalue missing entirely
    ledger = {"BASE": {"currency": "BASE", "cashbalance": 218093824, "unrealizedpnl": 154673.2},
              "USD": {"currency": "USD", "cashbalance": 213375440}}
    positions = [{"acctId": ACCT, "assetClass": "STK", "conid": 265598, "ticker": "AAPL", "position": -5,
                  "avgPrice": 192.26, "mktPrice": 190.1, "mktValue": -950.5, "unrealizedPnl": 10.8, "pageSize": 100},
                 {"acctId": ACCT, "assetClass": "STK", "conid": 8314, "ticker": "IBM", "position": 3,
                  "pageSize": 100},
                 {"acctId": ACCT, "assetClass": "CRYPTO", "conid": 479624278, "ticker": "BTC", "position": 0.0036,
                  "pageSize": 100}]
    orders = {"orders": [{"acct": ACCT, "account": ACCT, "orderId": 1234568790, "ticker": "AAPL", "status": "Submitted",
                          "side": "BUY", "origOrderType": "LIMIT", "price": "165", "totalSize": 100.0,
                          "filledQuantity": 0.0, "order_ref": "mat0123456789abcdef"},
                         {"acct": ACCT, "account": ACCT, "orderId": 1, "ticker": "AAPL", "status": "Filled",
                          "side": "SELL", "totalSize": 5.0, "filledQuantity": 5.0}]}
    a, _ = adapter({("GET", P + f"/portfolio/{ACCT}/summary"): ok(summary),
                    ("GET", P + f"/portfolio/{ACCT}/ledger"): ok(ledger),
                    ("GET", P + f"/portfolio/{ACCT}/positions/0"): ok(positions),
                    ("GET", P + "/iserver/account/orders"): ok(orders)})
    snap = a.snapshot(CRED, "c1")
    t = snap.totals[0]
    assert t.wallet == WalletType.SECURITIES and t.quote_asset == "USD"
    assert t.equity == Decimal("206415536") and t.available_balance == Decimal("205266944")
    assert t.maintenance_margin is None and t.wallet_balance is None  # isNull / missing stay None, never 0
    assert t.unrealized_pnl == Decimal("154673.2")
    assert [(b.asset, b.total, b.free) for b in snap.balances] == [("USD", Decimal("213375440"), None)]
    aapl, ibm = snap.positions
    assert (aapl.symbol, aapl.market, aapl.side, aapl.quantity) == ("AAPL", "stock", "short", Decimal("5"))
    assert aapl.entry_price == Decimal("192.26") and aapl.leverage is None and aapl.liquidation_price is None
    assert ibm.entry_price is None and ibm.mark_price is None and ibm.unrealized_pnl is None
    assert any("CRYPTO" in w for w in snap.warnings)
    assert [o.client_order_id for o in snap.open_orders] == ["mat0123456789abcdef"]  # filled order not "open"


def test_conid_lookup_picks_the_single_us_listing_and_refuses_ambiguity():
    a, rec = adapter()
    assert a.conid("AAPL") == 265598
    assert a.conid("AAPL") == 265598
    assert sum(1 for c in rec.calls if c[2] == P + "/trsrv/stocks") == 1  # cached
    b, _ = adapter({("GET", P + "/trsrv/stocks"): ok({"XYZ": [
        {"name": "A", "assetClass": "STK", "contracts": [{"conid": 1, "isUS": True}]},
        {"name": "B", "assetClass": "STK", "contracts": [{"conid": 2, "isUS": True}]}]})})
    with pytest.raises(ExchangeError, match="more than one"):
        b.conid("XYZ")


def test_reference_price_handles_preflight_and_refuses_previous_close():
    answers = iter([[{"conid": 265598, "conidEx": "265598"}],  # pre-flight: no data yet
                    [{"31": "168.42", "conid": 265598, "conidEx": "265598", "6509": "RpB"}]])
    a, _ = adapter({("GET", P + "/iserver/marketdata/snapshot"): lambda parts: ok(next(answers))})
    assert a.reference_price("AAPL", "stock") == Decimal("168.42")
    b, _ = adapter({("GET", P + "/iserver/marketdata/snapshot"): ok([{"31": "C168.42", "conid": 265598}])})
    with pytest.raises(ExchangeError, match="previous close"):
        b.reference_price("AAPL", "stock")


def test_place_order_sends_coid_and_maps_acknowledgement():
    a, rec = adapter({("POST", P + f"/iserver/account/{ACCT}/orders"):
                      ok([{"order_id": "987654", "order_status": "Submitted", "encrypt_message": "1"}])})
    order = a.place_order(CRED, order_request())
    assert (order.order_id, order.client_order_id, order.status) == ("987654", "mat0123456789abcdef", OrderStatus.NEW)
    ticket = next(c for c in rec.calls if c[0] == "POST")[4]["orders"][0]
    assert ticket == {"acctId": ACCT, "conid": 265598, "secType": "STK", "cOID": "mat0123456789abcdef",
                      "orderType": "LMT", "side": "BUY", "quantity": 100.0, "tif": "GTC", "price": 165.0}


def test_reply_questions_are_surfaced_and_never_auto_confirmed():
    question = [{"id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c", "isSuppressed": False, "messageIds": ["o163"],
                 "message": ["The following order \"BUY 100 AAPL NASDAQ.NMS @ 165.0\" price exceeds \nthe Percentage "
                             "constraint of 3%.\nAre you sure you want to submit this order?"]}]
    a, rec = adapter({("POST", P + f"/iserver/account/{ACCT}/orders"): ok(question)})
    with pytest.raises(ExchangeError, match="NOT placed.*Percentage constraint of 3%") as exc:
        a.place_order(CRED, order_request())
    assert not isinstance(exc.value, OutcomeUnknown)
    assert not any("/iserver/reply" in c[2] or "/questions/suppress" in c[2] for c in rec.calls)


@pytest.mark.parametrize("resp", [HttpResponse(503, b'{"error":"timeout"}', {}), HttpResponse(500, b"", {}),
                                  ok([{"unexpected": True}]), HttpResponse(200, b"<html>", {})])
def test_unknown_order_outcomes_map_to_outcome_unknown(resp):
    a, _ = adapter({("POST", P + f"/iserver/account/{ACCT}/orders"): resp})
    with pytest.raises(OutcomeUnknown):
        a.place_order(CRED, order_request(type="market", price=None, time_in_force=None))


def test_reconcile_by_coid_and_cancel_reports_pending_cancel_only():
    orders = {"orders": [{"acct": ACCT, "orderId": 1370093239, "ticker": "AAPL", "status": "PreSubmitted",
                          "side": "BUY", "orderType": "Limit", "totalSize": "100", "filledQuantity": "0",
                          "order_ref": "mat0123456789abcdef"}]}
    a, rec = adapter({("GET", P + "/iserver/account/orders"): ok(orders),
                      ("DELETE", P + f"/iserver/account/{ACCT}/order/1370093239"):
                      ok({"msg": "Request was submitted", "order_id": 1370093239, "conid": 265598, "account": ACCT})})
    found = a.query_order(CRED, "AAPL", "stock", client_order_id="mat0123456789abcdef")
    assert found.order_id == "1370093239" and found.status == OrderStatus.NEW
    assert a.query_order(CRED, "AAPL", "stock", client_order_id="matffffffffffffffff") is None
    cancelled = a.cancel_order(CRED, "AAPL", "stock", client_order_id="mat0123456789abcdef")
    assert cancelled.status == OrderStatus.PENDING_CANCEL  # acknowledgement only, not a confirmed cancel
    assert any(c[0] == "DELETE" for c in rec.calls)


def test_credential_fields_are_validated_before_any_request():
    a, rec = adapter()
    with pytest.raises(AuthError, match="account number"):
        a.snapshot(Credential(Secret("../../evil"), Secret("paperuser1234")), "c1")
    assert rec.calls == []


def test_an_order_from_an_earlier_gateway_session_is_cancelled_by_its_stored_id():
    from my_ai_trading.accounts.adapters.base import OutcomeUnknown
    a, rec = adapter({("GET", P + "/iserver/account/orders"): ok({"orders": []}),  # new session: not listed
                      ("DELETE", P + f"/iserver/account/{ACCT}/order/1370093239"):
                      ok({"msg": "Request was submitted", "order_id": 1370093239, "conid": 265598, "account": ACCT})})
    with pytest.raises(OutcomeUnknown, match="accepted the cancel request"):  # re-audit N4: was "cannot be cancelled"
        a.cancel_order(CRED, "AAPL", "stock", client_order_id="mat0123456789abcdef", exchange_order_id="1370093239")
    assert any(c[0] == "DELETE" and c[2].endswith("/order/1370093239") for c in rec.calls)
    with pytest.raises(I.ExchangeError, match="does not list"):
        a.cancel_order(CRED, "AAPL", "stock", client_order_id="mat0123456789abcdef")
