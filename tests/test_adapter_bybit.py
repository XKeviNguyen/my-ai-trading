"""R8: Bybit V5 adapter against payload shapes from the official V5 docs (bybit-exchange.github.io/docs/v5).
No network: a recording transport answers by (method, path)."""
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from my_ai_trading.accounts.adapters import bybit as B
from my_ai_trading.accounts.adapters.base import (
    AuthError, ExchangeError, HttpResponse, NotSupported, OrderRequest, OutcomeUnknown, RateLimited,
)
from my_ai_trading.accounts.credentials import Credential, Secret
from my_ai_trading.accounts.models import Environment, OrderStatus
from my_ai_trading.accounts.service import PolicyRefused, check_key_policy

CRED = Credential(Secret("bybit-test-api-key-0001"), Secret("bybit-test-api-secret-should-never-leak"))
NOW = 1_800_000_000.0


def ok(result, status=200):
    return HttpResponse(status, json.dumps({"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {},
                                            "time": int(NOW * 1000)}).encode(), {})


def err(code, msg="error", headers=None):
    return HttpResponse(200, json.dumps({"retCode": code, "retMsg": msg, "result": {}}).encode(), headers or {})


class Recorder:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.calls.append({"method": method, "host": parts.scheme + "://" + parts.netloc, "path": parts.path,
                           "raw_query": parts.query, "query": parse_qs(parts.query), "headers": headers, "body": body})
        handler = self.routes.get((method, parts.path))
        if handler is None:
            return HttpResponse(404, b'{"retCode":10017,"retMsg":"no route in test"}', {})
        return handler(parts) if callable(handler) else handler


def adapter(env, routes):
    B._offsets.clear()
    time_route = ok({"timeSecond": str(int(NOW)), "timeNano": str(int(NOW) * 10**9)})
    rec = Recorder({("GET", "/v5/market/time"): time_route, **routes})
    return B.Bybit(env, transport=rec, clock=lambda: NOW), rec


def calls_to(rec, path):
    return [c for c in rec.calls if c["path"] == path]


# The example response of GET /v5/user/query-api in the docs: a read-only key with read scopes.
DOC_KEY_INFO = {
    "id": "2208369", "note": "testnet", "apiKey": "XXXXXXXX", "readOnly": 1, "secret": "",
    "permissions": {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"],
                    "Wallet": ["AccountTransfer", "SubMemberTransfer"], "Options": [], "Derivatives": ["DerivativesTrade"],
                    "CopyTrading": [], "BlockTrade": [], "Exchange": ["ExchangeHistory"], "NFT": [], "Affiliate": [],
                    "Earn": ["Earn"], "FiatP2P": ["FiatP2POrder", "Advertising"],
                    "FiatConvertBroker": ["FiatConvertBrokerOrder"], "FiatGlobalPay": [], "FiatBitPay": ["FaitPayOrder"],
                    "BitCard": ["BitCard"], "ByXPost": ["ByXPost"]},
    "ips": ["18.181.170.164", "13.212.45.47", "13.212.45.48"], "type": 1, "deadlineDay": -2,
    "expiredAt": "1970-01-01T00:00:00Z", "createdAt": "2025-10-13T03:20:45Z", "uta": 1, "isMaster": True,
}


def test_signed_get_and_post_follow_the_documented_rule_and_never_carry_the_secret():
    # /v5/guide: sign timestamp + api_key + recv_window + (queryString | jsonBodyString) with HMAC-SHA256 (hex).
    # The docs' example signature does not publish its secret, so the rule is recomputed independently here.
    order = OrderRequest(symbol="BTCUSDT", market="usdm_perpetual", side="sell", type="limit", quantity=Decimal("0.010"),
                         price=Decimal("70000.0"), client_order_id="mat-0002", reduce_only=True)
    a, rec = adapter(Environment.TESTNET, {
        ("GET", "/v5/user/query-api"): ok(DOC_KEY_INFO),
        ("POST", "/v5/order/create"): ok({"orderId": "1", "orderLinkId": "mat-0002"}),
        ("GET", "/v5/order/realtime"): ok({"list": [], "category": "linear"}),
        ("GET", "/v5/order/history"): ok({"list": [], "category": "linear"})})
    a.permissions(CRED)
    a.place_order(CRED, order)
    secret, key = CRED.api_secret.reveal(), CRED.api_key.reveal()
    get, post = calls_to(rec, "/v5/user/query-api")[0], calls_to(rec, "/v5/order/create")[0]
    for call, payload in ((get, get["raw_query"]), (post, post["body"].decode())):
        h = call["headers"]
        assert h["X-BAPI-API-KEY"] == key and h["X-BAPI-RECV-WINDOW"] == "5000"
        assert h["X-BAPI-TIMESTAMP"] == str(int(NOW * 1000))
        expected = hmac.new(secret.encode(), (h["X-BAPI-TIMESTAMP"] + key + "5000" + payload).encode(),
                            hashlib.sha256).hexdigest()
        assert h["X-BAPI-SIGN"] == expected
    assert json.loads(post["body"]) == {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Limit",
                                        "qty": "0.01", "orderLinkId": "mat-0002", "price": "70000",
                                        "timeInForce": "GTC", "reduceOnly": True}
    for call in rec.calls:
        assert secret not in json.dumps([call["path"], call["raw_query"], call["headers"],
                                         (call["body"] or b"").decode()])


def test_testnet_and_live_use_only_their_fixed_hosts():
    hosts = {Environment.TESTNET: {"https://api-testnet.bybit.com"}, Environment.LIVE: {"https://api.bybit.com"}}
    for env, allowed in hosts.items():
        a, rec = adapter(env, {})
        with pytest.raises(ExchangeError):
            a.snapshot(CRED, "c1")
        assert rec.calls and {c["host"] for c in rec.calls} <= allowed


def test_live_permissions_accept_the_documented_read_only_key_and_refuse_read_write_or_unknown():
    a, _ = adapter(Environment.LIVE, {("GET", "/v5/user/query-api"): ok(DOC_KEY_INFO)})
    perms = a.permissions(CRED)
    assert (perms.can_trade, perms.can_move_funds, perms.ip_restricted, perms.expires_ms) == (False, False, True, None)
    check_key_policy(Environment.LIVE, perms)

    trading = {**DOC_KEY_INFO, "readOnly": 0, "permissions": {"Spot": ["SpotTrade"], "Wallet": []}}
    a, _ = adapter(Environment.LIVE, {("GET", "/v5/user/query-api"): ok(trading)})
    perms = a.permissions(CRED)
    assert perms.spot_trade is True and perms.can_move_funds is False
    with pytest.raises(PolicyRefused, match="trading"):
        check_key_policy(Environment.LIVE, perms)

    for info in ({**DOC_KEY_INFO, "readOnly": 0, "permissions": {"Wallet": ["Withdraw"]}},
                 {**DOC_KEY_INFO, "readOnly": 0, "permissions": {"SomethingNew": ["X"]}},  # unmapped group
                 {k: v for k, v in DOC_KEY_INFO.items() if k != "readOnly"}):  # flag missing
        a, _ = adapter(Environment.LIVE, {("GET", "/v5/user/query-api"): ok(info)})
        with pytest.raises(PolicyRefused):
            check_key_policy(Environment.LIVE, a.permissions(CRED))


DOC_WALLET = {"list": [{  # example response of GET /v5/account/wallet-balance
    "totalEquity": "3.31216591", "accountIMRate": "0", "totalMarginBalance": "3.00326056", "totalInitialMargin": "0",
    "accountType": "UNIFIED", "totalAvailableBalance": "3.00326056", "accountMMRate": "0", "totalPerpUPL": "0",
    "totalWalletBalance": "3.00326056", "accountLTV": "0", "totalMaintenanceMargin": "0",
    "coin": [{"availableToBorrow": "3", "bonus": "0", "accruedInterest": "0", "availableToWithdraw": "0",
              "totalOrderIM": "0", "equity": "0", "totalPositionMM": "0", "usdValue": "0", "spotHedgingQty": "0.01592413",
              "unrealisedPnl": "0", "collateralSwitch": True, "borrowAmount": "0.0", "totalPositionIM": "0",
              "walletBalance": "0", "cumRealisedPnl": "0", "locked": "0", "marginCollateral": True, "coin": "BTC",
              "spotBorrow": "0"},
             {"coin": "USDT", "walletBalance": "3.0", "locked": "", "usdValue": "", "equity": "3.0"}]}]}
POSITION = {  # the docs' position example, as a linear row; liqPrice "" = no liquidation price
    "positionIdx": 0, "symbol": "BTCUSDT", "side": "Sell", "size": "0.010", "avgPrice": "27464.50441675",
    "positionValue": "274.6450", "positionStatus": "Normal", "leverage": "10", "markPrice": "28224.50", "liqPrice": "",
    "unrealisedPnl": "-7.59995583", "curRealisedPnl": "0.00013123", "createdTime": "1676538056258",
    "updatedTime": "1697673600012"}
DOC_OPEN_ORDER = {  # example response of GET /v5/order/realtime
    "orderId": "fd4300ae-7847-404e-b947-b46980a4d140", "orderLinkId": "test-000005", "symbol": "ETHUSDT",
    "price": "1600.00", "qty": "0.10", "side": "Buy", "positionIdx": 1, "orderStatus": "New", "avgPrice": "0",
    "leavesQty": "0.10", "cumExecQty": "0.00", "timeInForce": "GTC", "orderType": "Limit", "triggerPrice": "0.00",
    "reduceOnly": False, "createdTime": "1684738540559", "updatedTime": "1684738540561"}


def test_snapshot_maps_unified_account_positions_and_orders_without_inventing_values():
    def realtime(parts):
        cat = parse_qs(parts.query)["category"][0]
        return ok({"list": [DOC_OPEN_ORDER] if cat == "linear" else [], "nextPageCursor": "", "category": cat})
    flat = {**POSITION, "symbol": "ETHUSDT", "side": "", "size": "0", "liqPrice": "0"}
    routes = {("GET", "/v5/account/wallet-balance"): ok(DOC_WALLET),
              ("GET", "/v5/account/info"): ok({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 4}),
              ("GET", "/v5/position/list"): ok({"list": [POSITION, flat], "nextPageCursor": "", "category": "linear"}),
              ("GET", "/v5/order/realtime"): realtime}
    a, rec = adapter(Environment.LIVE, routes)
    snap = a.snapshot(CRED, "conn1")
    t = snap.totals[0]
    assert (t.wallet, t.equity, t.available_balance) == ("unified", Decimal("3.31216591"), Decimal("3.00326056"))
    usdt = next(b for b in snap.balances if b.asset == "USDT")
    assert usdt.total == Decimal("3.0") and usdt.free is None and usdt.locked is None and usdt.usd_value is None
    assert usdt.usd_value_provenance is None
    [pos] = snap.positions  # the empty position is dropped
    assert (pos.side, pos.quantity, pos.leverage, pos.margin_mode) == ("short", Decimal("0.010"), Decimal(10), "cross")
    assert pos.liquidation_price is None and pos.entry_price == Decimal("27464.50441675")
    [order] = snap.open_orders
    assert (order.status, order.client_order_id, order.average_price, order.stop_price) == \
        (OrderStatus.NEW, "test-000005", None, None)
    assert snap.unavailable == [] and snap.warnings == []
    assert calls_to(rec, "/v5/position/list")[0]["query"]["settleCoin"] == ["USDT"]


def test_snapshot_reports_a_refused_section_as_unavailable_and_raises_when_everything_is_refused():
    routes = {("GET", "/v5/account/wallet-balance"): err(10028, "The API can only be accessed by unified account users."),
              ("GET", "/v5/account/info"): ok({"marginMode": "PORTFOLIO_MARGIN"}),
              ("GET", "/v5/position/list"): ok({"list": [POSITION]}),
              ("GET", "/v5/order/realtime"): ok({"list": []})}
    a, _ = adapter(Environment.TESTNET, routes)
    snap = a.snapshot(CRED, "conn1")
    assert len(snap.unavailable) == 1 and snap.unavailable[0].startswith("unified account balances")
    assert snap.positions[0].margin_mode is None  # portfolio margin is neither cross nor isolated
    a, _ = adapter(Environment.TESTNET, {(m, p): err(10003, "API key is invalid.") for m, p in routes})
    with pytest.raises(AuthError, match="10003"):
        a.snapshot(CRED, "conn1")


ORDER = OrderRequest(symbol="BTCUSDT", market="spot", side="buy", type="market", quantity=Decimal("0.00100000"),
                     client_order_id="mat-0001")


def test_live_order_endpoints_are_disabled_before_any_request():
    a, rec = adapter(Environment.LIVE, {})
    with pytest.raises(NotSupported, match="disabled"):
        a.place_order(CRED, ORDER)
    with pytest.raises(NotSupported, match="disabled"):
        a.query_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001")
    with pytest.raises(NotSupported, match="disabled"):
        a.cancel_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001")
    assert rec.calls == []


def test_spot_market_order_is_sized_in_base_coin_and_the_ack_is_not_treated_as_a_status():
    ack = ok({"orderId": "1321003749386327552", "orderLinkId": "mat-0001"})
    filled = {**DOC_OPEN_ORDER, "symbol": "BTCUSDT", "orderLinkId": "mat-0001", "orderStatus": "Filled",
              "orderType": "Market", "qty": "0.001", "cumExecQty": "0.001", "avgPrice": "65000.5"}
    a, rec = adapter(Environment.TESTNET, {("POST", "/v5/order/create"): ack,
                                           ("GET", "/v5/order/realtime"): ok({"list": [filled]})})
    order = a.place_order(CRED, ORDER)
    body = json.loads(calls_to(rec, "/v5/order/create")[0]["body"])
    assert body == {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.001",
                    "orderLinkId": "mat-0001", "marketUnit": "baseCoin"}
    assert order.status is OrderStatus.FILLED and order.average_price == Decimal("65000.5")

    a, _ = adapter(Environment.TESTNET, {("POST", "/v5/order/create"): ack,
                                         ("GET", "/v5/order/realtime"): ok({"list": []}),
                                         ("GET", "/v5/order/history"): ok({"list": []})})
    order = a.place_order(CRED, ORDER)
    assert order.status is OrderStatus.UNKNOWN and order.order_id == "1321003749386327552"
    assert order.provenance == "derived"


@pytest.mark.parametrize("response", [
    HttpResponse(503, b"", {}),
    HttpResponse(200, b"<html>gateway</html>", {}),
    err(10000, "Server Timeout"),
    err(10016, "Server error."),
    err(429, "The trading service is experiencing a high server load."),
    err(170146, "Order creation timeout"),
    err(170035, "Submitted to the system for processing!"),
    err(110072, "OrderLinkedID is duplicate"),
])
def test_unknown_order_outcome_is_never_reported_as_a_failure(response):
    a, _ = adapter(Environment.TESTNET, {("POST", "/v5/order/create"): response})
    with pytest.raises(OutcomeUnknown):
        a.place_order(CRED, ORDER)


def test_a_definite_rejection_is_an_ordinary_error_and_a_missing_order_is_none():
    a, rec = adapter(Environment.TESTNET, {("POST", "/v5/order/create"): err(170131, "Insufficient balance."),
                                           ("GET", "/v5/order/realtime"): ok({"list": []}),
                                           ("GET", "/v5/order/history"): ok({"list": []}),
                                           ("POST", "/v5/order/cancel"): err(110001, "Order does not exist")})
    with pytest.raises(ExchangeError, match="170131") as exc:
        a.place_order(CRED, ORDER)
    assert not isinstance(exc.value, OutcomeUnknown)
    assert a.query_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001") is None
    assert calls_to(rec, "/v5/order/history")[0]["query"]["orderLinkId"] == ["mat-0001"]
    with pytest.raises(ExchangeError, match="110001") as exc:
        a.cancel_order(CRED, "BTCUSDT", "spot", client_order_id="mat-0001")
    assert not isinstance(exc.value, OutcomeUnknown)


def test_retcode_auth_and_rate_limit_errors_are_typed():
    a, _ = adapter(Environment.TESTNET, {("GET", "/v5/user/query-api"): err(10010, "Unmatched IP")})
    with pytest.raises(AuthError, match="10010"):
        a.permissions(CRED)
    reset = {"x-bapi-limit-reset-timestamp": str(int(NOW * 1000) + 3000)}
    a, _ = adapter(Environment.TESTNET, {("GET", "/v5/user/query-api"): err(10006, "Too many visits!", reset)})
    with pytest.raises(RateLimited) as exc:
        a.permissions(CRED)
    assert exc.value.retry_after_s == 3.0


def test_unrecognized_order_status_maps_to_unknown_and_history_follows_cursors_verbatim():
    def history(parts):
        q = parse_qs(parts.query)
        if "cursor" not in q:
            return ok({"list": [{**DOC_OPEN_ORDER, "orderStatus": "SomethingNew"}], "nextPageCursor": "page%3D2"})
        return ok({"list": [{**DOC_OPEN_ORDER, "orderStatus": "PartiallyFilledCanceled"}], "nextPageCursor": "page%3D3"})
    a, rec = adapter(Environment.LIVE, {("GET", "/v5/order/history"): history})
    orders = a.order_history(CRED, "ETHUSDT", "usdm_perpetual", limit=2)
    assert [o.status for o in orders] == [OrderStatus.UNKNOWN, OrderStatus.CANCELED]
    second = calls_to(rec, "/v5/order/history")[1]["raw_query"]
    assert "cursor=page%3D2" in second  # sent as received, not double-encoded
    assert len(calls_to(rec, "/v5/order/history")) == 2  # stops at the limit


DOC_SPOT_INSTRUMENT = {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
                       "lotSizeFilter": {"basePrecision": "0.000001", "quotePrecision": "0.0000001",
                                         "minOrderQty": "0.000011", "maxOrderQty": "83", "minOrderAmt": "5",
                                         "maxOrderAmt": "8000000", "maxLimitOrderQty": "83", "maxMarketOrderQty": "41.5"},
                       "priceFilter": {"tickSize": "0.1"}}
DOC_LINEAR_INSTRUMENT = {"symbol": "BTCUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "BTC",
                         "quoteCoin": "USDT", "priceFilter": {"minPrice": "0.10", "maxPrice": "1999999.80", "tickSize": "0.10"},
                         "lotSizeFilter": {"maxOrderQty": "1190.000", "minOrderQty": "0.001", "qtyStep": "0.001",
                                           "maxMktOrderQty": "500.000", "minNotionalValue": "5"}}


def test_symbol_rules_read_documented_instrument_filters():
    def info(parts):
        cat = parse_qs(parts.query)["category"][0]
        sym = parse_qs(parts.query)["symbol"][0]
        row = DOC_SPOT_INSTRUMENT if cat == "spot" else DOC_LINEAR_INSTRUMENT
        return ok({"category": cat, "list": [row] if sym == "BTCUSDT" else []})
    a, rec = adapter(Environment.TESTNET, {("GET", "/v5/market/instruments-info"): info})
    s = a.symbol_rules("BTCUSDT", "spot")
    assert (s.price_tick, s.quantity_step, s.min_quantity, s.max_quantity, s.min_notional) == \
        (Decimal("0.1"), Decimal("0.000001"), None, Decimal("41.5"), Decimal("5"))
    lin = a.symbol_rules("BTCUSDT", "usdm_perpetual")
    assert (lin.quantity_step, lin.min_quantity, lin.max_quantity, lin.status) == \
        (Decimal("0.001"), Decimal("0.001"), Decimal("500"), "Trading")
    with pytest.raises(ExchangeError):
        a.symbol_rules("NOPEUSDT", "spot")
    assert all("X-BAPI-API-KEY" not in c["headers"] for c in rec.calls)  # public endpoints are unsigned


DOC_TRANSLOG = {"nextPageCursor": "", "list": [  # first two rows of the docs' transaction-log example
    {"id": "592324_XRPUSDT_161440249321", "symbol": "XRPUSDT", "side": "Buy", "funding": "-0.003676", "orderLinkId": "",
     "fee": "0.00000000", "change": "-0.003676", "cashFlow": "0", "transactionTime": "1672128000000",
     "type": "SETTLEMENT", "currency": "USDT", "category": "linear", "tradeId": "534c0003-4bf7-486f-aa02-78cee36825e4"},
    {"id": "592324_XRPUSDT_161440249321", "symbol": "XRPUSDT", "side": "Buy", "funding": "", "fee": "0.01908720",
     "change": "-0.0190872", "cashFlow": "0", "transactionTime": "1672121182224", "type": "TRADE", "currency": "USDT",
     "category": "linear", "tradeId": "5184f079-88ec-54c7-8774-5173cafd2b4e"}]}


def test_cashflows_map_the_transaction_log_and_include_deposits_and_withdrawals_only_on_live():
    routes = {("GET", "/v5/account/transaction-log"): ok(DOC_TRANSLOG),
              ("GET", "/v5/asset/deposit/query-record"): ok({"rows": [
                  {"coin": "USDT", "chain": "TRX", "amount": "999.0496", "txID": "04bf3fba", "status": 3,
                   "successAt": "1742728163000", "id": "160237231"}], "nextPageCursor": ""}),
              ("GET", "/v5/asset/withdraw/query-record"): ok({"rows": [
                  {"coin": "USDC", "chain": "ETH", "amount": "41.43008", "txID": "0x3d7b", "status": "success",
                   "createTime": "1742738305000", "withdrawId": "131629076"}], "nextPageCursor": ""})}
    live, rec = adapter(Environment.LIVE, routes)
    flows = [(c.kind, c.asset, c.amount, c.time_ms) for c in live.cashflows(CRED)]
    assert flows == [("withdrawal", "USDC", Decimal("-41.43008"), 1742738305000),
                     ("deposit", "USDT", Decimal("999.0496"), 1742728163000),
                     ("funding_fee", "USDT", Decimal("-0.003676"), 1672128000000),
                     ("commission", "USDT", Decimal("-0.01908720"), 1672121182224)]  # zero fee / cashFlow rows dropped
    q = calls_to(rec, "/v5/account/transaction-log")[0]["query"]
    assert int(q["endTime"][0]) - int(q["startTime"][0]) == 7 * 24 * 3600 * 1000  # the documented maximum window
    test, rec = adapter(Environment.TESTNET, routes)
    assert [c.kind for c in test.cashflows(CRED)] == ["funding_fee", "commission"]
    assert not any(c["path"].startswith("/v5/asset/") for c in rec.calls)
