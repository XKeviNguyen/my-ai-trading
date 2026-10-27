"""Exchange connections, portfolio, positions, testnet orders and performance (R8; AGENTS.md §2).

Same features and wording as the extension's Accounts view (extension/src/views/accounts.ts).
- API keys are typed into password fields, sent once to the local engine and cleared from the form right away,
  whether the request succeeds or fails. They are never stored in QSettings, shown or logged.
- Live connections are read-only. Orders exist only for testnet connections with Trading Mode on, and each one
  needs a preview and a confirmation within the preview's 60 s window.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import formatting as F
from ..context import AppContext
from ..widgets import ResultArea, bullet_list, buddy_label, card, heading, kv_form, plain_label, scroll_page, status_label

SECTIONS = (("connections", "Connections"), ("portfolio", "Portfolio"), ("positions", "Positions"),
            ("orders", "Orders"), ("performance", "Performance"))
MARKET_CHOICES = (("spot", "Spot"), ("usdm_perpetual", "USD-M perpetual"), ("stock", "Stock"))
SECRET_HINTS = (Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
                | Qt.InputMethodHint.ImhNoAutoUppercase)


def data_table(name: str, caption: str, head: list[str], rows: list[list[str]]) -> QTableWidget:
    t = QTableWidget(len(rows), len(head))
    t.setObjectName(name)
    t.setAccessibleName(caption)
    t.setHorizontalHeaderLabels(head)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setTabKeyNavigation(False)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for i, r in enumerate(rows):
        for j, value in enumerate(r):
            t.setItem(i, j, QTableWidgetItem(value))  # table items are plain text
    t.setMinimumHeight(min(320, 40 + 30 * len(rows)))
    return t


def _dicts(v: Any) -> list[dict[str, Any]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _row(*widgets: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        lay.addWidget(x)
    lay.addStretch(1)
    return w


def _combo(choices: tuple[tuple[str, str], ...] | list[tuple[str, str]], current: str | None = None) -> QComboBox:
    c = QComboBox()
    for value, label in choices:
        c.addItem(label, value)
    if current is not None:
        c.setCurrentIndex(max(c.findData(current), 0))
    return c


def _secret_edit(name: str) -> QLineEdit:
    e = QLineEdit()
    e.setObjectName(name)
    e.setEchoMode(QLineEdit.EchoMode.Password)  # also disables copy/cut and input-method prediction
    e.setInputMethodHints(SECRET_HINTS)
    e.setMaxLength(4096)
    return e


class AccountsView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.catalog: dict[str, Any] | None = None
        self.connections: list[dict[str, Any]] = []
        self.env: str | None = None  # portfolio/positions: live and testnet are never added together
        self._seq = 0  # bumped on every render: stale results are dropped
        self._orders_seq = 0
        self._preview_seq = 0
        self._remaining = 0
        self._place_btn: QPushButton | None = None
        self._countdown_label = None
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.addWidget(plain_label("Market analysis never needs an account. Connect an exchange only to see your own "
                                    "balances, positions and orders.", muted=True))
        self.notice = ResultArea("accounts-notice")
        outer.addWidget(self.notice)
        self.sections = QTabWidget()
        self.sections.setObjectName("account-sections")
        self.sections.setAccessibleName("Account sections")
        self.sections.setUsesScrollButtons(True)
        self.bodies: dict[str, ResultArea] = {}
        for key, title in SECTIONS:
            inner = QWidget()
            il = QVBoxLayout(inner)
            body = ResultArea(f"accounts-{key}")
            il.addWidget(body)
            il.addStretch(1)
            self.bodies[key] = body
            self.sections.addTab(scroll_page(inner), title)
        self.sections.currentChanged.connect(lambda _i: self.render())
        outer.addWidget(self.sections, 1)

    # ---- navigation ----
    def show_tab(self) -> None:
        self.render()

    def section(self) -> str:
        return SECTIONS[max(self.sections.currentIndex(), 0)][0]

    def go(self, key: str) -> None:
        index = next(i for i, (k, _t) in enumerate(SECTIONS) if k == key)
        if index == self.sections.currentIndex():
            self.render()
        else:
            self.sections.setCurrentIndex(index)  # currentChanged renders

    def _fresh(self, gen: int) -> bool:
        return gen == self._seq

    def render(self) -> None:
        self._stop_countdown()
        self._seq += 1
        gen, sec = self._seq, self.section()
        body = self.bodies[sec]
        body.set(status_label("loading", "Loading…"))
        need_catalog = sec in ("connections", "orders", "performance") and self.catalog is None
        client = self.ctx.client

        def job() -> tuple[dict, dict | None, dict | None]:
            conns = client.connections()
            catalog = client.exchanges() if need_catalog else None
            env = self.env
            p = client.portfolio(env) if sec in ("portfolio", "positions") and _dicts(conns.get("connections")) else None
            return conns, catalog, p

        def ok(res: tuple[dict, dict | None, dict | None]) -> None:
            if not self._fresh(gen):
                return
            conns, catalog, p = res
            self.connections = _dicts(conns.get("connections"))
            if catalog is not None:
                self.catalog = catalog
            if sec == "connections":
                return self._render_connections(body)
            if not self.connections:
                return self._need_connection(body)
            if sec == "portfolio":
                return self._render_portfolio(body, p or {})
            if sec == "positions":
                return self._render_positions(body, p or {})
            if sec == "orders":
                return self._render_orders(body)
            return self._render_performance(body)

        def err(e: Exception) -> None:
            if self._fresh(gen):
                body.set(self.ctx.error_widget(e))
        self.ctx.run(job, ok, err)

    def _need_connection(self, body: ResultArea) -> None:
        b = QPushButton("Open Connections")
        b.clicked.connect(lambda: self.go("connections"))
        body.set(plain_label("Connect an exchange first."), _row(b))

    # ---------------------------------------------------------------- connections
    def _render_connections(self, body: ResultArea) -> None:
        cards = [self._connection_card(c) for c in self.connections]
        body.set(
            heading(f"Connected ({len(self.connections)})"),
            *(cards or [plain_label("No exchange connected.", muted=True)]),
            self._connect_form(self.catalog or {}),
            plain_label("One-click sign-in with Binance, Bybit or OKX (OAuth) needs partner approval from each exchange "
                        "and is not available yet. Use an API key instead.", muted=True),
        )

    def _connection_card(self, c: dict[str, Any]) -> QWidget:
        box, lay = card(F.connection_title(c))
        box.setObjectName("connection-card")
        lay.addWidget(kv_form([
            ("Key", f"••••{F.text(c.get('key_hint'), '')}"),
            ("Stored", F.connection_stored(c)),
            ("Permissions", F.permissions_line(c)),
            ("IP restricted", F.yes_no(F.key_permissions(c).get("ip_restricted"))),
        ]))
        msg = ResultArea("connection-msg")
        buttons = []
        if F.connection_can_trade(c):
            toggle = QPushButton("Turn Trading Mode off" if c.get("trading_enabled") else "Turn Trading Mode on…")
            toggle.setObjectName("trading-toggle")
            toggle.clicked.connect(lambda: self._toggle_trading(c, msg))
            buttons.append(toggle)
        disconnect = QPushButton("Disconnect")
        disconnect.setObjectName("disconnect")
        disconnect.setAccessibleName(f"Disconnect {F.connection_name(c)}")
        disconnect.clicked.connect(lambda: self._disconnect(c, msg))
        buttons.append(disconnect)
        lay.addWidget(_row(*buttons))
        lay.addWidget(msg)
        return box

    def _toggle_trading(self, c: dict[str, Any], msg: ResultArea) -> None:
        cid, gen, client = F.text(c.get("connection_id"), ""), self._seq, self.ctx.client
        fail = lambda e: msg.set(self.ctx.error_widget(e))  # noqa: E731
        if c.get("trading_enabled"):
            self.ctx.run(lambda: client.set_trading_mode(cid, False, None), lambda _r: self.render(), fail)
            return
        msg.set(status_label("loading", "Loading the consent text…"))

        def got_consent(r: dict[str, Any]) -> None:
            consent = r.get("consent_text")
            if not self._fresh(gen):
                return
            if not isinstance(consent, str) or not consent:
                return msg.set(status_label("error", "The engine did not return the consent text."))
            msg.set()
            agreed = self.ctx.confirm(self, "Turn Trading Mode on", consent,
                                      "Testnet/demo only. Every order still needs its own preview and confirmation.",
                                      "I agree, turn Trading Mode on")
            if not agreed:
                return msg.set(status_label("info", "Trading Mode stays off."))
            msg.set(status_label("loading", "Turning Trading Mode on…"))
            self.ctx.run(lambda: client.set_trading_mode(cid, True, consent), lambda _r: self.render(), fail)
        self.ctx.run(client.trading_consent, got_consent, fail)

    def _disconnect(self, c: dict[str, Any], msg: ResultArea) -> None:
        name, cid, client = F.connection_name(c), F.text(c.get("connection_id"), ""), self.ctx.client
        if not self.ctx.confirm(self, "Disconnect exchange",
                                f"Disconnect {name}? The key is deleted from this computer. It keeps working at the "
                                "exchange until you delete it there.", "", "Disconnect"):
            return

        def ok(r: dict[str, Any]) -> None:
            self.notice.set(status_label("ok", F.text(r.get("note"), "Disconnected.")))
            self.render()
        self.ctx.run(lambda: client.disconnect(cid), ok, lambda e: msg.set(self.ctx.error_widget(e)))

    def _connect_form(self, catalog: dict[str, Any]) -> QWidget:
        box, lay = card("Connect an exchange")
        box.setObjectName("connect-form")
        lay.addWidget(plain_label("Live keys must be read-only: keys that can trade, withdraw or transfer are refused. "
                                  "Orders are only ever sent to testnet/demo accounts."))
        exchanges = _dicts(catalog.get("exchanges"))
        ex = _combo([(F.text(e.get("id"), ""), F.text(e.get("name"), "")) for e in exchanges])
        ex.setObjectName("connect-exchange")
        env = _combo((("testnet", "Testnet / demo"), ("live", "Live (read-only key)")))
        env.setObjectName("connect-environment")
        lifetimes = [x for x in catalog.get("lifetimes") or [] if isinstance(x, str)]
        life = _combo([(x, F.LIFETIME_LABELS.get(x, x)) for x in lifetimes], F.text(catalog.get("default_lifetime"), ""))
        life.setObjectName("connect-lifetime")
        label = QLineEdit()
        label.setObjectName("connect-label")
        label.setMaxLength(60)
        key, secret, passphrase = (_secret_edit("connect-api-key"), _secret_edit("connect-api-secret"),
                                   _secret_edit("connect-passphrase"))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow(buddy_label("E&xchange", ex), ex)
        form.addRow(buddy_label("Acco&unt", env), env)
        form.addRow(buddy_label("&Keep the key", life), life)
        form.addRow("", plain_label("Session only: the key is forgotten when the engine stops. Longer options use your "
                                    "operating system's credential vault.", muted=True))
        form.addRow(buddy_label("Label (optional)", label), label)
        key_label, secret_label = buddy_label("API key", key), buddy_label("API secret", secret)
        form.addRow(key_label, key)
        form.addRow(secret_label, secret)
        form.addRow(buddy_label("Passphrase", passphrase), passphrase)
        lay.addLayout(form)
        notes = ResultArea("connect-notes")
        lay.addWidget(notes)
        submit = QPushButton("Check and connect")
        submit.setObjectName("connect-submit")
        lay.addWidget(_row(submit))
        msg = ResultArea("connect-msg")
        lay.addWidget(msg)

        def info() -> dict[str, Any] | None:
            return next((e for e in exchanges if e.get("id") == ex.currentData()), None)

        def sync() -> None:
            i = info()
            form.setRowVisible(passphrase, bool(i and i.get("needs_passphrase")))
            key_label.setText(F.text((i or {}).get("key_label"), "API key"))  # e.g. IBKR: paper account number
            secret_label.setText(F.text((i or {}).get("secret_label"), "API secret"))
            envs = [x for x in (i or {}).get("environments") or [] if isinstance(x, str)]
            model = env.model()
            for row in range(env.count()):
                item = model.item(row)
                if item is not None:
                    item.setEnabled(not i or env.itemData(row) in envs)
            cur = model.item(env.currentIndex())
            if cur is not None and not cur.isEnabled() and envs:
                env.setCurrentIndex(max(env.findData(envs[0]), 0))
            if i is None:
                return notes.set()
            notes.set(bullet_list(F.str_list(i.get("notes")), ""),
                      plain_label(f"Create keys at: {F.text(i.get('key_setup_url'))}", muted=True))
        ex.currentIndexChanged.connect(lambda _i: sync())
        sync()

        def go() -> None:
            i = info()
            needs_pass = bool(i and i.get("needs_passphrase"))
            creds = {"api_key": key.text().strip(), "api_secret": secret.text().strip(),
                     "passphrase": passphrase.text() if needs_pass else None}
            for edit in (key, secret, passphrase):  # never keep secrets in the window longer than needed
                edit.clear()
            if i is None:
                creds.clear()
                return msg.set(status_label("error", "No exchange is available to connect."))
            if len(creds["api_key"]) < 3 or len(creds["api_secret"]) < 3:
                creds.clear()
                return msg.set(status_label("error", f"Enter the {key_label.text()} and the {secret_label.text()}."))
            args = {"exchange": ex.currentData(), "environment": env.currentData(), "lifetime": life.currentData(),
                    "label": label.text().strip()}
            client = self.ctx.client
            submit.setEnabled(False)
            msg.set(status_label("loading", "Checking the key with the exchange…"))

            def job() -> dict[str, Any]:
                try:
                    return client.connect(**args, **creds)
                finally:
                    creds.clear()

            def ok(r: dict[str, Any]) -> None:
                self.notice.set(status_label("ok", f"Connected. {F.text(r.get('note'), '')}"))
                self.render()

            def err(e: Exception) -> None:
                submit.setEnabled(True)
                msg.set(self.ctx.error_widget(e))
            self.ctx.run(job, ok, err)
        submit.clicked.connect(go)
        return box

    # ---------------------------------------------------------------- portfolio & positions
    def _env_switch(self, p: dict[str, Any]) -> QWidget | None:
        envs = [e for e in F.str_list(p.get("available_environments")) if e in ("live", "testnet")]
        if len(envs) < 2:
            return None
        pick = QComboBox()
        pick.setObjectName("portfolio-environment")
        for e in envs:
            pick.addItem("Live accounts" if e == "live" else "Testnet accounts", e)
        pick.setCurrentIndex(max(pick.findData(p.get("environment")), 0))

        def changed(_i: int) -> None:
            self.env = pick.currentData()
            self.render()
        pick.currentIndexChanged.connect(changed)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(buddy_label("Accounts &shown", pick))
        lay.addWidget(pick, 1)
        lay.addWidget(plain_label("Live and testnet funds are never added together.", muted=True))
        return w

    @staticmethod
    def _gaps(p: dict[str, Any]) -> QWidget | None:
        """What the exchanges could not include: exposure and positions are incomplete by exactly these items."""
        items = F.str_list(p.get("completeness_warnings"))
        if not items:
            return None
        box, lay = card(f"Not included ({len(items)})")
        box.setObjectName("completeness-warnings")
        lay.addWidget(plain_label("Exposure and positions leave out these items; the reported equity may still "
                                  "include them."))
        lay.addWidget(bullet_list(items))
        return box

    @staticmethod
    def _env_name(p: dict[str, Any]) -> str:
        return "Live · read-only" if p.get("environment") == "live" else "Testnet"

    def _render_portfolio(self, body: ResultArea, p: dict[str, Any]) -> None:
        total = p.get("total_value_usd") if isinstance(p.get("total_value_usd"), dict) else {}
        head, hl = card(f"Portfolio · {self._env_name(p)}")
        head.setObjectName("portfolio-total")
        hl.addWidget(plain_label(f"Total ({F.provenance_label(total.get('provenance'))}): "
                                 f"${F.text(total.get('value'))}", bold=True, name="kv-value"))
        if total.get("note"):
            hl.addWidget(plain_label(F.text(total.get("note")), muted=True))
        out: list[QWidget | None] = [self._env_switch(p), head, self._gaps(p)]
        warnings = F.str_list(p.get("warnings"))
        if warnings:
            w, wl = card(f"Risk warnings ({len(warnings)})")
            w.setObjectName("risk-warnings")
            wl.addWidget(bullet_list(warnings))
            out.append(w)
        errors = _dicts(p.get("errors"))
        if errors:
            e, el = card("Connections with errors")
            e.setObjectName("connection-errors")
            el.addWidget(bullet_list([f"{F.text(x.get('exchange'))}: {F.text(x.get('error'))}" for x in errors]))
            out.append(e)
        out.append(heading("Allocation"))
        alloc = _dicts(p.get("allocation"))
        out.append(data_table("allocation-table", "Exposure by asset", ["Asset", "Exposure (USD)", "Share"],
                              [[F.text(a.get("asset")), F.text(a.get("exposure_usd")),
                                f"{F.text(a.get('share_pct'))}%" if a.get("share_pct") else F.DASH] for a in alloc])
                   if alloc else plain_label("No priced holdings or positions.", muted=True))
        out.append(heading("Exposure"))
        d = p.get("exposure_by_direction") if isinstance(p.get("exposure_by_direction"), dict) else {}
        out.append(data_table("exposure-table", "Exposure by direction", ["Direction", "USD"],
                              [["Long", F.text(d.get("long"))], ["Short", F.text(d.get("short"))],
                               ["Net", F.text(p.get("net_exposure_usd"))]]))
        by_ex = p.get("by_exchange") if isinstance(p.get("by_exchange"), dict) else {}
        if by_ex:
            out.append(data_table("exchange-table", "Value by exchange", ["Exchange", "USD"],
                                  [[F.text(k), F.text(v)] for k, v in by_ex.items()]))
        margin = _dicts(p.get("margin"))
        if margin:
            out.append(data_table("margin-table", "Margin use", ["Exchange", "Wallet", "Maint. margin / equity"],
                                  [[F.text(m.get("exchange")), F.text(m.get("wallet")),
                                    f"{F.text(m.get('maintenance_margin_pct_of_equity'))}%"] for m in margin]))
        unpriced = F.str_list(p.get("unpriced_assets"))
        if unpriced:
            out.append(plain_label(f"Not valued (no public price): {', '.join(unpriced)}", muted=True))
        body.set(*out)

    def _render_positions(self, body: ResultArea, p: dict[str, Any]) -> None:
        positions = _dicts(p.get("positions"))
        rows = [[f"{F.text(x.get('symbol'))} · {F.text(x.get('exchange'))}", F.text(x.get("side")),
                 F.text(x.get("quantity")), F.text(x.get("notional_usd")), F.text(x.get("unrealized_pnl")),
                 f"{F.text(x.get('leverage'))}×" if x.get("leverage") else F.DASH,
                 f"{F.text(x.get('liquidation_distance_pct'))}%" if x.get("liquidation_distance_pct") else F.DASH]
                for x in positions]
        body.set(
            self._env_switch(p),
            heading(f"Open positions ({len(positions)}, {self._env_name(p)})"),
            self._gaps(p),
            data_table("positions-table", "Open positions",
                       ["Symbol", "Side", "Size", "Notional", "Unreal. P&L", "Leverage", "To liq."], rows)
            if rows else plain_label("No open positions.", muted=True),
            plain_label("Values are reported by the exchange; distance to liquidation is calculated from its mark and "
                        "liquidation prices.", muted=True),
        )

    def _connection_picker(self, name: str) -> QComboBox:
        pick = QComboBox()
        pick.setObjectName(name)
        for c in self.connections:
            pick.addItem(F.connection_choice(c), F.text(c.get("connection_id"), ""))
        return pick

    # ---------------------------------------------------------------- orders
    def _render_orders(self, body: ResultArea) -> None:
        pick = self._connection_picker("orders-connection")
        area = ResultArea("orders-area")
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addRow(buddy_label("&Connection", pick), pick)
        pick.currentIndexChanged.connect(lambda _i: self._load_orders(pick.currentData(), area))
        body.set(form, area)
        self._load_orders(pick.currentData(), area)

    def _load_orders(self, cid: str, area: ResultArea) -> None:
        self._stop_countdown()
        self._orders_seq += 1
        mine, gen, client = self._orders_seq, self._seq, self.ctx.client
        area.set(status_label("loading", "Loading orders…"))

        def job() -> tuple[dict, dict]:
            return client.snapshot(cid), client.journal()

        def ok(res: tuple[dict, dict]) -> None:
            if mine != self._orders_seq or not self._fresh(gen):
                return
            conn = next((c for c in self.connections if c.get("connection_id") == cid), None)
            if conn is None:
                return area.set(status_label("error", "This connection is no longer listed."))
            snap, journal = res
            rows = [j for j in _dicts(journal.get("orders")) if j.get("connection_id") == cid]
            stuck = set(F.str_list(journal.get("needs_reconcile")))
            area.set(*self._orders_for(conn, snap, rows, stuck))

        def err(e: Exception) -> None:
            if mine == self._orders_seq and self._fresh(gen):
                area.set(self.ctx.error_widget(e))
        self.ctx.run(job, ok, err)

    def _orders_for(self, conn: dict[str, Any], snap: dict[str, Any], journal: list[dict[str, Any]],
                    stuck: set[str]) -> list[QWidget]:
        open_orders = _dicts(snap.get("open_orders"))
        out: list[QWidget] = [heading(f"Open orders ({len(open_orders)})")]
        out.append(data_table("open-orders-table", "Open orders",
                              ["Symbol", "Side", "Type", "Price", "Qty", "Filled", "Status"],
                              [[F.text(o.get("symbol")), F.text(o.get("side")), F.text(o.get("type")),
                                F.text(o.get("price"), "market"), F.text(o.get("quantity")),
                                F.text(o.get("filled_quantity")), F.text(o.get("status"))] for o in open_orders])
                   if open_orders else plain_label("No open orders.", muted=True))
        unavailable = F.str_list(snap.get("unavailable"))
        if unavailable:
            out.append(plain_label(f"Not available: {'; '.join(unavailable)}", muted=True))
        if conn.get("environment") != "testnet":
            out.append(plain_label("Live connections are read-only in this app. Orders can be placed only on "
                                   "testnet/demo connections.", muted=True, name="live-read-only-note"))
            return out
        if not conn.get("trading_enabled"):
            out.append(plain_label("Trading Mode is off for this testnet connection. Turn it on in Connections to "
                                   "place testnet orders."))
        else:
            out.append(self._ticket(conn))
        if journal:
            out.append(heading("Order journal (this app)"))
            out.extend(self._journal_row(j, F.text(j.get("client_order_id"), "") in stuck) for j in journal)
        return out

    def _journal_row(self, j: dict[str, Any], stuck: bool) -> QWidget:
        coid, state = F.text(j.get("client_order_id"), ""), F.text(j.get("state"), "")
        summary = F.journal_summary(j)
        box, lay = card(f"{summary} · {state}" + (" · needs a status check" if stuck else ""))
        box.setObjectName("journal-row")
        if j.get("message"):
            lay.addWidget(plain_label(F.text(j.get("message")), muted=True))
        msg = ResultArea("journal-msg")
        client = self.ctx.client

        def act(fn: Callable[[], dict[str, Any]]) -> None:
            msg.set(status_label("loading", "Asking the exchange…"))

            def ok(r: dict[str, Any]) -> None:
                st = F.text(r.get("state"), "")
                note = F.text(r.get("note"), "")
                msg.set(status_label("info" if st == "UNKNOWN" else "ok", st + (f" — {note}" if note else "")))
            self.ctx.run(fn, ok, lambda e: msg.set(self.ctx.error_widget(e)))
        check = QPushButton("Check status")
        check.setAccessibleName(f"Check status of {summary}")
        check.clicked.connect(lambda: act(lambda: client.reconcile(coid)))
        buttons = [check]
        if state in ("NEW", "PARTIALLY_FILLED"):
            cancel = QPushButton("Cancel order")
            cancel.setAccessibleName(f"Cancel {summary}")

            def do_cancel() -> None:
                if self.ctx.confirm(self, "Cancel testnet order", "Cancel this testnet order?", summary,
                                    "Yes, cancel the order"):
                    act(lambda: client.cancel_order(coid))
            cancel.clicked.connect(do_cancel)
            buttons.append(cancel)
        lay.addWidget(_row(*buttons))
        lay.addWidget(msg)
        return box

    def _markets_for(self, conn: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        """Only the markets the connected exchange supports (e.g. stock for IBKR paper), as the extension does."""
        info = next((e for e in _dicts((self.catalog or {}).get("exchanges")) if e.get("id") == conn.get("exchange")), {})
        offered = set(F.str_list(info.get("markets"))) or {"spot", "usdm_perpetual"}
        return tuple(c for c in MARKET_CHOICES if c[0] in offered) or MARKET_CHOICES[:2]

    def _ticket(self, conn: dict[str, Any]) -> QWidget:
        box, lay = card("Testnet order")
        box.setObjectName("order-ticket")
        lay.addWidget(plain_label("Sent only to this testnet/demo account, and only after you review the preview and "
                                  "confirm."))
        symbol = QLineEdit(self.ctx.symbol or "BTCUSDT")
        symbol.setObjectName("order-symbol")
        symbol.setMaxLength(30)
        market = _combo(self._markets_for(conn), self.ctx.market)
        market.setObjectName("order-market")
        side = _combo((("buy", "Buy"), ("sell", "Sell")))
        side.setObjectName("order-side")
        otype = _combo((("limit", "Limit"), ("market", "Market")))
        otype.setObjectName("order-type")
        qty = QLineEdit()
        qty.setObjectName("order-quantity")
        qty.setMaxLength(40)
        price = QLineEdit()
        price.setObjectName("order-price")
        price.setMaxLength(40)
        reduce = QCheckBox("Reduce only (futures)")
        reduce.setObjectName("order-reduce-only")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow(buddy_label("S&ymbol", symbol), symbol)
        form.addRow(buddy_label("Mar&ket", market), market)
        form.addRow(buddy_label("Si&de", side), side)
        form.addRow(buddy_label("&Type", otype), otype)
        form.addRow(buddy_label("&Quantity", qty), qty)
        form.addRow(buddy_label("Limit &price", price), price)
        form.addRow("", reduce)
        lay.addLayout(form)
        otype.currentIndexChanged.connect(lambda _i: form.setRowVisible(price, otype.currentData() == "limit"))
        preview_btn = QPushButton("Preview order")
        preview_btn.setObjectName("order-preview-btn")
        lay.addWidget(_row(preview_btn))
        out = ResultArea("order-preview")
        lay.addWidget(out)
        cid, client = F.text(conn.get("connection_id"), ""), self.ctx.client

        def preview() -> None:
            self._stop_countdown()
            sym = F.normalize_symbol(symbol.text())
            if not sym:
                return out.set(status_label("error", "Enter a symbol of 3 to 20 letters or digits, for example BTCUSDT."))
            symbol.setText(sym)
            q = qty.text().strip()
            if not q:
                return out.set(status_label("error", "Enter a quantity."))
            draft: dict[str, Any] = {"symbol": sym, "market": market.currentData(), "side": side.currentData(),
                                     "type": otype.currentData(), "quantity": q}
            if otype.currentData() == "limit":
                draft["price"] = price.text().strip() or None
            if reduce.isChecked():
                draft["reduce_only"] = True
            self._preview_seq += 1
            mine, gen = self._preview_seq, self._seq
            out.set(status_label("loading", "Checking the order against exchange rules…"))

            def ok(p: dict[str, Any]) -> None:
                if mine == self._preview_seq and self._fresh(gen):
                    out.set(self._preview_card(p, out))

            def err(e: Exception) -> None:
                if mine == self._preview_seq and self._fresh(gen):
                    out.set(self.ctx.error_widget(e))
            self.ctx.run(lambda: client.preview_order(cid, draft), ok, err)
        preview_btn.clicked.connect(preview)
        return box

    def _preview_card(self, p: dict[str, Any], out: ResultArea) -> QWidget:
        order = p.get("order") if isinstance(p.get("order"), dict) else {}
        box, lay = card(F.preview_title(order))
        box.setObjectName("preview-card")
        lay.addWidget(plain_label(f"Testnet · {F.text(p.get('exchange'))} · client order id "
                                  f"{F.text(order.get('client_order_id'))}", muted=True))
        lay.addWidget(plain_label("Exchange rule checks", bold=True))
        lay.addWidget(bullet_list([f"{'OK' if c.get('ok') else 'FAILED'} — {F.text(c.get('detail'))}"
                                   for c in _dicts(p.get("checks"))]))
        lay.addWidget(plain_label("Estimates", bold=True))
        lay.addWidget(bullet_list(F.estimate_lines(p.get("estimates"))))
        warnings = F.str_list(p.get("warnings"))
        if warnings:
            w, wl = card("Warnings")
            w.setObjectName("preview-warnings")
            wl.addWidget(bullet_list(warnings))
            lay.addWidget(w)
        place = QPushButton("Place testnet order")
        place.setObjectName("place-order")
        expires = p.get("expires_in_s")
        remaining = math.floor(expires) if isinstance(expires, (int, float)) and math.isfinite(expires) else 0
        left = plain_label("", name="preview-countdown")
        lay.addWidget(_row(place, left))
        preview_id, token = F.text(p.get("preview_id"), ""), p.get("confirm_token")  # token: memory only, never shown
        client, gen = self.ctx.client, self._seq

        def submit() -> None:
            self._stop_countdown()
            place.setEnabled(False)
            out_ok = lambda r: out.set(  # noqa: E731
                status_label("info" if r.get("state") in ("UNKNOWN", "NOT_FOUND") else "ok",
                             f"{F.text(r.get('state'), '')}: {F.text(r.get('note'), '')}"),
                _row(refresh))
            refresh = QPushButton("Refresh orders")
            refresh.clicked.connect(self.render)
            self.ctx.run(lambda: client.submit_order(preview_id, token if isinstance(token, str) else ""),
                         lambda r: self._fresh(gen) and out_ok(r),
                         lambda e: self._fresh(gen) and out.set(self.ctx.error_widget(e)))
        place.clicked.connect(submit)
        self._start_countdown(place, left, remaining)
        return box

    # ---- confirmation countdown ----
    def _start_countdown(self, place: QPushButton, left, remaining: int) -> None:
        self._place_btn, self._countdown_label, self._remaining = place, left, remaining
        if remaining <= 0:
            return self._expire()
        left.setText(f"Confirm within {remaining} s")
        self.timer.start()

    def _tick(self) -> None:
        if self._place_btn is None or self._countdown_label is None:
            return self.timer.stop()
        self._remaining -= 1
        if self._remaining <= 0:
            return self._expire()
        try:
            self._countdown_label.setText(f"Confirm within {self._remaining} s")
        except RuntimeError:  # the preview was replaced while the timer ran
            self._stop_countdown()

    def _expire(self) -> None:
        place, left = self._place_btn, self._countdown_label
        self._stop_countdown()
        try:
            if place is not None:
                place.setEnabled(False)
            if left is not None:
                left.setText("Expired — preview again")
        except RuntimeError:
            pass

    def _stop_countdown(self) -> None:
        self.timer.stop()
        self._place_btn = self._countdown_label = None

    # ---------------------------------------------------------------- performance
    def _render_performance(self, body: ResultArea) -> None:
        box, lay = card("Performance from your fills")
        box.setObjectName("performance-form")
        pick = self._connection_picker("performance-connection")
        symbol = QLineEdit(self.ctx.symbol or "BTCUSDT")
        symbol.setObjectName("performance-symbol")
        symbol.setMaxLength(30)
        market = _combo(MARKET_CHOICES, self.ctx.market)
        market.setObjectName("performance-market")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow(buddy_label("&Connection", pick), pick)
        form.addRow(buddy_label("S&ymbol", symbol), symbol)
        form.addRow(buddy_label("Mar&ket", market), market)
        lay.addLayout(form)
        run = QPushButton("Calculate")
        run.setObjectName("performance-run")
        lay.addWidget(_row(run))
        out = ResultArea("performance-out")
        client = self.ctx.client

        def go() -> None:
            sym = F.normalize_symbol(symbol.text())
            if not sym:
                return out.set(status_label("error", "Enter a symbol of 3 to 20 letters or digits, for example BTCUSDT."))
            symbol.setText(sym)
            cid, mk, gen = pick.currentData(), market.currentData(), self._seq
            out.set(status_label("loading", "Reading your fills…"))

            def ok(r: dict[str, Any]) -> None:
                if not self._fresh(gen):
                    return
                if not r.get("trades"):
                    return out.set(plain_label(F.text(r.get("note"), "No closed trades."), muted=True))
                c, cl = card(f"{sym} · {F.market_label(mk)}")
                c.setObjectName("performance-result")
                cl.addWidget(kv_form(F.performance_rows(r)))
                if r.get("warning"):
                    cl.addWidget(status_label("info", F.text(r.get("warning"))))
                unmatched = r.get("unmatched_sells")
                if unmatched:
                    cl.addWidget(plain_label(f"{F.text(unmatched)} sells had no earlier buy in the loaded history and "
                                             "were not counted as trades.", muted=True))
                other = r.get("fills_with_fees_in_other_assets")
                if other:
                    cl.addWidget(plain_label(f"{F.text(other)} fills paid fees in another asset (not subtracted).",
                                             muted=True))
                prov = ", ".join(F.provenance_label(x) for x in F.str_list(r.get("provenance")))
                cl.addWidget(plain_label(f"{prov}. {F.text(r.get('note'), '')}", muted=True))
                out.set(c)

            def err(e: Exception) -> None:
                if self._fresh(gen):
                    out.set(self.ctx.error_widget(e))
            self.ctx.run(lambda: client.performance(cid, sym, mk), ok, err)
        run.clicked.connect(go)
        body.set(box, out)
