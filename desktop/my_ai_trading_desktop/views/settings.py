"""Settings: API token + connection test, watchlist, analysis timeframe, quiet hours, alerts pause,
AI consent / deep-analysis opt-in and monthly budgets. Saved with PUT /api/v1/settings (full object)."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from .. import formatting as F
from ..client import TIMEFRAMES, UnauthorizedError, is_valid_token
from ..context import AppContext
from ..widgets import ResultArea, buddy_label, card, clear_layout, explained_check, plain_label, scroll_page, status_label


class SettingsView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.wl_rows: list[tuple[QWidget, QLineEdit, QComboBox]] = []
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.addWidget(self._connection())
        self.prefs = ResultArea("prefs")
        lay.addWidget(self.prefs)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll_page(inner))

    def show_tab(self) -> None:
        self.load_prefs()

    # ---- connection ----
    def _connection(self) -> QWidget:
        box, lay = card("Connection")
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Paste token (optional)")
        self.token_input.setObjectName("token-input")
        form = QFormLayout()
        form.addRow(buddy_label("Local API &token", self.token_input), self.token_input)
        lay.addLayout(form)
        lay.addWidget(plain_label(
            "By default the app reads the token file the engine creates (run `my-ai-trading token` to print its path, "
            "normally ~/.config/my-ai-trading/api_token). A pasted token is kept in memory for this session only and "
            "is sent only to http://127.0.0.1:8765.", muted=True))
        self.token_state = plain_label("", name="token-state")
        lay.addWidget(self.token_state)
        row = QHBoxLayout()
        use = QPushButton("Use pasted token")
        use.clicked.connect(self.use_pasted)
        reload_file = QPushButton("Use token file")
        reload_file.clicked.connect(self.use_file)
        test = QPushButton("Test &connection")
        test.clicked.connect(self.test_connection)
        for b in (use, reload_file, test):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.conn_out = ResultArea("conn-out")
        lay.addWidget(self.conn_out)
        self.refresh_token_state()
        return box

    def refresh_token_state(self) -> None:
        self.token_state.setText(self.ctx.tokens.describe())

    def use_pasted(self) -> None:
        t = self.token_input.text().strip()
        self.token_input.clear()
        if not is_valid_token(t):
            self.conn_out.set(status_label("error", "That does not look like an API token (expected 32+ printable "
                                                    "characters, no spaces)."))
            return
        self.ctx.tokens.set_pasted(t)
        self.refresh_token_state()
        self.test_connection()
        self.load_prefs()

    def use_file(self) -> None:
        self.ctx.tokens.set_pasted(None)
        self.refresh_token_state()
        self.test_connection()

    def test_connection(self) -> None:
        self.conn_out.set(status_label("loading", "Checking /health…"))

        def job() -> dict[str, Any]:
            health = self.ctx.client.health()
            self.ctx.client.get_settings()
            return health

        def ok(h: dict[str, Any]) -> None:
            self.conn_out.set(status_label("ok", f"Connected to engine {F.text(h.get('version'))} "
                                                 f"(read-only: {F.text(h.get('read_only'))}); token accepted."))
        self.ctx.run(job, ok, lambda e: self.conn_out.set(self.ctx.error_widget(e)))

    # ---- preferences ----
    def load_prefs(self) -> None:
        self.prefs.set(status_label("loading", "Loading settings…"))

        def ok(s: dict[str, Any]) -> None:
            self.ctx.settings_cache = s
            self.render_prefs(s)

        def err(e: Exception) -> None:
            if isinstance(e, UnauthorizedError):
                self.prefs.set(status_label("info", "Load a valid token to see your settings."))
            else:
                self.prefs.set(self.ctx.error_widget(e))
        self.ctx.run(self.ctx.client.get_settings, ok, err)

    def render_prefs(self, s: dict[str, Any]) -> None:
        w = QWidget()
        w.setObjectName("prefs-form")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        a_box, a_lay = card("Analysis")
        self.atf = QComboBox()
        for tf in TIMEFRAMES:
            self.atf.addItem(tf, tf)
        idx = self.atf.findData(s.get("analysis_timeframe"))
        self.atf.setCurrentIndex(max(idx, 0))
        f = QFormLayout()
        f.addRow(buddy_label("Default &analysis timeframe", self.atf), self.atf)
        a_lay.addLayout(f)
        a_lay.addWidget(plain_label("The timeframe you are viewing; methodologies still evaluate on their own native "
                                    "timeframe.", muted=True))
        lay.addWidget(a_box)

        wl_box, wl_lay = card("Watchlist (monitored by the local engine)")
        self.wl_body = QVBoxLayout()
        self.wl_rows = []
        for item in s.get("watchlist") or []:
            if isinstance(item, dict):
                self.add_watch_row(F.text(item.get("symbol"), ""), F.text(item.get("market"), "usdm_perpetual"))
        wl_lay.addLayout(self.wl_body)
        add = QPushButton("Add symbol")
        add.clicked.connect(lambda: self.add_watch_row("", "usdm_perpetual"))
        wl_lay.addWidget(add)
        lay.addWidget(wl_box)

        al_box, al_lay = card("Alerts")
        self.paused = QCheckBox("Pause all alerts")
        self.paused.setChecked(bool(s.get("alerts_paused")))
        al_lay.addWidget(self.paused)
        qh = s.get("quiet_hours_utc")
        self.quiet_on = explained_check(al_lay, "Quiet hours (UTC)", "Mark alerts as quiet during these hours.")
        self.quiet_on.setChecked(isinstance(qh, list) and len(qh) == 2)
        self.q_start, self.q_end = QSpinBox(), QSpinBox()
        for sb, val in ((self.q_start, qh[0] if self.quiet_on.isChecked() else 22),
                        (self.q_end, qh[1] if self.quiet_on.isChecked() else 7)):
            sb.setRange(0, 23)
            sb.setValue(int(val) if isinstance(val, int) else 0)
        qf = QFormLayout()
        qf.addRow(buddy_label("From hour", self.q_start), self.q_start)
        qf.addRow(buddy_label("To hour (exclusive)", self.q_end), self.q_end)
        al_lay.addLayout(qf)
        lay.addWidget(al_box)

        ai_box, ai_lay = card("AI")
        self.consent = explained_check(ai_lay, "AI data consent",
                                       "I consent to sending market context (snapshot, perspectives, my question or "
                                       "thesis, screenshots I choose) to the configured AI provider.")
        self.consent.setChecked(bool(s.get("llm_data_consent")))
        ai_lay.addWidget(plain_label("Without consent every answer is deterministic and costs nothing. Your API token "
                                     "and keys are never sent.", muted=True))
        self.deep = explained_check(ai_lay, "Allow deep analysis",
                                    "Higher effort; up to ~16k output tokens per request.")
        self.deep.setChecked(bool(s.get("deep_analysis_opt_in")))
        ai_lay.addWidget(plain_label("Deep analysis runs only when you tick it on a request, and only within the "
                                     "budgets below.", muted=True))
        self.tok_budget = QSpinBox()
        self.tok_budget.setRange(0, 100_000_000)
        self.tok_budget.setGroupSeparatorShown(True)
        self.tok_budget.setValue(int(s.get("monthly_ai_token_budget") or 0))
        self.usd_budget = QDoubleSpinBox()
        self.usd_budget.setRange(0, 10_000)
        self.usd_budget.setDecimals(2)
        self.usd_budget.setValue(float(s.get("monthly_ai_budget_usd") or 0))
        bf = QFormLayout()
        bf.addRow(buddy_label("Monthly token budget", self.tok_budget), self.tok_budget)
        bf.addRow(buddy_label("Monthly USD budget (0 = no USD cap)", self.usd_budget), self.usd_budget)
        ai_lay.addLayout(bf)
        lay.addWidget(ai_box)

        self.save_btn = QPushButton("&Save settings")
        self.save_btn.clicked.connect(self.save)
        lay.addWidget(self.save_btn)
        self.save_out = ResultArea("settings-out")
        lay.addWidget(self.save_out)
        self.prefs.set(w)

    def add_watch_row(self, symbol: str, market: str) -> None:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        sym = QLineEdit(symbol)
        sym.setMaxLength(24)
        sym.setPlaceholderText("SANDUSDT")
        sym.setAccessibleName("Watchlist symbol")
        mk = QComboBox()
        mk.addItem("USD-M perp", "usdm_perpetual")
        mk.addItem("Spot", "spot")
        mk.setCurrentIndex(1 if market == "spot" else 0)
        mk.setAccessibleName("Watchlist market")
        rm = QPushButton("Remove")
        rm.setAccessibleName("Remove watchlist entry")
        entry = (row, sym, mk)

        def remove() -> None:
            if entry in self.wl_rows:
                self.wl_rows.remove(entry)
            clear_layout(rl)
            row.setParent(None)
            row.deleteLater()
        rm.clicked.connect(remove)
        rl.addWidget(sym, 1)
        rl.addWidget(mk)
        rl.addWidget(rm)
        self.wl_body.addWidget(row)
        self.wl_rows.append(entry)

    def save(self) -> None:
        items, errors = F.clean_watchlist([(sym.text(), mk.currentData()) for _, sym, mk in self.wl_rows])
        if errors:
            return self.save_out.set(status_label("error", " ".join(errors)))
        values = {
            "analysis_timeframe": self.atf.currentData(),
            "watchlist": items,
            "quiet_hours_utc": [self.q_start.value(), self.q_end.value()] if self.quiet_on.isChecked() else None,
            "alerts_paused": self.paused.isChecked(),
            "llm_data_consent": self.consent.isChecked(),
            "deep_analysis_opt_in": self.deep.isChecked(),
            "monthly_ai_token_budget": self.tok_budget.value(),
            "monthly_ai_budget_usd": round(self.usd_budget.value(), 2),
        }
        self.save_btn.setEnabled(False)
        self.save_out.set(status_label("loading", "Saving…"))

        def ok(_s: dict[str, Any]) -> None:
            self.save_btn.setEnabled(True)
            self.save_out.set(status_label("ok", "Settings saved."))

        def err(e: Exception) -> None:
            self.save_btn.setEnabled(True)
            self.save_out.set(self.ctx.error_widget(e))
        self.ctx.update_settings_async(lambda cur: cur.update(values), ok, err)
