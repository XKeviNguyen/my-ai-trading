"""R8 M3: testnet Trading Mode — preview, confirm, journal-before-send, reconcile, never resend."""
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fakes import make_exchange
from my_ai_trading.accounts.adapters.base import (
    ExchangeAdapter, ExchangeError, ExchangeInfo, OrderRequest, OutcomeUnknown, SymbolRules,
)
from my_ai_trading.accounts.credentials import Credential, CredentialStore, Lifetime, MemoryVault, Secret
from my_ai_trading.accounts.models import (
    AccountSnapshot, AccountTotals, Balance, Environment, KeyPermissions, Order, OrderStatus, Position, WalletType,
)
from my_ai_trading.accounts.service import AccountService
from my_ai_trading.accounts.trading import CONSENT_TEXT, OrderDraft, TradingRefused, TradingService
from my_ai_trading.api import create_app
from my_ai_trading.copilot import Copilot
from my_ai_trading.store import Store

TRADER = KeyPermissions(read=True, spot_trade=True, derivatives_trade=True, margin_trade=False, withdraw=False,
                        transfer=False)


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


class SimExchange(ExchangeAdapter):
    """A tiny testnet: remembers orders by client ID; `mode` picks how the next send behaves."""
    INFO = ExchangeInfo(id="sim", name="Sim", kind="crypto_exchange",
                        environments=(Environment.LIVE, Environment.TESTNET), needs_passphrase=False,
                        markets=("spot", "usdm_perpetual"), key_setup_url="https://example.invalid")
    book: dict = {}
    sends: list = []
    mode = "ok"
    journal_seen: list = []
    store = None

    def permissions(self, cred):
        return TRADER

    def symbol_rules(self, symbol, market):
        return SymbolRules(symbol=symbol, market=market, base_asset="BTC", quote_asset="USDT", status="TRADING",
                           price_tick=Decimal("0.01"), quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
                           max_quantity=Decimal("100"), min_notional=Decimal("5"), order_types=("limit", "market"))

    def reference_price(self, symbol, market):
        return Decimal("100")

    def fee_rates(self, cred, symbol, market):
        return Decimal("0.001"), Decimal("0.002")

    def leverage(self, cred, symbol, market):
        return Decimal("5")

    def snapshot(self, cred, connection_id):
        return AccountSnapshot(connection_id=connection_id, exchange="sim", environment=self.environment, as_of_ms=1,
                               totals=[AccountTotals(wallet=WalletType.FUTURES_USDM, available_balance=Decimal("50"))],
                               balances=[Balance(asset="USDT", wallet=WalletType.SPOT, free=Decimal("1000"))],
                               positions=[Position(symbol="BTCUSDT", market="usdm_perpetual", side="long",
                                                   quantity=Decimal("0.5"))])

    def place_order(self, cred, order: OrderRequest):
        assert self.environment is Environment.TESTNET
        SimExchange.sends.append(order.client_order_id)
        if SimExchange.store is not None:  # the journal row must exist before the send
            SimExchange.journal_seen.append(SimExchange.store.journal("local", client_order_id=order.client_order_id))
        o = Order(order_id=str(len(self.sends)), client_order_id=order.client_order_id, symbol=order.symbol,
                  market=order.market, side=order.side, type=order.type, status=OrderStatus.NEW,
                  quantity=order.quantity, price=order.price, filled_quantity=Decimal(0))
        if self.mode == "reject":
            raise ExchangeError("Sim refused the request (code -2010: insufficient balance)")
        if self.mode in ("ok", "timeout_after_accept"):
            SimExchange.book[order.client_order_id] = o
        if self.mode.startswith("timeout"):
            raise OutcomeUnknown("timed out")
        return o

    def query_order(self, cred, symbol, market, *, client_order_id):
        return SimExchange.book.get(client_order_id)

    def cancel_order(self, cred, symbol, market, *, client_order_id, exchange_order_id=None):
        o = SimExchange.book[client_order_id].model_copy(update={"status": OrderStatus.CANCELED})
        SimExchange.book[client_order_id] = o
        return o


@pytest.fixture()
def world():
    SimExchange.book, SimExchange.sends, SimExchange.mode, SimExchange.journal_seen = {}, [], "ok", []
    wall, mono = Clock(), Clock()
    store = Store()
    SimExchange.store = store
    accounts = AccountService(CredentialStore(store, persistent_vault=MemoryVault, clock=wall),
                              adapter_factory=lambda ex, env: SimExchange(env), clock=wall)
    trading = TradingService(accounts, store, clock=mono)
    cred = Credential(Secret("sim-key-0000"), Secret("sim-secret-0000"))

    def connect(env=Environment.TESTNET, lifetime=Lifetime.SESSION):
        return accounts.connect(user="local", exchange="sim", environment=env, credential=cred, lifetime=lifetime,
                                label="sim").connection_id
    yield accounts, trading, store, connect, wall, mono
    SimExchange.store = None


DRAFT = OrderDraft(symbol="BTCUSDT", market="spot", side="buy", type="limit", quantity=Decimal("0.1"),
                   price=Decimal("99.50"))


def enable(trading, cid):
    trading.set_mode(cid, user="local", enabled=True, consent=CONSENT_TEXT)


def test_trading_mode_is_off_by_default_testnet_only_and_needs_exact_consent(world):
    accounts, trading, _, connect, *_ = world
    cid = connect()
    with pytest.raises(TradingRefused, match="Trading Mode is off"):
        trading.preview(cid, DRAFT, user="local")
    with pytest.raises(TradingRefused, match="consent"):
        trading.set_mode(cid, user="local", enabled=True, consent="yes")
    enable(trading, cid)
    trading.preview(cid, DRAFT, user="local")
    trading.set_mode(cid, user="local", enabled=False, consent=None)
    with pytest.raises(TradingRefused, match="Trading Mode is off"):
        trading.preview(cid, DRAFT, user="local")


def test_live_connections_can_never_enable_trading(world):
    accounts, trading, store, _, *_ = world
    meta = accounts.credentials.save(user="local", exchange="sim", environment=Environment.LIVE, label="live",
                                     lifetime=Lifetime.SESSION, permissions=TRADER,
                                     credential=Credential(Secret("live-key-0000"), Secret("live-secret-00")))
    with pytest.raises(TradingRefused, match="testnet"):
        trading.set_mode(meta.connection_id, user="local", enabled=True, consent=CONSENT_TEXT)
    # even if the flag were forced on in storage, orders still refuse a live connection
    store.save_connection(meta.model_copy(update={"trading_enabled": True}))
    with pytest.raises(TradingRefused, match="testnet"):
        trading.preview(meta.connection_id, DRAFT, user="local")


def test_preview_refuses_rule_breaks_with_nearest_valid_values_and_never_rounds(world):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    with pytest.raises(TradingRefused, match=r"nearest valid: 99\.5 or 99\.51"):
        trading.preview(cid, DRAFT.model_copy(update={"price": Decimal("99.505")}), user="local")
    with pytest.raises(TradingRefused, match="minimum 5"):
        trading.preview(cid, DRAFT.model_copy(update={"quantity": Decimal("0.01")}), user="local")
    with pytest.raises(TradingRefused, match="needs a price"):
        trading.preview(cid, DRAFT.model_copy(update={"price": None}), user="local")


def test_preview_shows_estimates_with_provenance_and_futures_risk(world):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    pub = p.public()
    assert pub["estimates"]["notional"] == {"value": "9.95", "asset": "USDT", "provenance": "derived"}
    assert pub["estimates"]["fee"]["provenance"] == "estimate" and pub["estimates"]["fee"]["rate"] == "0.001"
    assert pub["order"]["client_order_id"].startswith("mat") and len(token) >= 20
    fut = OrderDraft(symbol="BTCUSDT", market="usdm_perpetual", side="sell", type="market", quantity=Decimal("3"),
                     reduce_only=True)
    pf, _ = trading.preview(cid, fut, user="local")
    est = pf.public()["estimates"]
    assert est["initial_margin"]["value"] == "60" and est["liquidation_price"]["value"] is None
    assert est["position_after"]["side"] == "flat"  # reduce-only cannot flip a 0.5 long into a short
    assert any("exceeds available balance" in w for w in pf.warnings)


def test_submit_sends_once_journals_first_and_refuses_replay_and_wrong_tokens(world):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    out = trading.submit(p.preview_id, token, user="local")
    assert out["state"] == "NEW" and "does not mean it filled" in out["note"]
    assert SimExchange.sends == [p.request.client_order_id]
    assert SimExchange.journal_seen[0][0]["state"] == "SENDING"  # written before the send
    assert store.journal("local")[0]["state"] == "NEW"
    with pytest.raises(TradingRefused, match="already used"):
        trading.submit(p.preview_id, token, user="local")
    p2, _ = trading.preview(cid, DRAFT, user="local")
    with pytest.raises(TradingRefused, match="does not match"):
        trading.submit(p2.preview_id, "x" * 32, user="local")
    assert len(SimExchange.sends) == 1


def test_confirmation_expires_after_60_seconds(world):
    _, trading, _, connect, _, mono = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    mono.t += 60.0
    with pytest.raises(TradingRefused, match="expired"):
        trading.submit(p.preview_id, token, user="local")
    assert SimExchange.sends == []


def test_concurrent_double_submit_sends_exactly_one_order(world):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            results.append(trading.submit(p.preview_id, token, user="local")["state"])
        except TradingRefused:
            results.append("refused")
    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["NEW"] + ["refused"] * 7
    assert len(SimExchange.sends) == 1


@pytest.mark.parametrize("mode,state", [("timeout_after_accept", "NEW"), ("timeout_lost", "UNKNOWN")])
def test_unknown_outcome_is_reconciled_and_never_resent(world, mode, state):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    SimExchange.mode = mode
    p, token = trading.preview(cid, DRAFT, user="local")
    out = trading.submit(p.preview_id, token, user="local")
    assert out["state"] == state and "instead of resent" in out["note"]
    assert len(SimExchange.sends) == 1
    assert store.journal("local")[0]["state"] == state


def test_a_definite_rejection_is_journaled_and_reported(world):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    SimExchange.mode = "reject"
    p, token = trading.preview(cid, DRAFT, user="local")
    with pytest.raises(TradingRefused, match="-2010"):
        trading.submit(p.preview_id, token, user="local")
    assert store.journal("local")[0]["state"] == "REJECTED"


def test_an_order_left_sending_by_a_crash_stays_open_until_the_user_resolves_it(world):
    # Audit FLOW-1/2 (concern C): repeated empty lookups are never proof the order was not executed.
    _, trading, store, connect, *_ = world
    cid = connect()
    coid = "mat" + "0" * 16
    store.journal_insert({"client_order_id": coid, "user_id": "local", "connection_id": cid, "exchange": "sim",
                          "environment": "testnet", "state": "SENDING", "created_at": 1,  # long ago
                          "request_json": '{"symbol": "BTCUSDT", "market": "spot"}'})
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]
    results = [trading.reconcile(coid, user="local") for _ in range(5)]
    assert [r["state"] for r in results] == ["SENDING"] * 5 and all(r["verification"] == "not_listed" for r in results)
    assert all("place a new order" not in r["note"] and "Do not place it again" in r["note"] for r in results)
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]  # still tracked
    assert trading.resolve(coid, user="local")["state"] == "USER_RESOLVED"  # only by explicit user action
    assert trading.needs_reconcile(user="local") == []


def test_a_fresh_unknown_order_is_never_given_up_within_a_minute(world):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    SimExchange.mode = "timeout_lost"
    p, token = trading.preview(cid, DRAFT, user="local")
    trading.submit(p.preview_id, token, user="local")
    coid = p.request.client_order_id
    states = [trading.reconcile(coid, user="local")["state"] for _ in range(5)]
    assert states == ["UNKNOWN"] * 5  # many lookups, but the order is seconds old
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]
    SimExchange.book[coid] = SimExchange.book.get(coid) or Order(  # the order shows up late after all
        order_id="9", client_order_id=coid, symbol="BTCUSDT", market="spot", side="buy", type="limit",
        status=OrderStatus.NEW)
    assert trading.reconcile(coid, user="local")["state"] == "NEW"
    assert len(SimExchange.sends) == 1


def test_a_final_state_is_never_downgraded_when_the_exchange_purges_old_orders(world):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    coid = trading.submit(p.preview_id, token, user="local")["client_order_id"]
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"
    del SimExchange.book[coid]  # the exchange forgot the old canceled order
    assert trading.reconcile(coid, user="local")["state"] == "CANCELED"
    assert store.journal("local")[0]["state"] == "CANCELED"


def test_cancel_still_works_after_trading_mode_is_turned_off(world):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    coid = trading.submit(p.preview_id, token, user="local")["client_order_id"]
    trading.set_mode(cid, user="local", enabled=False, consent=None)
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"


def test_an_unreachable_exchange_on_send_is_looked_up_not_reported_as_rejected(world):
    from my_ai_trading.accounts.adapters.base import ExchangeUnavailable
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    original = SimExchange.place_order

    def flaky(self, cred, order):
        SimExchange.sends.append(order.client_order_id)
        raise ExchangeUnavailable("Sim is unavailable (HTTP 302)")
    SimExchange.place_order = flaky
    try:
        p, token = trading.preview(cid, DRAFT, user="local")
        assert trading.submit(p.preview_id, token, user="local")["state"] == "UNKNOWN"
    finally:
        SimExchange.place_order = original
    assert store.journal("local")[0]["state"] == "UNKNOWN" and len(SimExchange.sends) == 1


def test_credentials_that_expire_between_preview_and_submit_stop_the_order(world):
    _, trading, _, connect, wall, _ = world
    cid = connect(lifetime=Lifetime.HOUR_1)
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    wall.t += 3600
    from my_ai_trading.accounts.credentials import CredentialExpired
    with pytest.raises(CredentialExpired):
        trading.submit(p.preview_id, token, user="local")
    assert SimExchange.sends == []


def test_http_flow_cancel_needs_confirmation_and_web_pages_are_blocked(world):
    accounts, trading, store, connect, *_ = world
    _, svc = make_exchange()
    token = "k" * 40
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=token, accounts=accounts)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    H = {"Authorization": f"Bearer {token}"}
    cid = connect()
    consent = c.get("/api/v1/trading/consent", headers=H).json()["consent_text"]
    assert c.put("/api/v1/trading/mode", json={"connection_id": cid, "enabled": True, "consent": consent},
                 headers=H).status_code == 200
    body = {"connection_id": cid, "draft": {"symbol": "BTCUSDT", "market": "spot", "side": "buy", "type": "limit",
                                            "quantity": "0.1", "price": "99.50"}}
    pv = c.post("/api/v1/trading/preview", json=body, headers=H)
    assert pv.status_code == 200, pv.text
    sent = c.post("/api/v1/trading/orders", json={"preview_id": pv.json()["preview_id"],
                                                 "confirm_token": pv.json()["confirm_token"]}, headers=H)
    assert sent.status_code == 200 and sent.json()["state"] == "NEW"
    coid = sent.json()["client_order_id"]
    assert c.post(f"/api/v1/trading/orders/{coid}/cancel", json={"confirm": False}, headers=H).status_code == 409
    assert c.post(f"/api/v1/trading/orders/{coid}/cancel", json={"confirm": True}, headers=H).json()["state"] == "CANCELED"
    assert c.post("/api/v1/trading/preview", json=body, headers={**H, "Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/v1/trading/orders", headers=H).json()["orders"][0]["state"] == "CANCELED"


def test_stock_drafts_accept_share_class_symbols_and_reject_junk():
    d = OrderDraft(symbol="BRK.B", market="stock", side="buy", type="limit", quantity=Decimal("1"), price=Decimal("450"))
    assert d.symbol == "BRK.B"
    for bad in (".BRK", "brk.b", "A B", "X" * 31):
        with pytest.raises(ValueError):
            OrderDraft(symbol=bad, market="stock", side="buy", type="market", quantity=Decimal("1"))


def test_exchange_catalog_carries_provider_specific_credential_labels():
    from my_ai_trading.accounts.service import AccountService as S
    by_id = {e["id"]: e for e in S.exchanges()}
    assert (by_id["binance"]["key_label"], by_id["binance"]["secret_label"]) == ("API key", "API secret")
    assert (by_id["ibkr"]["key_label"], by_id["ibkr"]["secret_label"]) == ("Paper account number", "Paper username")
    assert by_id["ibkr"]["environments"] == ["testnet"] and by_id["ibkr"]["oauth"] == "not_offered"


def test_balance_warnings_also_read_unified_accounts(world):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    unified = AccountSnapshot(connection_id=cid, exchange="sim", environment=Environment.TESTNET, as_of_ms=1,
                              totals=[AccountTotals(wallet=WalletType.UNIFIED, available_balance=Decimal("10"))],
                              balances=[Balance(asset="USDT", wallet=WalletType.UNIFIED, free=Decimal("5"))])
    original = SimExchange.snapshot
    SimExchange.snapshot = lambda self, cred, connection_id: unified
    try:
        spot, _ = trading.preview(cid, DRAFT, user="local")  # 9.95 USDT order vs 5 USDT free
        assert any("exceeds free USDT balance 5" in w for w in spot.warnings)
        fut, _ = trading.preview(cid, OrderDraft(symbol="BTCUSDT", market="usdm_perpetual", side="buy",
                                                 type="market", quantity=Decimal("1")), user="local")
        assert any("exceeds available balance 10" in w for w in fut.warnings)  # 100 / 5x = 20 margin
    finally:
        SimExchange.snapshot = original


def test_a_stale_status_never_overwrites_a_final_state_even_when_interleaved(world):
    _, trading, store, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    p, token = trading.preview(cid, DRAFT, user="local")
    coid = trading.submit(p.preview_id, token, user="local")["client_order_id"]
    stale = SimExchange.book[coid]  # the status check reads NEW from the exchange ...
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"  # ... a cancel lands meanwhile ...
    SimExchange.book[coid] = stale  # ... and the late status check now writes its older NEW result
    assert trading.reconcile(coid, user="local")["state"] == "CANCELED"
    assert store.journal("local")[0]["state"] == "CANCELED"
    assert store.journal_update(coid, state="NEW") == "CANCELED"  # the guard lives in the SQL itself
    assert store.journal_update(coid, state="FILLED") == "FILLED"  # final -> final (exchange truth) is allowed


def test_a_rule_breaking_preview_makes_no_signed_calls(world, monkeypatch):
    _, trading, _, connect, *_ = world
    cid = connect()
    enable(trading, cid)
    signed = []
    for name in ("fee_rates", "leverage", "snapshot"):
        monkeypatch.setattr(SimExchange, name, lambda self, *a, n=name: signed.append(n))
    with pytest.raises(TradingRefused, match="minimum 5"):
        trading.preview(cid, DRAFT.model_copy(update={"quantity": Decimal("0.01")}), user="local")
    assert signed == []  # SIMP-2: fees, leverage and the account snapshot were fetched before refusing
