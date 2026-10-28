"""Audit 2026-10-27 regressions: order reconciliation never turns "not listed" into "never executed".

FLOW-1, FLOW-2, FLOW-3, FLOW-4/ENV-1, FLOW-5, FLOW-6, FLOW-12/SEC-7. Deterministic: injected wall clock,
fake adapters with hook points, threading barriers; no sleeps.
"""
import threading
from decimal import Decimal

import pytest

from my_ai_trading.accounts.adapters.base import ExchangeAdapter, ExchangeInfo, SymbolRules
from my_ai_trading.accounts.credentials import Credential, CredentialStore, Lifetime, MemoryVault, Secret
from my_ai_trading.accounts.models import AccountSnapshot, Environment, KeyPermissions, Order, OrderStatus
from my_ai_trading.accounts.service import AccountService
from my_ai_trading.accounts.trading import CONSENT_TEXT, MISS_SPACING_MS, OrderDraft, TradingRefused, TradingService
from my_ai_trading.store import Store

TRADER = KeyPermissions(read=True, spot_trade=True, derivatives_trade=False, margin_trade=False, withdraw=False,
                        transfer=False)
DRAFT = OrderDraft(symbol="BTCUSDT", market="spot", side="buy", type="limit", quantity=Decimal("0.1"),
                   price=Decimal("99.50"))


class Ex(ExchangeAdapter):
    INFO = ExchangeInfo(id="sim", name="Sim", kind="crypto_exchange", environments=(Environment.TESTNET,),
                        needs_passphrase=False, markets=("spot",), key_setup_url="x")
    listed: dict = {}            # what query_order can see
    ack_status = OrderStatus.UNKNOWN
    sends: list = []
    on_query = None              # hook: called inside query_order before it answers
    on_place = None

    def permissions(self, cred):
        return TRADER

    def symbol_rules(self, symbol, market):
        return SymbolRules(symbol=symbol, market=market, base_asset="BTC", quote_asset="USDT", status="TRADING",
                           price_tick=Decimal("0.01"), quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
                           min_notional=Decimal("5"), order_types=("limit",))

    def reference_price(self, symbol, market):
        return Decimal("100")

    def snapshot(self, cred, cid):
        return AccountSnapshot(connection_id=cid, exchange="sim", environment=self.environment, as_of_ms=1)

    def place_order(self, cred, o):
        if Ex.on_place:
            Ex.on_place()
        Ex.sends.append(o.client_order_id)
        # an acknowledgement with the exchange's own id, but (like Bybit/OKX async acks) not yet queryable
        return Order(order_id="1234567", client_order_id=o.client_order_id, symbol=o.symbol, market=o.market,
                     side=o.side, type=o.type, status=Ex.ack_status, quantity=o.quantity)

    def query_order(self, cred, symbol, market, *, client_order_id):
        answer = Ex.listed.get(client_order_id)
        if Ex.on_query:
            Ex.on_query()
        return answer

    def cancel_order(self, cred, symbol, market, *, client_order_id, exchange_order_id=None):
        return Order(order_id="1234567", client_order_id=client_order_id, symbol=symbol, market=market, side="buy",
                     type="limit", status=OrderStatus.CANCELED)


class Wall:
    def __init__(self):
        self.ms = 1_800_000_000_000

    def __call__(self):
        return self.ms


@pytest.fixture()
def env():
    Ex.listed, Ex.ack_status, Ex.sends, Ex.on_query, Ex.on_place = {}, OrderStatus.UNKNOWN, [], None, None
    store, wall = Store(), Wall()
    cred_clock = lambda: wall.ms / 1000  # noqa: E731
    accounts = AccountService(CredentialStore(store, persistent_vault=MemoryVault, clock=cred_clock),
                              adapter_factory=lambda ex, e: Ex(e), clock=cred_clock)
    trading = TradingService(accounts, store, wall_ms=wall)

    def connect(lifetime=Lifetime.SESSION):
        cid = accounts.connect(user="local", exchange="sim", environment=Environment.TESTNET, lifetime=lifetime,
                               label="sim", credential=Credential(Secret("k-000000"), Secret("s-000000"))).connection_id
        trading.set_mode(cid, user="local", enabled=True, consent=CONSENT_TEXT)
        return cid

    def submit(cid):
        p, tok = trading.preview(cid, DRAFT, user="local")
        return trading.submit(p.preview_id, tok, user="local")["client_order_id"]
    return store, wall, accounts, trading, connect, submit


def miss_many(trading, wall, coid, n=5):
    out = []
    for _ in range(n):
        wall.ms += MISS_SPACING_MS
        out.append(trading.reconcile(coid, user="local"))
    return out


def test_flow1_an_acknowledged_order_is_never_declared_missing(env):
    store, wall, _, trading, connect, submit = env
    coid = submit(connect())
    row = store.journal_row(coid)
    assert row["acknowledged"] == 1 and row["exchange_order_id"] == "1234567"
    wall.ms += 120_000
    results = miss_many(trading, wall, coid)
    assert {r["state"] for r in results} == {"UNKNOWN"} and {r["verification"] for r in results} == {"not_listed"}
    assert all("Do not place it again" in r["note"] and "place a new order" not in r["note"] for r in results)
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]
    with pytest.raises(TradingRefused, match="acknowledged"):
        trading.resolve(coid, user="local")  # its fate comes from the exchange, never from a local guess


def test_flow2_an_exchange_reported_state_is_never_downgraded_by_a_later_absence(env):
    store, wall, _, trading, connect, submit = env
    Ex.ack_status = OrderStatus.NEW
    coid = submit(connect())
    wall.ms += 3_600_000  # e.g. the IBKR gateway started a new session and no longer lists it
    assert {r["state"] for r in miss_many(trading, wall, coid)} == {"NEW"}
    assert store.journal_row(coid)["state"] == "NEW"


def test_flow2_misses_are_spaced_and_reset_when_the_order_is_seen(env):
    store, wall, _, trading, connect, submit = env
    coid = submit(connect())
    wall.ms += 1  # lookups start after the acknowledgement
    for _ in range(4):  # same instant: one miss, not four
        trading.reconcile(coid, user="local")
    assert store.journal_row(coid)["lookups"] == 1
    wall.ms += MISS_SPACING_MS - 1
    trading.reconcile(coid, user="local")
    assert store.journal_row(coid)["lookups"] == 1
    wall.ms += 1
    trading.reconcile(coid, user="local")
    assert store.journal_row(coid)["lookups"] == 2
    Ex.listed[coid] = Order(order_id="1234567", client_order_id=coid, symbol="BTCUSDT", market="spot", side="buy",
                            type="limit", status=OrderStatus.NEW)
    assert trading.reconcile(coid, user="local")["state"] == "NEW"
    assert store.journal_row(coid)["lookups"] == 0


def test_flow3_a_late_not_listed_result_never_overwrites_a_newer_confirmation(env):
    store, wall, _, trading, connect, submit = env
    cid = connect()
    coid = submit(cid)
    inside, release = threading.Event(), threading.Event()
    Ex.on_query = lambda: (inside.set(), release.wait(5))  # the lookup has read "not listed" and is paused
    result = {}
    t = threading.Thread(target=lambda: result.update(trading.reconcile(coid, user="local")))
    t.start()
    assert inside.wait(5)
    Ex.on_query = None
    wall.ms += 1
    Ex.listed[coid] = Order(order_id="1234567", client_order_id=coid, symbol="BTCUSDT", market="spot", side="buy",
                            type="limit", status=OrderStatus.NEW)
    assert trading.reconcile(coid, user="local")["state"] == "NEW"  # a newer lookup confirms NEW
    release.set()
    t.join(5)
    row = store.journal_row(coid)
    assert row["state"] == "NEW" and row["verification"] == "confirmed"  # the late miss changed nothing


def test_flow3_uncertain_orders_can_still_be_canceled(env):
    _, wall, _, trading, connect, submit = env
    coid = submit(connect())
    wall.ms += 1  # a lookup in the same millisecond as the acknowledgement is ignored, and reports "confirmed"
    assert trading.reconcile(coid, user="local")["verification"] == "not_listed"
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"


@pytest.mark.parametrize("how", ["expired", "restart"])
def test_flow4_an_unsettled_order_survives_its_connection(env, how):
    store, wall, accounts, trading, connect, submit = env
    cid = connect(Lifetime.HOUR_1 if how == "expired" else Lifetime.SESSION)
    coid = submit(cid)
    if how == "expired":
        wall.ms += 3_600_001
    else:
        accounts.credentials.purge_on_start()  # the app restarted: session connections are gone
    with pytest.raises(TradingRefused, match="the order record is kept"):
        trading.reconcile(coid, user="local")
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]
    connect()  # the user connects a key for the same exchange and environment again
    r = trading.reconcile(coid, user="local")
    assert "current sim testnet connection" in r["note"] and r["state"] == "UNKNOWN"
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"


def test_flow5_set_mode_never_resurrects_a_disconnected_connection(env):
    store, wall, accounts, trading, connect, submit = env
    cid = connect()
    accounts.disconnect(cid, user="local")
    with pytest.raises(Exception):
        trading.set_mode(cid, user="local", enabled=True, consent=CONSENT_TEXT)
    assert store.all_connections() == []
    # a pending vault deletion is not undone either
    cid = connect()
    meta = store.connection(cid, "local")
    store.save_connection(meta.model_copy(update={"purge_pending": True}))
    with pytest.raises(Exception):
        trading.set_mode(cid, user="local", enabled=False, consent=None)
    assert store.connection(cid, "local").purge_pending is True


def test_flow6_a_mode_change_invalidates_previews_and_is_rechecked_before_sending(env):
    store, wall, _, trading, connect, submit = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    trading.set_mode(cid, user="local", enabled=False, consent=None)
    trading.set_mode(cid, user="local", enabled=True, consent=CONSENT_TEXT)  # off and on again
    with pytest.raises(TradingRefused):
        trading.submit(p.preview_id, tok, user="local")
    assert Ex.sends == []


def test_flow12_a_wrong_token_or_user_does_not_burn_the_preview(env):
    _, _, _, trading, connect, _ = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    with pytest.raises(TradingRefused, match="does not match"):
        trading.submit(p.preview_id, "x" * 32, user="local")
    with pytest.raises(TradingRefused, match="unknown or already used"):
        trading.submit(p.preview_id, tok, user="someone-else")
    assert trading.submit(p.preview_id, tok, user="local")["state"] == "UNKNOWN"
    assert len(Ex.sends) == 1


def test_older_not_found_rows_are_open_again_after_upgrade(env):
    store, _, _, trading, connect, _ = env
    cid = connect()
    store.journal_insert({"client_order_id": "mat" + "1" * 16, "user_id": "local", "connection_id": cid,
                          "exchange": "sim", "environment": "testnet", "state": "NOT_FOUND",
                          "request_json": '{"symbol": "BTCUSDT", "market": "spot"}'})
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == ["mat" + "1" * 16]


def test_migration_adds_columns_and_marks_old_acknowledged_rows(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE trading_orders (client_order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
      connection_id TEXT NOT NULL, exchange TEXT NOT NULL, environment TEXT NOT NULL, request_json TEXT NOT NULL,
      state TEXT NOT NULL, order_json TEXT, message TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      lookups INTEGER NOT NULL DEFAULT 0);
      INSERT INTO trading_orders VALUES ('matold','local','c','sim','testnet','{}','NEW','{"order_id":"9"}',NULL,1,1,0);""")
    con.commit()
    con.close()
    row = Store(db).journal_row("matold")
    assert row["acknowledged"] == 1 and "verification" in row


def test_resolve_route_needs_confirmation(env):
    from fastapi.testclient import TestClient
    from fakes import make_exchange
    from my_ai_trading.api import create_app
    from my_ai_trading.copilot import Copilot
    store, _, accounts, _, connect, _ = env
    app = create_app(service=make_exchange()[1], store=store, copilot=Copilot(store, None), api_token="r" * 40,
                     accounts=accounts)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    H = {"Authorization": "Bearer " + "r" * 40}
    cid = connect()
    store.journal_insert({"client_order_id": "mat" + "2" * 16, "user_id": "local", "connection_id": cid,
                          "exchange": "sim", "environment": "testnet", "state": "UNKNOWN",
                          "request_json": '{"symbol": "BTCUSDT", "market": "spot"}'})
    assert c.post(f"/api/v1/trading/orders/mat{'2' * 16}/resolve", json={"confirm": False}, headers=H).status_code == 409
    assert c.post(f"/api/v1/trading/orders/mat{'2' * 16}/resolve", json={"confirm": True},
                  headers=H).json()["state"] == "USER_RESOLVED"


# --- Re-audit 2026-10-27 (flow and security breakers): N1, N2, N3, N5, FLOW-6 branches ---

def _send_in_background(trading, cid, release):
    """Start a submit whose place_order blocks until `release` is set; returns (thread, result box, started)."""
    started, box = threading.Event(), {}

    def blocked():
        started.set()
        release.wait(5)
    Ex.on_place = blocked
    p, tok = trading.preview(cid, DRAFT, user="local")

    def run():
        box["result"] = trading.submit(p.preview_id, tok, user="local")
    t = threading.Thread(target=run)
    t.start()
    assert started.wait(5)
    return t, box, "mat" + p.preview_id


def test_n1_an_order_cannot_be_resolved_while_its_send_is_in_flight(env):
    store, wall, _, trading, connect, _ = env
    Ex.ack_status = OrderStatus.NEW
    release = threading.Event()
    t, box, coid = _send_in_background(trading, connect(), release)
    with pytest.raises(TradingRefused, match="being sent"):
        trading.resolve(coid, user="local")
    release.set()
    t.join(5)
    assert box["result"]["state"] == "NEW" and store.journal_row(coid)["state"] == "NEW"
    assert [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]


def test_n1_an_acknowledgement_between_the_check_and_the_write_is_never_resolved(env):
    store, wall, _, trading, connect, submit = env
    coid = submit(connect())
    store._x("UPDATE trading_orders SET acknowledged=0, state='UNKNOWN' WHERE client_order_id=?", (coid,))
    real_resolve = store.journal_resolve

    def ack_first(c, **kw):  # the exchange acknowledges just after resolve() read the row
        store._x("UPDATE trading_orders SET acknowledged=1 WHERE client_order_id=?", (c,))
        return real_resolve(c, **kw)
    store.journal_resolve = ack_first
    with pytest.raises(TradingRefused, match="never-acknowledged"):
        trading.resolve(coid, user="local")
    assert store.journal_row(coid)["state"] == "UNKNOWN"


def test_n1_an_exchange_report_replaces_a_user_resolved_state(env):
    store, wall, _, trading, connect, submit = env
    coid = submit(connect())
    store._x("UPDATE trading_orders SET acknowledged=0 WHERE client_order_id=?", (coid,))
    assert trading.resolve(coid, user="local")["state"] == "USER_RESOLVED"
    wall.ms += 1
    Ex.listed[coid] = Order(order_id="1234567", client_order_id=coid, symbol="BTCUSDT", market="spot", side="buy",
                            type="limit", status=OrderStatus.NEW, quantity=Decimal("0.1"))
    assert trading.reconcile(coid, user="local")["state"] == "NEW"  # live at the exchange: tracked and cancelable
    assert trading.cancel(coid, user="local")["state"] == "CANCELED"


def test_n2_the_fallback_connection_must_use_the_same_key(env):
    store, wall, accounts, trading, connect, submit = env
    cid = connect()
    coid = submit(cid)
    accounts.disconnect(cid, user="local")
    other = accounts.connect(user="local", exchange="sim", environment=Environment.TESTNET, lifetime=Lifetime.SESSION,
                             label="other account", credential=Credential(Secret("k-111111"), Secret("s-111111")))
    with pytest.raises(TradingRefused, match="same sim testnet key"):
        trading.reconcile(coid, user="local")  # the other account would report it as missing
    accounts.connect(user="local", exchange="sim", environment=Environment.TESTNET, lifetime=Lifetime.SESSION,
                     label="same key again", credential=Credential(Secret("k-000000"), Secret("s-000000")))
    assert "checked through your current" in trading.reconcile(coid, user="local")["note"]
    assert other.connection_id


def test_n3_an_older_answer_never_moves_the_state_back(env):
    store, wall, _, trading, connect, submit = env
    Ex.ack_status = OrderStatus.PARTIALLY_FILLED
    coid = submit(connect())
    wall.ms += 1
    Ex.listed[coid] = Order(order_id="1234567", client_order_id=coid, symbol="BTCUSDT", market="spot", side="buy",
                            type="limit", status=OrderStatus.NEW, quantity=Decimal("0.1"))  # a stale view
    assert trading.reconcile(coid, user="local")["state"] == "PARTIALLY_FILLED"


def test_n3_a_send_timeout_does_not_overwrite_a_state_found_meanwhile(env, monkeypatch):
    from my_ai_trading.accounts.adapters.base import OutcomeUnknown
    store, wall, _, trading, connect, _ = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    coid = "mat" + p.preview_id

    def found_then_timeout(self, cred, o):
        store.journal_seen(coid, state="NEW", order_json="{}", exchange_order_id="1", now_ms=wall.ms)
        raise OutcomeUnknown("timed out")
    monkeypatch.setattr(Ex, "place_order", found_then_timeout)
    trading.submit(p.preview_id, tok, user="local")
    assert store.journal_row(coid)["state"] == "NEW"


def test_n5_failed_lookups_are_not_misses_and_ignored_misses_report_the_stored_verification(env, monkeypatch):
    from my_ai_trading.accounts.adapters.base import ExchangeUnavailable
    store, wall, _, trading, connect, submit = env
    coid = submit(connect())
    same_ms = trading.reconcile(coid, user="local")  # same millisecond as the acknowledgement: ignored
    assert same_ms["verification"] == "confirmed"

    def down(self, *a, **kw):
        raise ExchangeUnavailable("down")
    with monkeypatch.context() as m:
        m.setattr(Ex, "query_order", down)
        for _ in range(3):
            wall.ms += MISS_SPACING_MS
            assert trading.reconcile(coid, user="local")["verification"] == "lookup_failed"
    wall.ms += MISS_SPACING_MS
    assert trading.reconcile(coid, user="local")["misses"] == 1  # was 4: failures counted as misses


def test_n5_a_refused_mode_change_keeps_pending_previews(env):
    _, _, _, trading, connect, _ = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    with pytest.raises(TradingRefused, match="consent"):
        trading.set_mode(cid, user="local", enabled=True, consent="wrong")
    assert trading.submit(p.preview_id, tok, user="local")["client_order_id"] == "mat" + p.preview_id


def test_flow6_mode_switched_off_after_the_last_connection_check_sends_nothing(env):
    store, _, _, trading, connect, _ = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    real_insert = store.journal_insert

    def switch_off_during_insert(row):  # after submit's _tradable() check, before the final generation check
        real_insert(row)
        trading.set_mode(cid, user="local", enabled=False, consent=None)
    store.journal_insert = switch_off_during_insert
    with pytest.raises(TradingRefused, match="nothing was sent"):
        trading.submit(p.preview_id, tok, user="local")
    assert Ex.sends == [] and store.journal_row("mat" + p.preview_id)["state"] == "REJECTED"


def test_flow6_mode_switched_while_a_preview_is_built_is_not_stored(env, monkeypatch):
    _, _, _, trading, connect, _ = env
    cid = connect()
    real = Ex.reference_price

    def switch(self, symbol, market):
        trading.set_mode(cid, user="local", enabled=False, consent=None)
        return real(self, symbol, market)
    monkeypatch.setattr(Ex, "reference_price", switch)
    with pytest.raises(TradingRefused, match="while the preview was being built"):
        trading.preview(cid, DRAFT, user="local")


def test_n1_resolve_in_the_gap_between_the_journal_row_and_the_send_is_refused(env, monkeypatch):
    # Verifier D1: the "being sent" mark was set after the SENDING row was written.
    from my_ai_trading.accounts.adapters.base import OutcomeUnknown
    store, wall, _, trading, connect, _ = env
    cid = connect()
    p, tok = trading.preview(cid, DRAFT, user="local")
    coid, refused = "mat" + p.preview_id, []
    real_insert = store.journal_insert

    def insert_then_resolve(row):
        real_insert(row)
        try:
            trading.resolve(coid, user="local")
        except TradingRefused as exc:
            refused.append(str(exc))
    monkeypatch.setattr(store, "journal_insert", insert_then_resolve)

    def timeout(self, cred, o):
        Ex.sends.append(o.client_order_id)
        raise OutcomeUnknown("timed out")
    monkeypatch.setattr(Ex, "place_order", timeout)
    out = trading.submit(p.preview_id, tok, user="local")
    assert refused and "being sent" in refused[0]
    assert out["state"] == "UNKNOWN" and [r["client_order_id"] for r in trading.needs_reconcile(user="local")] == [coid]


def test_the_fallback_needs_the_same_key_not_just_the_same_last_four_characters(env):
    # Codex review on PR #8: two different keys ending in the same 4 characters were treated as the same key.
    store, wall, accounts, trading, connect, submit = env
    cid = connect()  # key "k-000000"
    coid = submit(cid)
    accounts.disconnect(cid, user="local")
    accounts.connect(user="local", exchange="sim", environment=Environment.TESTNET, lifetime=Lifetime.SESSION,
                     label="another account", credential=Credential(Secret("other-000000"), Secret("s-1")))
    with pytest.raises(TradingRefused, match="key ending 0000"):  # same hint, different key: refused
        trading.reconcile(coid, user="local")
    with pytest.raises(TradingRefused):
        trading.cancel(coid, user="local")
    accounts.connect(user="local", exchange="sim", environment=Environment.TESTNET, lifetime=Lifetime.SESSION,
                     label="same key", credential=Credential(Secret("k-000000"), Secret("s-000000")))
    assert "checked through your current" in trading.reconcile(coid, user="local")["note"]
    assert "k-000000" not in str(store.journal_row(coid))  # only the hint and a one-way fingerprint are stored
