"""AI usage and estimated cost (GET /api/v1/usage)."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import formatting as F
from ..context import AppContext
from ..widgets import ResultArea, card, kv_form, plain_label, scroll_page, status_label

COLUMNS = ["Feature", "Model", "Calls", "In", "Out", "Est. cost", "Avg ms"]


def usage_table(rows: list[dict[str, Any]]) -> QTableWidget:
    t = QTableWidget(len(rows), len(COLUMNS))
    t.setObjectName("usage-table")
    t.setAccessibleName("Usage by feature and model")
    t.setHorizontalHeaderLabels(COLUMNS)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setTabKeyNavigation(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for i, r in enumerate(rows):
        for j, value in enumerate(F.usage_table_row(r)):
            t.setItem(i, j, QTableWidgetItem(value))  # table items are plain text
    t.setMinimumHeight(min(400, 40 + 30 * len(rows)))
    return t


class UsageView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        refresh = QPushButton("&Refresh usage")
        refresh.clicked.connect(self.load)
        bar.addWidget(refresh)
        bar.addStretch(1)
        outer.addLayout(bar)
        self.result = ResultArea("usage-result")
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(self.result)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)

    def show_tab(self) -> None:
        self.load()

    def load(self) -> None:
        self.result.set(status_label("loading", "Loading usage…"))
        self.ctx.run(self.ctx.client.usage, self.render, lambda e: self.result.set(self.ctx.error_widget(e)))

    def render(self, u: dict[str, Any]) -> None:
        box, lay = card(f"Since {F.fmt_ms(u.get('since_ms'))}")
        box.setObjectName("usage-totals")
        lay.addWidget(kv_form(F.usage_rows(u)))
        if u.get("note"):
            lay.addWidget(plain_label(F.text(u.get("note")), muted=True))
        rows = [r for r in u.get("by_feature_model") or [] if isinstance(r, dict)]
        table: QWidget = usage_table(rows) if rows else \
            plain_label("No AI calls this month. Deterministic analysis costs no tokens.", muted=True)
        self.result.set(box, table)
