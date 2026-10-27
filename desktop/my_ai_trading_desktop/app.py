"""Main window and entry point for the native desktop client of the local My AI Trading engine."""
from __future__ import annotations

import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QStyle, QSystemTrayIcon,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__, theme
from . import formatting as F
from .client import DEFAULT_PORT, MARKETS, TIMEFRAMES
from .context import AppContext
from .views.accounts import AccountsView
from .views.alerts import AlertsView
from .views.analysis import AnalysisView
from .views.copilot import CopilotView
from .views.methods import MethodsView
from .views.reports import ReportsView
from .views.settings import SettingsView
from .views.studio import StudioView
from .views.usage import UsageView
from .widgets import buddy_label, status_label

TAB_ORDER = ("analysis", "copilot", "studio", "accounts", "alerts", "reports", "methods", "usage", "settings")
TAB_TITLES = {"analysis": "Analysis", "copilot": "Copilot", "studio": "Strategy Studio", "accounts": "Accounts",
              "alerts": "Alerts",
              "reports": "Reports", "methods": "Methods", "usage": "Usage", "settings": "Settings"}


class MainWindow(QMainWindow):
    def __init__(self, *, port: int = DEFAULT_PORT, qsettings: QSettings | None = None,
                 start_polling: bool = True):
        super().__init__()
        self.setWindowTitle(f"My AI Trading — research only (v{__version__})")
        self.setMinimumSize(360, 420)
        self.ctx = AppContext(port=port, settings=qsettings, parent=self)
        self.ctx.select_tab = self.select_tab

        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
            self.tray.setToolTip("My AI Trading")
            self.tray.show()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.addWidget(self._instrument_bar())
        rule = QFrame()  # the theme paints it as the rainbow neon line (theme.py); plain line without the theme
        rule.setObjectName("rainbow-rule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(2)
        outer.addWidget(rule)
        self.bar_msg = status_label("info", "")
        self.bar_msg.setVisible(False)
        outer.addWidget(self.bar_msg)

        self.views = {
            "analysis": AnalysisView(self.ctx),
            "copilot": CopilotView(self.ctx),
            "studio": StudioView(self.ctx),
            "accounts": AccountsView(self.ctx),
            "alerts": AlertsView(self.ctx, notify=self.notify, tray_available=self.tray is not None),
            "reports": ReportsView(self.ctx),
            "methods": MethodsView(self.ctx),
            "usage": UsageView(self.ctx),
            "settings": SettingsView(self.ctx),
        }
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Sections")
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setDocumentMode(True)
        for i, key in enumerate(TAB_ORDER):
            self.tabs.addTab(self.views[key], TAB_TITLES[key])
            self.tabs.setTabToolTip(i, f"{TAB_TITLES[key]} (Ctrl+{i + 1})")
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda k=key: self.select_tab(k))
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._restore()
        if start_polling:
            self.views["alerts"].start()

    # ---- instrument bar ----
    def _instrument_bar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("instrument-bar")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.symbol_edit = QLineEdit()
        self.symbol_edit.setPlaceholderText("SANDUSDT")
        self.symbol_edit.setMaxLength(24)
        self.symbol_edit.setMinimumWidth(self.symbol_edit.fontMetrics().horizontalAdvance("SANDUSDTXX"))
        self.symbol_edit.returnPressed.connect(self.apply_instrument)
        self.market_combo = QComboBox()
        for m in MARKETS:
            self.market_combo.addItem(F.market_label(m), m)
        self.tf_combo = QComboBox()
        for tf in TIMEFRAMES:
            self.tf_combo.addItem(tf, tf)
        self.load_btn = QPushButton("&Load")
        self.load_btn.clicked.connect(self.apply_instrument)
        # Two rows so the bar stays readable in narrow windows and at large font sizes.
        top, bottom = QHBoxLayout(), QHBoxLayout()
        top.addWidget(buddy_label("S&ymbol", self.symbol_edit))
        top.addWidget(self.symbol_edit, 1)
        top.addWidget(buddy_label("&Market", self.market_combo))
        top.addWidget(self.market_combo)
        bottom.addWidget(buddy_label("Analysis timefram&e", self.tf_combo))
        bottom.addWidget(self.tf_combo)
        bottom.addStretch(1)
        bottom.addWidget(self.load_btn)
        lay.addLayout(top)
        lay.addLayout(bottom)
        self.symbol_edit.setAccessibleName("Symbol")
        self.market_combo.setAccessibleName("Market")
        self.tf_combo.setAccessibleName("Analysis timeframe")
        return w

    def _restore(self) -> None:
        qs = self.ctx.qsettings
        sym = qs.value("instrument/symbol", "", type=str)
        market = qs.value("instrument/market", "usdm_perpetual", type=str)
        tf = qs.value("instrument/analysis_tf", "1h", type=str)
        self.market_combo.setCurrentIndex(max(self.market_combo.findData(market), 0))
        self.tf_combo.setCurrentIndex(max(self.tf_combo.findData(tf), 0))
        norm = F.normalize_symbol(sym) if sym else None
        if norm:
            self.symbol_edit.setText(norm)
            self.ctx.symbol, self.ctx.market, self.ctx.analysis_tf = norm, self.market_combo.currentData(), \
                self.tf_combo.currentData()
            self.ctx.instrument_changed.emit()
        geom = qs.value("window/geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        else:
            self.resize(900, 760)

    def apply_instrument(self) -> None:
        raw = self.symbol_edit.text()
        sym = F.normalize_symbol(raw)
        if not sym:
            self.bar_msg.setText("Error: Enter a symbol of 3 to 20 letters or digits, for example SANDUSDT.")
            self.bar_msg.setVisible(True)
            return
        self.bar_msg.setVisible(False)
        self.symbol_edit.setText(sym)
        changed = (sym, self.market_combo.currentData(), self.tf_combo.currentData()) != \
            (self.ctx.symbol, self.ctx.market, self.ctx.analysis_tf)
        self.ctx.set_instrument(sym, self.market_combo.currentData(), self.tf_combo.currentData())
        if not changed or self.tabs.currentWidget() is not self.views["analysis"]:
            self.select_tab("analysis")
            if not changed:
                self.views["analysis"].load()

    # ---- tabs ----
    def select_tab(self, key: str) -> None:
        view = self.views.get(key)
        if view is not None:
            self.tabs.setCurrentWidget(view)
            view.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _tab_changed(self, _index: int) -> None:
        view = self.tabs.currentWidget()
        show = getattr(view, "show_tab", None)
        if callable(show):
            show()

    # ---- notifications ----
    def notify(self, title: str, body: str, priority: int) -> None:
        if self.tray is not None:
            icon = QSystemTrayIcon.MessageIcon.Warning if priority >= 3 else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, body, icon, 10_000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.ctx.qsettings.setValue("window/geometry", self.saveGeometry())
        self.views["alerts"].stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("My AI Trading Desktop")
    app.setOrganizationName("my-ai-trading")
    theme.apply(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
