"""Alerts feed: polls GET /api/v1/alerts?after_seq=N every 30 s while the app runs.

The first successful poll only sets a baseline; later non-quiet alerts can raise a tray notification."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import formatting as F
from ..client import UnauthorizedError
from ..context import AppContext
from ..widgets import ResultArea, card, explained_check, plain_label, scroll_page, status_label

POLL_MS = 30_000


class AlertsView(QWidget):
    def __init__(self, ctx: AppContext, notify: Callable[[str, str, int], None] | None = None,
                 tray_available: bool = False):
        super().__init__()
        self.ctx = ctx
        self.notify = notify
        self.feed = F.AlertFeed()
        self.inflight = False
        self.last_status: tuple[float, bool, str] | None = None
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        check = QPushButton("&Check now")
        check.clicked.connect(self.poll)
        clear = QPushButton("Clear list")
        clear.clicked.connect(self.reset)
        bar.addWidget(check)
        bar.addWidget(clear)
        bar.addStretch(1)
        outer.addLayout(bar)
        self.notify_box = explained_check(outer, "Desktop notifications", "Only for non-quiet alerts, after the "
                                          "first check (which sets a baseline). Plain text only.")
        self.notify_box.setChecked(ctx.qsettings.value("alerts/notify", False, type=bool))
        self.notify_box.toggled.connect(lambda on: ctx.qsettings.setValue("alerts/notify", on))
        if not tray_available:
            self.notify_box.setEnabled(False)
            self.notify_box.setText("Desktop notifications (no system tray available)")
        self.status_box = ResultArea("alerts-status")
        outer.addWidget(self.status_box)
        self.list_box = ResultArea("alerts-list")
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(self.list_box)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.render()

    def start(self) -> None:
        self.timer.start()
        self.poll()

    def stop(self) -> None:
        self.timer.stop()

    def show_tab(self) -> None:
        self.poll()

    def reset(self) -> None:
        self.feed.reset()
        self.render()
        self.poll()

    def poll(self) -> None:
        if self.inflight:
            return
        self.inflight = True
        after = self.feed.last_seq

        def ok(r: dict[str, Any]) -> None:
            self.inflight = False
            before = self.feed.last_seq
            loud = self.feed.apply(r)
            new_count = sum(1 for a in self.feed.alerts if a["seq"] > before) if before else 0
            self.last_status = (time.time() * 1000, True, f"{new_count} new" if new_count else "")
            if loud and self.notify and self.notify_box.isEnabled() and self.notify_box.isChecked():
                for a in loud:
                    title, body = F.notification_text(a)
                    self.notify(title, body, a.get("priority", 0) if isinstance(a.get("priority"), int) else 0)
            self.render()

        def err(e: Exception) -> None:
            self.inflight = False
            self.last_status = (time.time() * 1000, False, str(e))
            self.render(unauthorized=isinstance(e, UnauthorizedError))
        self.ctx.run(lambda: self.ctx.client.alerts(after, 100), ok, err)

    def render(self, unauthorized: bool = False) -> None:
        now = time.time() * 1000
        if self.last_status is None:
            self.status_box.set(status_label("info", "Not checked yet."))
        else:
            at, ok, info = self.last_status
            if ok:
                self.status_box.set(status_label("ok", f"Last checked {F.fmt_age(at, now)}" + (f" · {info}" if info else "") + "."))
            else:
                w = QWidget()
                lay = QHBoxLayout(w)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.addWidget(status_label("error", f"Last check {F.fmt_age(at, now)} failed: {info}"), 1)
                if unauthorized:
                    b = QPushButton("Open Settings")
                    b.clicked.connect(lambda: self.ctx.select_tab("settings"))
                    lay.addWidget(b)
                self.status_box.set(w)
        if not self.feed.alerts:
            self.list_box.set(plain_label(
                "No alerts yet. Alerts come from the local monitor (`my-ai-trading serve`) for your watchlist, "
                "approved strategies and methodology state changes.", muted=True))
            return
        self.list_box.set(*(self._item(a, now) for a in self.feed.alerts))

    def _item(self, a: dict[str, Any], now: float) -> QWidget:
        quiet = F.is_quiet(a)
        prio = a.get("priority") if isinstance(a.get("priority"), int) else 0
        title = f"{F.symbol_from_key(F.text(a.get('instrument_key'), ''))} {F.text(a.get('timeframe'), '')} · " \
                f"{F.text(a.get('event_type'))}" + (" · high priority" if prio >= 3 else "") + \
                (" · quiet hours" if quiet else "")
        box, lay = card(title)
        box.setObjectName("alert-quiet" if quiet else "alert")
        lay.addWidget(plain_label(F.alert_summary(a) or F.DASH, muted=quiet))
        lay.addWidget(plain_label(f"source {F.text(a.get('source'))} · bar opened {F.fmt_ms(a.get('bar_open_time_ms'))} · "
                                  f"recorded {F.fmt_age(a.get('created_at'), now)} · #{F.text(a.get('seq'))}", muted=True))
        return box
