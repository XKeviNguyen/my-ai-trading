"""R8 M2: Binance adapter against payload shapes from the official docs / binance-connector-js types.
No network: a recording transport answers by (method, path)."""
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from my_ai_trading.accounts.adapters import binance as B
from my_ai_trading.accounts.adapters.base import (
    AuthError, ExchangeError, HttpResponse, NotSupported, OrderRequest, OutcomeUnknown,
)
from my_ai_trading.accounts.credentials import Credential, Secret
from my_ai_trading.accounts.models import Environment, OrderStatus
from my_ai_trading.accounts.service import PolicyRefused, check_key_policy

CRED = Credential(Secret("binance-test-api-key-0001"), Secret("binance-test-api-secret-should-never-leak"))
NOW = 1_800_000_000.0


def ok(data, status=200):
    return HttpResponse(status, json.dumps(data).encode(), {})


class Recorder:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.calls.append((method, parts.scheme + "://" + parts.netloc, parts.path, parse_qs(parts.query), headers))
        handler = self.routes.get((method, parts.path))
        if handler is None:
            return HttpResponse(404, b'{"code":-1,"msg":"no route in test"}', {})
        return handler(parts) if callable(handler) else handler


def adapter(env, routes):
    B._offsets.clear()
    B._info_cache.clear()
    rec = Recorder({("GET", "/api/v3/time"): ok({"serverTime": int(NOW * 1000)}),
                    ("GET", "/fapi/v1/time"): ok({"serverTime": int(NOW * 1000)}), **routes})
    return B.Binance(env, transport=rec, clock=lambda: NOW), rec


def test_hmac_signature_matches_binance_documented_example():
    # Example from the Binance API docs ("SIGNED endpoint examples").
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest() == \
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_signed_requests_carry_key_header_and_valid_signature_but_never_the_secret():
    a, rec = adapter(Environment.TESTNET, {("GET", "/api/v3/account"): ok({"canTrade": True, "balances": []}),
                                           ("GET", "/fapi/v3/account"): HttpResponse(401, b'{"code":-2015,"msg":"Invalid API-key"}', {})})
    a.permissions(CRED)
    method, host, path, query, headers = next(c for c in rec.calls if c[2] == "/api/v3/account")
    assert headers == {"X-MBX-APIKEY": CRED.api_key.reveal()}
    raw = [c for c in rec.calls if c[2] == "/api/v3/account"][0]
    for call in rec.calls:
        assert CRED.api_secret.reveal() not in json.dumps([call[2], call[3], call[4]])
    unsigned = "&".join(f"{k}={v[0]}" for k, v in query.items() if k != "signature")
    expected = hmac.new(CRED.api_secret.reveal().encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    assert query["signature"] == [expected] and query["recvWindow"] == ["5000"]
    assert raw[1] == "https://testnet.binance.vision"


def test_testnet_and_live_use_only_their_fixed_hosts():
    hosts = {Environment.TESTNET: {"https://testnet.binance.vision", "https://demo-fapi.binance.com"},
             Environment.LIVE: {"https://api.binance.com", "https://fapi.binance.com"}}
    for env, allowed in hosts.items():
        a, rec = adapter(env, {})
        with pytest.raises(Exception):
            a.snapshot(CRED, "c1")
        assert {c[1] for c in rec.calls} <= allowed


DOC_API_RESTRICTIONS = {  # the example response in the Binance docs
    "ipRestrict": False, "createTime": 1623840271000, "enableReading": True, "enableWithdrawals": False,
    "enableInternalTransfer": True, "enableMargin": False, "enableFutures": False, "permitsUniversalTransfer": True,
    "enableVanillaOptions": False, "enableFixApiTrade": False, "enableFixReadOnly": True,
    "enableSpotAndMarginTrading": False, "enablePortfolioMarginTrading": True,
}


def test_live_permissions_map_from_api_restrictions_and_the_policy_refuses_transfer_rights():
    a, _ = adapter(Environment.LIVE, {("GET", "/sapi/v1/account/apiRestrictions"): ok(DOC_API_RESTRICTIONS)})
    perms = a.permissions(CRED)
    assert perms.read is True and perms.withdraw is False
    assert perms.transfer is True  # enableInternalTransfer / permitsUniversalTransfer
    assert perms.derivatives_trade is True  # enablePortfolioMarginTrading
    with pytest.raises(PolicyRefused):
        check_key_policy(Environment.LIVE, perms)
    read_only = {k: (v if k in ("enableReading", "createTime", "enableFixReadOnly") else False)
                 for k, v in DOC_API_RESTRICTIONS.items()}
    a, _ = adapter(Environment.LIVE, {("GET", "/sapi/v1/account/apiRestrictions"): ok(read_only)})
    check_key_policy(Environment.LIVE, a.permissions(CRED))  # a truly read-only key is accepted


def test_testnet_key_that_works_on_neither_market_is_rejected():
    bad = HttpResponse(401, b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}', {})
    a, _ = adapter(Environment.TESTNET, {("GET", "/api/v3/account"): bad, ("GET", "/fapi/v3/account"): bad})
    with pytest.raises(AuthError, match="both spot"):
        a.permissions(CRED)


FUT_ACCOUNT = {"totalInitialMargin": "12.5", "totalMaintMargin": "1.25", "totalWalletBalance": "126.72469206",
               "totalUnrealizedProfit": "-3.1", "totalMarginBalance": "123.62469206", "availableBalance": "110.0",
               "assets": [{"asset": "USDT", "walletBalance": "126.72469206", "availableBalance": "110.0"},
                          {"asset": "BNB", "walletBalance": "0", "availableBalance": "0"}],
               "positions": []}
POSITIONS = [
    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.010", "entryPrice": "65000.0",
     "markPrice": "65310.0", "unRealizedProfit": "-3.1", "liquidationPrice": "77123.4", "notional": "-653.1",
     "marginAsset": "USDT"},
    {"symbol": "ADAUSDT", "positionSide": "BOTH", "positionAmt": "0", "entryPrice": "0", "markPrice": "0.38",
     "unRealizedProfit": "0", "liquidationPrice": "0", "notional": "0"},
    {"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "0.5", "entryPrice": "3000", "markPrice": "3010",
     "unRealizedProfit": "5", "liquidationPrice": "0", "notional": "1505"},
]
CONFIG = [{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 5},
          {"symbol": "ETHUSDT", "marginType": "ISOLATED", "leverage": 10}]


def test_snapshot_maps_spot_and_futures_and_never_invents_values():
    spot_account = {"canTrade": True, "balances": [{"asset": "BTC", "free": "0.01000000", "locked": "0.00100000"},
                                                   {"asset": "XRP", "free": "0.00000000", "locked": "0.00000000"}]}
    routes = {("GET", "/api/v3/account"): ok(spot_account), ("GET", "/api/v3/openOrders"): ok([]),
              ("GET", "/fapi/v3/account"): ok(FUT_ACCOUNT), ("GET", "/fapi/v3/positionRisk"): ok(POSITIONS),
              ("GET", "/fapi/v1/symbolConfig"): ok(CONFIG),
              ("GET", "/fapi/v1/openOrders"): ok([{"orderId": 7, "clientOrderId": "abc", "symbol": "BTCUSDT",
                                                  "side": "BUY", "type": "LIMIT", "status": "NEW", "price": "60000",
                                                  "origQty": "0.01", "executedQty": "0", "avgPrice": "0",
                                                  "time": 1, "updateTime": 2, "reduceOnly": False}])}
    a, _ = adapter(Environment.TESTNET, routes)
    snap = a.snapshot(CRED, "conn1")
    assert [(b.asset, b.free, b.locked) for b in snap.balances if b.wallet == "spot"] == \
        [("BTC", Decimal("0.01000000"), Decimal("0.00100000"))]  # zero balances dropped, total not invented
    fut = snap.totals[0]
    assert fut.equity == Decimal("123.62469206") and fut.unrealized_pnl == Decimal("-3.1")
    btc, eth = snap.positions
    assert (btc.side, btc.quantity, btc.leverage, btc.margin_mode) == ("short", Decimal("0.010"), Decimal(5), "cross")
    assert btc.liquidation_price == Decimal("77123.4")
    assert eth.side == "long" and eth.margin_mode == "isolated" and eth.liquidation_price is None  # "0" = none
    assert snap.open_orders[0].status is OrderStatus.NEW and snap.open_orders[0].average_price is None
    assert snap.unavailable == []


def test_snapshot_reports_a_market_the_key_cannot_use_as_unavailable():
    bad = HttpResponse(401, b'{"code":-2015,"msg":"Invalid API-key"}', {})
    routes = {("GET", "/api/v3/account"): ok({"balances": []}), ("GET", "/api/v3/openOrders"): ok([]),
              ("GET", "/fapi/v3/account"): bad}
    a, _ = adapter(Environment.TESTNET, routes)
    snap = a.snapshot(CRED, "conn1")
    assert snap.unavailable == ["usdm_perpetual: this key does not work on https://demo-fapi.binance.com"]


ORDER = OrderRequest(symbol="BTCUSDT", market="spot", side="buy", type="limit", quantity=Decimal("0.00100000"),
                     price=Decimal("60000.00"), client_order_id="mat-0001")


def test_live_order_endpoints_are_disabled_before_any_request():
    a, rec = adapter(Environment.LIVE, {})
    with pytest.raises(NotSupported, match="disabled"):
        a.place_order(CRED, ORDER)
    with pytest.raises(NotSupported, match="disabled"):
        a.cancel_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001")
    assert rec.calls == []


def test_testnet_order_sends_exact_documented_parameters():
    resp = {"symbol": "BTCUSDT", "orderId": 28, "clientOrderId": "mat-0001", "transactTime": 1, "price": "60000.00",
            "origQty": "0.00100000", "executedQty": "0.00000000", "cummulativeQuoteQty": "0.00000000",
            "status": "NEW", "timeInForce": "GTC", "type": "LIMIT", "side": "BUY"}
    a, rec = adapter(Environment.TESTNET, {("POST", "/api/v3/order"): ok(resp)})
    order = a.place_order(CRED, ORDER)
    q = next(c[3] for c in rec.calls if c[2] == "/api/v3/order")
    assert {k: v[0] for k, v in q.items() if k not in ("timestamp", "recvWindow", "signature")} == {
        "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.001", "price": "60000",
        "timeInForce": "GTC", "newClientOrderId": "mat-0001", "newOrderRespType": "RESULT"}
    assert order.status is OrderStatus.NEW and order.client_order_id == "mat-0001"


@pytest.mark.parametrize("response", [
    HttpResponse(408, b"{}", {}),  # docs: timeout waiting for the backend
    HttpResponse(400, b'{"code":-1006,"msg":"An unexpected response was received from the message bus."}', {}),
    HttpResponse(302, b"", {"location": "https://elsewhere.example"}),
    HttpResponse(503, b"", {}),
    HttpResponse(500, b'{"code":-1000,"msg":"internal"}', {}),
    HttpResponse(400, b'{"code":-1007,"msg":"Timeout waiting for response from backend server."}', {}),
])
def test_unknown_execution_status_is_never_reported_as_a_failure(response):
    a, _ = adapter(Environment.TESTNET, {("POST", "/api/v3/order"): response})
    with pytest.raises(OutcomeUnknown):
        a.place_order(CRED, ORDER)


def test_a_definite_rejection_is_an_ordinary_error_and_lookup_of_a_missing_order_is_none():
    reject = HttpResponse(400, b'{"code":-2010,"msg":"Account has insufficient balance for requested action."}', {})
    missing = HttpResponse(400, b'{"code":-2013,"msg":"Order does not exist."}', {})
    a, _ = adapter(Environment.TESTNET, {("POST", "/api/v3/order"): reject, ("GET", "/api/v3/order"): missing})
    with pytest.raises(ExchangeError, match="-2010") as exc:
        a.place_order(CRED, ORDER)
    assert not isinstance(exc.value, OutcomeUnknown)
    assert a.query_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001") is None


def test_symbol_rules_read_documented_filters():
    info = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
                         "orderTypes": ["LIMIT", "MARKET"],
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                                     {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000",
                                      "maxQty": "9000.00000000"},
                                     {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}
    a, _ = adapter(Environment.TESTNET, {("GET", "/api/v3/exchangeInfo"): ok(info)})
    r = a.symbol_rules("BTCUSDT", "spot")
    assert (r.price_tick, r.quantity_step, r.min_notional) == (Decimal("0.01"), Decimal("0.00001"), Decimal("5"))
    with pytest.raises(ExchangeError):
        a.symbol_rules("NOPEUSDT", "spot")


def test_cashflows_include_withdrawal_history_only_on_live_and_parse_utc_times():
    routes = {("GET", "/fapi/v1/income"): ok([{"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "-0.12",
                                              "asset": "USDT", "time": 1570000000000, "tranId": 9}]),
              ("GET", "/sapi/v1/capital/deposit/hisrec"): ok([{"coin": "USDT", "amount": "100", "status": 1,
                                                               "insertTime": 1570608000000, "txId": "0xabc"}]),
              ("GET", "/sapi/v1/capital/withdraw/history"): ok([{"coin": "BTC", "amount": "0.5", "status": 6,
                                                                 "applyTime": "2019-10-12 11:12:02", "txId": "0xdef"}])}
    live, _ = adapter(Environment.LIVE, routes)
    kinds = [(c.kind, c.amount, c.time_ms) for c in live.cashflows(CRED)]
    assert ("withdrawal", Decimal("-0.5"), 1570878722000) in kinds
    assert ("funding_fee", Decimal("-0.12"), 1570000000000) in kinds
    test, rec = adapter(Environment.TESTNET, routes)
    assert [c.kind for c in test.cashflows(CRED)] == ["funding_fee"]
    assert not any("/sapi/" in c[2] for c in rec.calls)


def test_a_missing_or_malformed_permission_flag_is_unknown_and_the_live_key_is_refused():
    only_read = {"enableReading": True}  # every write flag missing: unknown, never "off"
    a, _ = adapter(Environment.LIVE, {("GET", "/sapi/v1/account/apiRestrictions"): ok(only_read)})
    perms = a.permissions(CRED)
    assert perms.read is True and perms.spot_trade is None and perms.withdraw is None
    with pytest.raises(PolicyRefused):
        check_key_policy(Environment.LIVE, perms)
    stringly = {k: ("false" if isinstance(v, bool) else v) for k, v in DOC_API_RESTRICTIONS.items()}
    a, _ = adapter(Environment.LIVE, {("GET", "/sapi/v1/account/apiRestrictions"): ok(stringly)})
    with pytest.raises(PolicyRefused):
        check_key_policy(Environment.LIVE, a.permissions(CRED))
    a, _ = adapter(Environment.LIVE, {("GET", "/sapi/v1/account/apiRestrictions"): ok(["not", "a", "dict"])})
    from my_ai_trading.accounts.adapters.base import ExchangeUnavailable
    with pytest.raises(ExchangeUnavailable):
        a.permissions(CRED)


def test_a_malformed_reply_after_sending_is_an_unknown_outcome_not_a_crash(monkeypatch):
    from http.client import BadStatusLine
    from my_ai_trading.accounts.adapters import base

    class Opener:
        def open(self, req, timeout):
            raise BadStatusLine("garbage")  # re-audit ENV-12: an order send became HTTP 500 and stayed SENDING
    monkeypatch.setattr(base, "_OPENER", Opener())
    with pytest.raises(base.OutcomeUnknown):
        base.urllib_transport("POST", "https://testnet.binance.vision/api/v3/order", {}, b"x", 5.0)


def test_an_error_reply_with_a_broken_body_keeps_its_status(monkeypatch):
    from http.client import IncompleteRead
    from urllib.error import HTTPError
    from my_ai_trading.accounts.adapters import base

    class Broken:
        def read(self, *a):
            raise IncompleteRead(b"", 10)  # verifier B: raised out of the HTTPError handler
        def close(self):
            pass

    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 400, "Bad Request", {}, Broken())
    monkeypatch.setattr(base, "_OPENER", Opener())
    r = base.urllib_transport("POST", "https://testnet.binance.vision/api/v3/order", {}, b"x", 5.0)
    assert r.status == 400 and r.body == b""
