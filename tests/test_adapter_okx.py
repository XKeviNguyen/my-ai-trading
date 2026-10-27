"""OKX V5 adapter against payload shapes from the official docs (https://www.okx.com/docs-v5/en/).
No network: a recording transport answers by (method, path)."""
import base64
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from my_ai_trading.accounts.adapters import okx as X
from my_ai_trading.accounts.adapters.base import (
    AuthError, ExchangeError, HttpResponse, NotSupported, OrderRequest, OutcomeUnknown, RateLimited,
)
from my_ai_trading.accounts.credentials import Credential, Secret
from my_ai_trading.accounts.models import Environment, OrderStatus
from my_ai_trading.accounts.service import PolicyRefused, check_key_policy

CRED = Credential(Secret("okx-test-api-key-0001"), Secret("okx-test-api-secret-should-never-leak"),
                  Secret("okx-test-passphrase-never-leak"))
NOW = 1_800_000_000.0


def ok(data, status=200):
    return HttpResponse(status, json.dumps({"code": "0", "msg": "", "data": data}).encode(), {})


def err(code, msg, status=200, rows=None):
    return HttpResponse(status, json.dumps({"code": code, "msg": msg, "data": rows or []}).encode(), {})


class Recorder:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.calls.append({"method": method, "host": parts.scheme + "://" + parts.netloc, "path": parts.path,
                           "raw_query": parts.query, "query": parse_qs(parts.query), "headers": headers,
                           "body": body.decode() if body else ""})
        handler = self.routes.get((method, parts.path))
        if handler is None:
            return err("50005", "no route in test", 404)
        return handler(parts) if callable(handler) else handler


def adapter(env, routes):
    X._offsets.clear()
    X._info_cache.clear()
    rec = Recorder({("GET", "/api/v5/public/time"): ok([{"ts": str(int(NOW * 1000))}]), **routes})
    return X.OKX(env, transport=rec, clock=lambda: NOW), rec


def calls(rec, path):
    return [c for c in rec.calls if c["path"] == path]


# Doc example of GET /api/v5/account/config (perm lists every permission of the key).
DOC_CONFIG = {"acctLv": "2", "ip": "", "label": "v5 test", "perm": "read_only,withdraw,trade",
              "posMode": "long_short_mode", "uid": "44705892343619584", "mainUid": "44705892343619584"}
# Doc example instrument (SPOT) and a linear SWAP with the documented ctVal semantics (0.01 BTC per contract).
SPOT_INST = {"instId": "BTC-USDT", "instType": "SPOT", "baseCcy": "BTC", "quoteCcy": "USDT", "groupId": "1",
             "lotSz": "0.00000001", "minSz": "0.00001", "maxLmtSz": "9999999999", "tickSz": "0.1", "state": "live",
             "ctVal": "", "ctType": "", "ctValCcy": "", "settleCcy": ""}
SWAP_INST = {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "baseCcy": "", "quoteCcy": "", "groupId": "4",
             "ctVal": "0.01", "ctType": "linear", "ctValCcy": "BTC", "settleCcy": "USDT", "lotSz": "0.1",
             "minSz": "0.1", "maxLmtSz": "100000", "tickSz": "0.1", "state": "live"}


def instruments(parts):
    kind = parse_qs(parts.query)["instType"][0]
    return ok([SPOT_INST] if kind == "SPOT" else [SWAP_INST])


def test_signature_and_timestamp_follow_the_documented_rules():
    # Docs: sign = Base64(HmacSHA256(timestamp + 'GET' + '/api/v5/account/balance?ccy=BTC', SecretKey)),
    # timestamp e.g. 2020-12-08T09:08:57.715Z; the example SecretKey is 22582BD0CFF14C41EDBF1AB98506286D.
    secret, ts = "22582BD0CFF14C41EDBF1AB98506286D", "2020-12-08T09:08:57.715Z"
    expected = base64.b64encode(hmac.new(secret.encode(), (ts + "GET/api/v5/account/balance?ccy=BTC").encode(),
                                         hashlib.sha256).digest()).decode()
    assert X.sign(secret, ts, "get", "/api/v5/account/balance?ccy=BTC", "") == expected
    body = '{"instId":"BTC-USDT","lever":"5","mgnMode":"isolated"}'  # documented body example
    assert X.sign(secret, ts, "POST", "/api/v5/account/set-leverage", body) == base64.b64encode(
        hmac.new(secret.encode(), (ts + "POST/api/v5/account/set-leverage" + body).encode(), hashlib.sha256).digest()).decode()
    assert X.iso_timestamp(1607418537715) == ts


def test_signed_requests_carry_documented_headers_and_never_the_secret():
    a, rec = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG]),
                                           ("GET", "/api/v5/trade/fills"): ok([])})
    a.permissions(CRED)
    a.fills(CRED, "BTCUSDT", "spot", limit=5)
    for call in calls(rec, "/api/v5/account/config") + calls(rec, "/api/v5/trade/fills"):
        h = call["headers"]
        assert h["OK-ACCESS-KEY"] == CRED.api_key.reveal() and h["OK-ACCESS-PASSPHRASE"] == CRED.passphrase.reveal()
        assert h["OK-ACCESS-TIMESTAMP"] == "2027-01-15T08:00:00.000Z"
        request_path = call["path"] + (f"?{call['raw_query']}" if call["raw_query"] else "")
        assert h["OK-ACCESS-SIGN"] == X.sign(CRED.api_secret.reveal(), h["OK-ACCESS-TIMESTAMP"], call["method"],
                                             request_path, call["body"])
    assert calls(rec, "/api/v5/trade/fills")[0]["query"] == {"instType": ["SPOT"], "instId": ["BTC-USDT"], "limit": ["5"]}
    assert all(CRED.api_secret.reveal() not in json.dumps(c) for c in rec.calls)


def test_demo_header_only_on_testnet_and_only_the_fixed_host():
    for env in (Environment.TESTNET, Environment.LIVE):
        a, rec = adapter(env, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG]),
                               ("GET", "/api/v5/public/instruments"): instruments})
        a.permissions(CRED)
        a.symbol_rules("BTCUSDT", "spot")
        assert {c["host"] for c in rec.calls} == {"https://openapi.okx.com"}
        flags = [c["headers"].get("x-simulated-trading") for c in rec.calls]
        assert flags == (["1"] * len(flags) if env is Environment.TESTNET else [None] * len(flags))


def test_live_permissions_map_from_perm_and_the_policy_refuses_anything_but_read_only():
    a, _ = adapter(Environment.LIVE, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG])})
    perms = a.permissions(CRED)
    assert (perms.read, perms.spot_trade, perms.withdraw, perms.transfer) == (True, True, True, True)
    with pytest.raises(PolicyRefused):
        check_key_policy(Environment.LIVE, perms)
    for cfg, expect_ok in (({**DOC_CONFIG, "perm": "read_only", "ip": "117.37.203.58"}, True),
                           ({**DOC_CONFIG, "perm": "read_only,trade"}, False),  # trade includes funding transfer
                           ({**DOC_CONFIG, "perm": "read_only,future_perm"}, False),  # unknown -> transfer None
                           ({k: v for k, v in DOC_CONFIG.items() if k != "perm"}, False)):  # not reported
        a, _ = adapter(Environment.LIVE, {("GET", "/api/v5/account/config"): ok([cfg])})
        perms = a.permissions(CRED)
        if expect_ok:
            check_key_policy(Environment.LIVE, perms)
            assert perms.ip_restricted is True and perms.can_trade is False and perms.can_move_funds is False
        else:
            with pytest.raises(PolicyRefused):
                check_key_policy(Environment.LIVE, perms)


@pytest.mark.parametrize("symbol,market,inst_id", [
    ("BTCUSDT", "spot", "BTC-USDT"), ("ETHBTC", "spot", "ETH-BTC"), ("SOLUSDC", "spot", "SOL-USDC"),
    ("BTCUSDT", "usdm_perpetual", "BTC-USDT-SWAP"), ("ETHUSDC", "usdm_perpetual", "ETH-USDC-SWAP"),
])
def test_inst_id_mapping_round_trips(symbol, market, inst_id):
    assert X.to_inst_id(symbol, market) == inst_id
    assert X.from_inst_id(inst_id) == (symbol, market)


def test_unmapped_instruments_are_not_guessed():
    assert X.from_inst_id("BTC-USD-SWAP") is None  # inverse (coin-margined) swap
    assert X.from_inst_id("BTC-USD-250627") is None and X.from_inst_id("BTC-USD-250627-60000-C") is None
    with pytest.raises(NotSupported):
        X.to_inst_id("BTCXYZ", "spot")


# GET /api/v5/account/balance doc example, trimmed to the mapped fields.
DOC_BALANCE = {"adjEq": "55415.624719833286", "availEq": "55415.624719833286", "imr": "0", "isoEq": "0",
               "mgnRatio": "", "mmr": "0", "totalEq": "55837.43556134779", "upl": "0", "uTime": "1705474164160",
               "details": [{"availBal": "4834.317093622894", "cashBal": "4850.435693622894", "ccy": "USDT",
                            "eq": "4992.890093622894", "eqUsd": "4991.542013297616", "frozenBal": "158.573",
                            "upl": "-7.545600000000006"}]}
SWAP_POSITION = {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "pos": "-2", "posSide": "net", "avgPx": "62961.4",
                 "markPx": "62891.9", "lever": "5", "mgnMode": "cross", "liqPx": "", "upl": "1.39",
                 "notionalUsd": "1257.8"}
INVERSE_POSITION = {**SWAP_POSITION, "instId": "BTC-USD-SWAP", "pos": "3"}
PENDING = {"instId": "BTC-USDT-SWAP", "ordId": "312269865356374016", "clOrdId": "mat0123456789abcdef", "side": "buy",
           "ordType": "limit", "state": "live", "px": "60000", "sz": "1.5", "accFillSz": "0", "avgPx": "",
           "reduceOnly": "false", "cTime": "1724733617998", "uTime": "1724733617998"}


def test_snapshot_maps_documented_fields_converts_contracts_and_never_invents_values():
    routes = {("GET", "/api/v5/account/balance"): ok([DOC_BALANCE]),
              ("GET", "/api/v5/account/positions"): ok([SWAP_POSITION, INVERSE_POSITION]),
              ("GET", "/api/v5/trade/orders-pending"):
                  lambda p: ok([PENDING] if parse_qs(p.query)["instType"] == ["SWAP"] else []),
              ("GET", "/api/v5/public/instruments"): instruments}
    a, _ = adapter(Environment.TESTNET, routes)
    snap = a.snapshot(CRED, "conn1")
    t = snap.totals[0]
    assert (t.wallet, t.equity, t.available_balance, t.quote_asset) == ("unified", Decimal("55837.43556134779"),
                                                                        Decimal("55415.624719833286"), "USD")
    assert t.margin_ratio is None and t.wallet_balance is None  # "" and not reported
    usdt = snap.balances[0]
    assert (usdt.free, usdt.locked, usdt.total, usdt.usd_value) == (
        Decimal("4834.317093622894"), Decimal("158.573"), Decimal("4992.890093622894"), Decimal("4991.542013297616"))
    [pos] = snap.positions
    assert (pos.symbol, pos.side, pos.quantity, pos.leverage, pos.margin_mode) == (
        "BTCUSDT", "short", Decimal("0.02"), Decimal(5), "cross")  # -2 contracts x 0.01 BTC
    assert pos.liquidation_price is None and pos.provenance == "derived"
    assert any("BTC-USD-SWAP" in w for w in snap.warnings)
    [order] = snap.open_orders
    assert (order.status, order.quantity, order.filled_quantity, order.average_price, order.reduce_only) == (
        OrderStatus.NEW, Decimal("0.015"), Decimal("0"), None, False)


ORDER = OrderRequest(symbol="BTCUSDT", market="spot", side="buy", type="limit", quantity=Decimal("0.00100000"),
                     price=Decimal("60000.00"), client_order_id="mat0123456789abcdef")


def test_live_order_endpoints_are_disabled_before_any_request():
    a, rec = adapter(Environment.LIVE, {})
    with pytest.raises(NotSupported, match="disabled"):
        a.place_order(CRED, ORDER)
    with pytest.raises(NotSupported, match="disabled"):
        a.query_order(CRED, "BTCUSDT", "spot", client_order_id=ORDER.client_order_id)
    with pytest.raises(NotSupported, match="disabled"):
        a.cancel_order(CRED, "BTCUSDT", "spot", client_order_id=ORDER.client_order_id)
    assert rec.calls == []


# GET /api/v5/trade/order doc example: a spot market buy whose sz is 100 USDT (tgtCcy quote_ccy).
DOC_ORDER = {"accFillSz": "0.00192834", "avgPx": "51858", "cTime": "1708587373361", "instId": "BTC-USDT",
             "instType": "SPOT", "ordId": "680800019749904384", "ordType": "market", "reduceOnly": "false",
             "side": "buy", "state": "filled", "sz": "100", "tdMode": "cash", "tgtCcy": "quote_ccy",
             "uTime": "1708587373362", "clOrdId": "mat0123456789abcdef", "px": ""}
DOC_ACK = [{"clOrdId": "mat0123456789abcdef", "ordId": "312269865356374016", "tag": "", "ts": "1695190491421",
            "sCode": "0", "sMsg": ""}]


def test_testnet_spot_orders_send_documented_fields_and_report_the_looked_up_status():
    a, rec = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG]),
                                           ("POST", "/api/v5/trade/order"): ok(DOC_ACK),
                                           ("GET", "/api/v5/trade/order"): ok([DOC_ORDER])})
    market = OrderRequest(symbol="BTCUSDT", market="spot", side="buy", type="market", quantity=Decimal("0.002"),
                          client_order_id="mat0123456789abcdef")
    order = a.place_order(CRED, market)
    assert json.loads(calls(rec, "/api/v5/trade/order")[0]["body"]) == {
        "instId": "BTC-USDT", "side": "buy", "clOrdId": "mat0123456789abcdef", "tdMode": "cash", "sz": "0.002",
        "tgtCcy": "base_ccy", "ordType": "market"}
    assert order.status is OrderStatus.FILLED and order.filled_quantity == Decimal("0.00192834")
    assert order.quantity is None  # doc example sz=100 is USDT, not BTC
    a.place_order(CRED, ORDER)
    body = json.loads([c for c in rec.calls if c["method"] == "POST"][1]["body"])
    assert (body["ordType"], body["px"], body["sz"], body["tdMode"]) == ("limit", "60000", "0.001", "cash")
    with pytest.raises(ExchangeError, match="1-32"):
        a.place_order(CRED, OrderRequest(**{**ORDER.__dict__, "client_order_id": "mat-0001"}))


def test_swap_orders_convert_base_quantity_to_contracts_and_refuse_partial_lots():
    net = {**DOC_CONFIG, "posMode": "net_mode"}
    a, rec = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([net]),
                                           ("GET", "/api/v5/public/instruments"): instruments,
                                           ("POST", "/api/v5/trade/order"): ok(DOC_ACK),
                                           ("GET", "/api/v5/trade/order"): err("51603", "Order does not exist.")})
    swap = OrderRequest(symbol="BTCUSDT", market="usdm_perpetual", side="sell", type="limit", quantity=Decimal("0.015"),
                        price=Decimal("65000"), client_order_id="mat0123456789abcdef", reduce_only=True)
    order = a.place_order(CRED, swap)
    body = json.loads(calls(rec, "/api/v5/trade/order")[0]["body"])
    assert (body["sz"], body["tdMode"], body["reduceOnly"]) == ("1.5", "cross", True)  # 0.015 BTC / 0.01 per contract
    assert order.status is OrderStatus.UNKNOWN and order.order_id == "312269865356374016"  # ack only, not confirmed
    sent = len([c for c in rec.calls if c["method"] == "POST"])
    with pytest.raises(ExchangeError, match="multiple of 0.001"):
        a.place_order(CRED, OrderRequest(**{**swap.__dict__, "quantity": Decimal("0.0155")}))
    assert len([c for c in rec.calls if c["method"] == "POST"]) == sent  # refused before sending
    hedge, _ = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG])})
    with pytest.raises(NotSupported, match="net"):
        hedge.place_order(CRED, swap)


@pytest.mark.parametrize("response", [
    HttpResponse(503, b"", {}),
    err("51149", "Order timed out. Please try again.", 500),
    err("50004", "API endpoint request timeout.", 400),
    err("1", "Operation failed.", 200, [{"sCode": "50001", "sMsg": "Service temporarily unavailable."}]),
])
def test_unknown_execution_status_is_never_reported_as_a_failure(response):
    a, _ = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG]),
                                         ("POST", "/api/v5/trade/order"): response,
                                         ("POST", "/api/v5/trade/cancel-order"): response})
    with pytest.raises(OutcomeUnknown):
        a.place_order(CRED, ORDER)
    with pytest.raises(OutcomeUnknown):
        a.cancel_order(CRED, "BTCUSDT", "spot", client_order_id=ORDER.client_order_id)


def test_definite_rejections_auth_and_rate_limits_map_to_their_errors_and_a_missing_order_is_none():
    reject = err("1", "All operations failed", 200, [{"ordId": "", "clOrdId": "mat0123456789abcdef",
                                                      "sCode": "51008", "sMsg": "Order failed. Insufficient USDT balance"}])
    a, _ = adapter(Environment.TESTNET, {("GET", "/api/v5/account/config"): ok([DOC_CONFIG]),
                                         ("POST", "/api/v5/trade/order"): reject,
                                         ("GET", "/api/v5/trade/order"): err("51603", "Order does not exist.")})
    with pytest.raises(ExchangeError, match="51008") as exc:
        a.place_order(CRED, ORDER)
    assert not isinstance(exc.value, OutcomeUnknown)
    assert a.query_order(CRED, "BTCUSDT", "spot", client_order_id=ORDER.client_order_id) is None
    for response, error in ((err("50113", "Invalid signature.", 401), AuthError),
                            (err("50101", "APIKey does not match current environment.", 401), AuthError),
                            (err("50011", "Too Many Requests", 429), RateLimited)):
        a, _ = adapter(Environment.LIVE, {("GET", "/api/v5/account/config"): response})
        with pytest.raises(error):
            a.permissions(CRED)


def test_symbol_rules_read_instruments_and_scale_swap_contracts_to_base_units():
    a, _ = adapter(Environment.TESTNET, {("GET", "/api/v5/public/instruments"): instruments})
    spot = a.symbol_rules("BTCUSDT", "spot")
    assert (spot.status, spot.price_tick, spot.quantity_step, spot.min_quantity, spot.min_notional) == (
        "TRADING", Decimal("0.1"), Decimal("0.00000001"), Decimal("0.00001"), None)  # no documented min notional
    swap = a.symbol_rules("BTCUSDT", "usdm_perpetual")
    assert (swap.base_asset, swap.quote_asset) == ("BTC", "USDT")
    assert (swap.quantity_step, swap.min_quantity, swap.max_quantity) == (Decimal("0.001"), Decimal("0.001"),
                                                                         Decimal("1000"))  # contracts x 0.01 BTC
    with pytest.raises(ExchangeError, match="does not list"):
        a.symbol_rules("ETHUSDT", "spot")


def test_fee_rates_flip_okx_sign_and_leverage_reads_leverage_info():
    fees = [{"feeGroup": [{"groupId": "1", "maker": "-0.0008", "taker": "-0.001"},
                          {"groupId": "2", "maker": "0.0001", "taker": "-0.0005"}], "instType": "SPOT", "level": "Lv1"}]
    lev = [{"ccy": "", "instId": "BTC-USDT-SWAP", "mgnMode": "cross", "posSide": "net", "lever": "10"}]
    a, rec = adapter(Environment.TESTNET, {("GET", "/api/v5/account/trade-fee"): ok(fees),
                                           ("GET", "/api/v5/public/instruments"): instruments,
                                           ("GET", "/api/v5/account/leverage-info"): ok(lev)})
    assert a.fee_rates(CRED, "BTCUSDT", "spot") == (Decimal("0.0008"), Decimal("0.001"))
    assert a.leverage(CRED, "BTCUSDT", "usdm_perpetual") == Decimal(10)
    assert a.leverage(CRED, "BTCUSDT", "spot") is None
    assert calls(rec, "/api/v5/account/leverage-info")[0]["query"] == {"instId": ["BTC-USDT-SWAP"], "mgnMode": ["cross"]}


BILLS = [{"billId": "1", "ccy": "USDT", "instId": "BTC-USDT-SWAP", "type": "8", "subType": "173", "pnl": "-0.12",
          "balChg": "-0.12", "fee": "0", "ts": "1695033476167"},
         {"billId": "2", "ccy": "USDT", "instId": "BTC-USDT-SWAP", "type": "2", "subType": "5", "pnl": "3.5",
          "balChg": "3.48", "fee": "-0.02", "ts": "1695033476100"},
         {"billId": "3", "ccy": "USDT", "instId": "", "type": "1", "subType": "11", "pnl": "", "balChg": "50",
          "fee": "0", "ts": "1695033476000"}]
DEPOSITS = [{"amt": "1", "ccy": "USDT", "chain": "USDT-TRC20", "depId": "88****33", "state": "2",
             "ts": "1674038705000", "txId": "fee235b3e812********857d36bb0426917f0df1802"}]
WITHDRAWALS = [{"chain": "ETH-Ethereum", "fee": "0.007", "feeCcy": "ETH", "ccy": "ETH", "amt": "0.029809",
                "txId": "0x35c******b360a174d", "state": "2", "ts": "1655251200000", "wdId": "15447421"}]


def test_cashflows_map_bills_and_include_deposits_and_withdrawals_only_on_live():
    routes = {("GET", "/api/v5/account/bills"): ok(BILLS), ("GET", "/api/v5/asset/deposit-history"): ok(DEPOSITS),
              ("GET", "/api/v5/asset/withdrawal-history"): ok(WITHDRAWALS)}
    live, _ = adapter(Environment.LIVE, routes)
    flows = [(c.kind, c.amount, c.symbol) for c in live.cashflows(CRED)]
    assert ("funding_fee", Decimal("-0.12"), "BTCUSDT") in flows
    assert ("commission", Decimal("-0.02"), "BTCUSDT") in flows and ("realized_pnl", Decimal("3.5"), "BTCUSDT") in flows
    assert ("internal_transfer", Decimal("50"), None) in flows
    assert ("deposit", Decimal("1"), None) in flows and ("withdrawal", Decimal("-0.029809"), None) in flows
    demo, rec = adapter(Environment.TESTNET, routes)
    assert {c.kind for c in demo.cashflows(CRED)} == {"funding_fee", "commission", "realized_pnl", "internal_transfer"}
    assert not any(c["path"].startswith("/api/v5/asset/") for c in rec.calls)
