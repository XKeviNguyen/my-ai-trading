"""Accounts tab: drives the offscreen window against the REAL core API with a simulated exchange adapter.

The simulated adapter (SimAdapter) replaces every exchange, so nothing leaves the machine. Keys used here are
made-up test strings, never real credentials."""
from __future__ import annotations

import sys
import threading
import time
from decimal import Decimal

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget  # noqa: E402

from conftest import TOKEN, free_port, repo_root, wait_until  # noqa: E402
from my_ai_trading_desktop import client as C  # noqa: E402
from my_ai_trading_desktop import formatting as F  # noqa: E402
from my_ai_trading_desktop.app import MainWindow  # noqa: E402
from my_ai_trading_desktop.widgets import Card, ResultArea  # noqa: E402

FAKE_KEY = "simkey-AAAA-1111-not-a-real-key"
FAKE_SECRET = "simsecret-BBBB-2222-not-a-real-secret"


def _sim_adapter_class():
    from my_ai_trading.accounts.adapters.base import ExchangeAdapter, ExchangeInfo, SymbolRules
    from my_ai_trading.accounts.models import (
        AccountSnapshot, AccountTotals, Balance, Environment, Fill, KeyPermissions, Order, OrderStatus, Position,
        WalletType,
    )

    class SimAdapter(ExchangeAdapter):
        """A tiny in-memory exchange: read-only keys on live, trading keys on testnet."""
        INFO = ExchangeInfo(id="sim", name="Sim", kind="crypto_exchange",
                            environments=(Environment.LIVE, Environment.TESTNET), needs_passphrase=False,
                            markets=("spot", "usdm_perpetual"), key_setup_url="https://example.invalid")
        book: dict = {}
        live_can_trade = False

        def permissions(self, cred):
            trade = self.environment is Environment.TESTNET or SimAdapter.live_can_trade
            return KeyPermissions(read=True, spot_trade=trade, derivatives_trade=trade, margin_trade=False,
                                  withdraw=False, transfer=False, ip_restricted=True)

        def symbol_rules(self, symbol, market):
            return SymbolRules(symbol=symbol, market=market, base_asset="BTC", quote_asset="USDT", status="TRADING",
                               price_tick=Decimal("0.01"), quantity_step=Decimal("0.001"),
                               min_quantity=Decimal("0.001"), max_quantity=Decimal("100"), min_notional=Decimal("5"),
                               order_types=("limit", "market"))

        def reference_price(self, symbol, market):
            return Decimal("100")

        def fee_rates(self, cred, symbol, market):
            return Decimal("0.001"), Decimal("0.002")

        def leverage(self, cred, symbol, market):
            return Decimal("5")

        def snapshot(self, cred, connection_id):
            open_orders = [o for o in SimAdapter.book.values() if o.status is OrderStatus.NEW]
            return AccountSnapshot(
                connection_id=connection_id, exchange="sim", environment=self.environment, as_of_ms=1,
                totals=[AccountTotals(wallet=WalletType.FUTURES_USDM, available_balance=Decimal("50"))],
                balances=[Balance(asset="USDT", wallet=WalletType.SPOT, free=Decimal("1000"))],
                positions=[Position(symbol="BTCUSDT", market="usdm_perpetual", side="long", quantity=Decimal("0.5"))],
                open_orders=open_orders)

        def fills(self, cred, symbol, market, *, limit=100):
            return [Fill(trade_id="1", order_id="1", symbol=symbol, market=market, side="buy", price=Decimal("100"),
                         quantity=Decimal("1"), fee=Decimal("0.1"), fee_asset="USDT", time_ms=1_000),
                    Fill(trade_id="2", order_id="2", symbol=symbol, market=market, side="sell", price=Decimal("110"),
                         quantity=Decimal("1"), fee=Decimal("0.1"), fee_asset="USDT", time_ms=2_000)]

        def place_order(self, cred, order):
            assert self.environment is Environment.TESTNET
            o = Order(order_id=str(len(SimAdapter.book) + 1), client_order_id=order.client_order_id,
                      symbol=order.symbol, market=order.market, side=order.side, type=order.type,
                      status=OrderStatus.NEW, quantity=order.quantity, price=order.price, filled_quantity=Decimal(0))
            SimAdapter.book[order.client_order_id] = o
            return o

        def query_order(self, cred, symbol, market, *, client_order_id):
            return SimAdapter.book.get(client_order_id)

        def cancel_order(self, cred, symbol, market, *, client_order_id):
            o = SimAdapter.book[client_order_id].model_copy(update={"status": OrderStatus.CANCELED})
            SimAdapter.book[client_order_id] = o
            return o

    return SimAdapter


@pytest.fixture()
def account_engine(tmp_path):
    """The real FastAPI app with a memory-only credential vault and the simulated adapter for every exchange."""
    fakes_dir = repo_root() / "tests"
    if not (fakes_dir / "fakes.py").exists():
        pytest.skip(f"core test fakes not found under {fakes_dir}; set MY_AI_TRADING_REPO_ROOT")
    sys.path.insert(0, str(fakes_dir))
    try:
        from fakes import make_exchange  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(fakes_dir))
    from my_ai_trading.accounts.credentials import CredentialStore, MemoryVault
    from my_ai_trading.accounts.service import AccountService
    from my_ai_trading.api import create_app
    from my_ai_trading.copilot import Copilot
    from my_ai_trading.store import Store

    sim = _sim_adapter_class()
    sim.book, sim.live_can_trade = {}, False
    _, svc = make_exchange()
    store = Store()
    accounts = AccountService(CredentialStore(store, persistent_vault=MemoryVault),
                              adapter_factory=lambda ex, env: sim(env))
    port = free_port()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN, port=port,
                     accounts=accounts)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "engine did not start"
    yield port, sim
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def win(qapp, account_engine, tmp_path):
    port, sim = account_engine
    qs = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    w = MainWindow(port=port, qsettings=qs, start_polling=False)
    w.ctx.tokens.set_pasted(TOKEN)
    w.resize(420, 600)
    w.show()
    w.sim = sim
    w.qs_path = tmp_path / "desktop.ini"
    yield w
    w.close()
    wait_until(lambda: w.ctx.runner.pending == 0, 10)
    w.deleteLater()


def texts(widget) -> str:
    parts = [lab.text() for lab in widget.findChildren(QLabel)]
    parts += [e.toPlainText() for e in widget.findChildren(QPlainTextEdit)]
    parts += [c.title() for c in widget.findChildren(Card)]
    return "\n".join(parts)


def button(widget, text: str) -> QPushButton:
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def open_accounts(win, section: str = "connections"):
    view = win.views["accounts"]
    win.select_tab("accounts")
    view.go(section)
    body = view.bodies[section]
    assert wait_until(lambda: view.ctx.runner.pending == 0 and "Loading…" not in texts(body))
    return view, body


def connect_via_form(view, body, environment="testnet", label="sim test"):
    form = body.findChild(Card, "connect-form")
    ex = form.findChild(QComboBox, "connect-exchange")
    ex.setCurrentIndex(ex.findData("binance"))
    env = form.findChild(QComboBox, "connect-environment")
    env.setCurrentIndex(env.findData(environment))
    form.findChild(QLineEdit, "connect-label").setText(label)
    key, secret = form.findChild(QLineEdit, "connect-api-key"), form.findChild(QLineEdit, "connect-api-secret")
    assert key.echoMode() == QLineEdit.EchoMode.Password and secret.echoMode() == QLineEdit.EchoMode.Password
    key.setText(FAKE_KEY)
    secret.setText(FAKE_SECRET)
    msg = form.findChild(ResultArea, "connect-msg")
    button(form, "Check and connect").click()
    # secrets leave the form at once, before the engine has answered
    assert key.text() == "" and secret.text() == ""
    return form, msg


def all_visible_text(win) -> str:
    parts = [lab.text() for lab in win.findChildren(QLabel)]
    parts += [e.text() for e in win.findChildren(QLineEdit)]
    parts += [e.toPlainText() for e in win.findChildren(QPlainTextEdit)]
    for t in win.findChildren(QTableWidget):
        parts += [t.item(i, j).text() for i in range(t.rowCount()) for j in range(t.columnCount()) if t.item(i, j)]
    return "\n".join(parts)


def test_connect_lists_hint_and_clears_secrets(win):
    view, body = open_accounts(win)
    assert "No exchange connected." in texts(body)
    assert "partner approval" in texts(body)  # OAuth note only, no fake button
    assert not [b for b in body.findChildren(QPushButton) if "OAuth" in b.text() or "Sign in" in b.text()]
    form = body.findChild(Card, "connect-form")
    life = form.findChild(QComboBox, "connect-lifetime")
    assert life.currentData() == "session" and life.currentText() == "This session only (default)"
    assert "credential vault" in texts(form)
    connect_via_form(view, body)
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    out = texts(view.bodies["connections"])
    assert f"••••{FAKE_KEY[-4:]}" in out
    assert "sim test · Testnet" in out
    assert "read yes · trade yes · withdraw no · transfer no" in out
    assert "IP restricted" in out and "ends when the engine stops" in out
    assert "Connected." in texts(view.notice) and "exchange account" in texts(view.notice)
    everything = all_visible_text(win)
    assert FAKE_SECRET not in everything and FAKE_KEY not in everything
    win.ctx.qsettings.sync()
    if win.qs_path.exists():  # nothing about the key reaches QSettings
        saved = win.qs_path.read_text()
        assert FAKE_SECRET not in saved and FAKE_KEY not in saved
    # a failed attempt clears the fields too
    win.sim.live_can_trade = True
    form, msg = connect_via_form(view, view.bodies["connections"], environment="live", label="live trader")
    assert wait_until(lambda: "Not allowed:" in texts(msg))
    assert "live keys must be read-only" in texts(msg)
    assert "token" not in texts(msg).lower()  # 403 here is a policy refusal, not a token problem
    assert "Open Settings" not in [b.text() for b in msg.findChildren(QPushButton)]
    assert FAKE_SECRET not in all_visible_text(win)


def test_passphrase_field_only_for_exchanges_that_need_it(win):
    view = win.views["accounts"]
    # only Binance is registered today: add a made-up passphrase exchange to the cached catalog
    view.catalog = {"lifetimes": ["session", "24h"], "default_lifetime": "session", "exchanges": [
        {"id": "binance", "name": "Binance", "environments": ["live", "testnet"], "needs_passphrase": False,
         "notes": ["note one"], "key_setup_url": "https://example.invalid/keys"},
        {"id": "phrasex", "name": "PhraseX", "environments": ["testnet"], "needs_passphrase": True,
         "notes": [], "key_setup_url": "https://example.invalid/px"}]}
    view, body = open_accounts(win)
    assert "Create keys at: https://example.invalid/keys" in texts(body)
    form = body.findChild(Card, "connect-form")
    ex = form.findChild(QComboBox, "connect-exchange")
    pp = form.findChild(QLineEdit, "connect-passphrase")
    catalog = {e["id"]: e for e in view.catalog["exchanges"]}
    for ex_id, info in catalog.items():
        ex.setCurrentIndex(ex.findData(ex_id))
        assert pp.isVisibleTo(form) == bool(info["needs_passphrase"]), ex_id
        env = form.findChild(QComboBox, "connect-environment")
        for row in range(env.count()):
            assert env.model().item(row).isEnabled() == (env.itemData(row) in info["environments"]), ex_id
    assert any(i["needs_passphrase"] for i in catalog.values())


def test_portfolio_and_positions_render(win):
    view, body = open_accounts(win)
    connect_via_form(view, body)
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    view, body = open_accounts(win, "portfolio")
    out = texts(body)
    total = body.findChild(Card, "portfolio-total")
    assert total is not None and "Total (" in texts(total) and "$" in texts(total)
    assert "Allocation" in out and "Exposure" in out
    assert body.findChild(QTableWidget, "exposure-table") is not None
    view, body = open_accounts(win, "positions")
    assert "Open positions (1, Testnet)" in texts(body)
    t = body.findChild(QTableWidget, "positions-table")
    assert t.item(0, 0).text().startswith("BTCUSDT")


def enable_trading(win, view):
    asked = []
    win.ctx.confirm = lambda parent, title, text, detail="", yes_label="": asked.append((title, text, yes_label)) or True
    body = view.bodies["connections"]
    button(body, "Turn Trading Mode on…").click()
    assert wait_until(lambda: "Trading Mode on" in texts(view.bodies["connections"]))
    return asked


def test_trading_mode_needs_consent_then_preview_place_and_journal(win):
    from my_ai_trading.accounts.trading import CONSENT_TEXT
    view, body = open_accounts(win)
    connect_via_form(view, body)
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    # declining the consent keeps Trading Mode off
    win.ctx.confirm = lambda *a, **k: False
    button(view.bodies["connections"], "Turn Trading Mode on…").click()
    assert wait_until(lambda: "Trading Mode stays off." in texts(view.bodies["connections"]))
    asked = enable_trading(win, view)
    assert asked and asked[0][1] == CONSENT_TEXT  # the exact consent text from the engine

    win.ctx.set_instrument("ETHUSDT", "spot", "1h")
    view, body = open_accounts(win, "orders")
    assert wait_until(lambda: body.findChild(Card, "order-ticket") is not None)
    ticket = body.findChild(Card, "order-ticket")
    assert ticket.findChild(QLineEdit, "order-symbol").text() == "ETHUSDT"  # active instrument
    ticket.findChild(QLineEdit, "order-quantity").setText("0.1")
    ticket.findChild(QLineEdit, "order-price").setText("99.50")
    button(ticket, "Preview order").click()
    assert wait_until(lambda: ticket.findChild(Card, "preview-card") is not None)
    card_ = ticket.findChild(Card, "preview-card")
    out = texts(card_)
    assert "BUY 0.1 ETHUSDT · limit @ 99.50" in out
    assert "Exchange rule checks" in out and "OK —" in out
    assert "notional: 9.95 USDT (calculated)" in out and "(estimate)" in out
    assert "Confirm within" in out
    button(card_, "Place testnet order").click()
    assert wait_until(lambda: "NEW: Accepted by the exchange" in texts(ticket))
    button(ticket, "Refresh orders").click()
    assert wait_until(lambda: any(c.objectName() == "journal-row" for c in view.bodies["orders"].findChildren(Card)))
    rows = [c for c in view.bodies["orders"].findChildren(Card) if c.objectName() == "journal-row"]
    assert rows[0].title() == "buy 0.1 ETHUSDT limit @ 99.50 · NEW"
    assert "Open orders (1)" in texts(view.bodies["orders"])
    # check status, then cancel through a confirmation
    button(rows[0], "Check status").click()
    assert wait_until(lambda: "NEW — status from the exchange" in texts(rows[0]))
    asked.clear()
    win.ctx.confirm = lambda parent, title, text, detail="", yes_label="": asked.append(text) or True
    button(rows[0], "Cancel order").click()
    assert asked == ["Cancel this testnet order?"]
    assert wait_until(lambda: "CANCELED" in texts(rows[0]))


def test_preview_countdown_disables_place_at_expiry(win):
    view, body = open_accounts(win)
    connect_via_form(view, body)
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    enable_trading(win, view)
    view, body = open_accounts(win, "orders")
    assert wait_until(lambda: body.findChild(Card, "order-ticket") is not None)
    ticket = body.findChild(Card, "order-ticket")
    ticket.findChild(QLineEdit, "order-quantity").setText("0.1")
    ticket.findChild(QLineEdit, "order-price").setText("99.50")
    button(ticket, "Preview order").click()
    assert wait_until(lambda: ticket.findChild(Card, "preview-card") is not None)
    place = button(ticket, "Place testnet order")
    assert view.timer.isActive() and 55 <= view._remaining <= 60
    view._remaining = 2
    view._tick()
    assert place.isEnabled() and "Confirm within 1 s" in texts(ticket)
    view._tick()
    assert not place.isEnabled() and "Expired — preview again" in texts(ticket)
    assert not view.timer.isActive()


def test_live_connection_shows_read_only_note_and_no_ticket(win):
    view, body = open_accounts(win)
    connect_via_form(view, body, environment="live", label="live ro")
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    out = texts(view.bodies["connections"])
    assert "live ro · Live · read-only" in out and "read yes · trade no" in out
    assert not [b for b in view.bodies["connections"].findChildren(QPushButton) if "Trading Mode" in b.text()]
    view, body = open_accounts(win, "orders")
    assert wait_until(lambda: "Live connections are read-only" in texts(body))
    assert body.findChild(Card, "order-ticket") is None


def test_performance_and_disconnect(win):
    view, body = open_accounts(win)
    connect_via_form(view, body)
    assert wait_until(lambda: "Connected (1)" in texts(view.bodies["connections"]))
    view, body = open_accounts(win, "performance")
    form = body.findChild(Card, "performance-form")
    form.findChild(QLineEdit, "performance-symbol").setText("btcusdt")
    button(form, "Calculate").click()
    assert wait_until(lambda: body.findChild(Card, "performance-result") is not None)
    out = texts(body)
    assert "Closed trades" in out and "Win rate" in out and "do not predict" in out
    view, body = open_accounts(win, "connections")
    asked = []
    win.ctx.confirm = lambda parent, title, text, detail="", yes_label="": asked.append(text) or True
    button(body, "Disconnect").click()
    assert asked and "deleted from this computer" in asked[0] and "until you delete it there" in asked[0]
    assert wait_until(lambda: "No exchange connected." in texts(view.bodies["connections"]))
    assert "still works at the exchange" in texts(view.notice)


def test_sections_without_connection_point_to_connections(win):
    view, body = open_accounts(win, "portfolio")
    assert "Connect an exchange first." in texts(body)
    button(body, "Open Connections").click()
    assert view.section() == "connections"


# ---------------------------------------------------------------- pure / client units
@pytest.mark.parametrize("status,cls,kind", [
    (403, C.ForbiddenError, "forbidden"), (409, C.ConflictError, "conflict"), (410, C.ExpiredError, "expired"),
    (424, C.ExchangeAuthError, "exchange_auth"), (501, C.NotSupportedError, "not_supported"),
    (504, C.OutcomeUnknownError, "outcome_unknown"),
])
def test_account_error_kinds_never_show_token_hint(status, cls, kind):
    err = C.error_for_status(status, "detail here")
    assert type(err) is cls and err.kind == kind
    assert "token" not in str(err).lower() and "Settings" not in str(err)
    assert not isinstance(err, C.UnauthorizedError)


def test_account_error_messages():
    assert str(C.error_for_status(403, "")) == "Not allowed: this action is outside the app's account policy"
    assert str(C.error_for_status(410, "")) == "This exchange connection expired; connect again."
    assert str(C.error_for_status(424, "bad key")) == "The exchange rejected the key: bad key"
    assert "do not resend" in str(C.error_for_status(504, ""))


def test_client_validates_ids_before_sending():
    c = C.ApiClient(lambda: "t" * 40, port=1)
    with pytest.raises(C.ValidationError):
        c.disconnect("../settings")
    with pytest.raises(C.ValidationError):
        c.reconcile("mat123")
    with pytest.raises(C.ValidationError):
        c.submit_order("nothex", "x" * 30)


def test_connect_redacts_secrets_from_error_detail(monkeypatch):
    c = C.ApiClient(lambda: "t" * 40, port=1)

    def boom(*a, **k):
        raise C.ValidationError(422, f"api_secret: bad value {FAKE_SECRET}")
    monkeypatch.setattr(c, "request", boom)
    with pytest.raises(C.ValidationError) as ei:
        c.connect(exchange="binance", environment="testnet", lifetime="session", label="", api_key=FAKE_KEY,
                  api_secret=FAKE_SECRET)
    assert FAKE_SECRET not in str(ei.value) and "[redacted]" in str(ei.value)


def test_account_formatting_helpers():
    c = {"label": "", "exchange": "binance", "environment": "live", "lifetime": "7d", "expires_ms": 0,
         "permissions": {"read": True, "spot_trade": False, "derivatives_trade": None, "withdraw": False,
                         "transfer": None}}
    assert F.connection_title(c) == "binance · Live · read-only"
    assert F.permissions_line(c) == "read yes · trade unknown · withdraw no · transfer unknown"
    assert F.connection_stored(c) == "7 days; until you remove it"
    assert not F.connection_can_trade(c)
    assert F.connection_can_trade({**c, "environment": "testnet", "permissions": {"spot_trade": True}})
    assert F.journal_summary({"client_order_id": "mat1", "request_json": "not json"}) == "mat1"
    assert F.journal_summary({"request_json": '{"side": "sell", "quantity": "1", "symbol": "X", "type": "market", '
                                              '"price": null}'}) == "sell 1 X market"
    assert F.estimate_lines({"fee": {"value": None, "provenance": "estimate", "note": "n"}}) == \
        ["fee: not estimated (estimate) — n"]
