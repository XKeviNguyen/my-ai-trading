"""Position size from a risk budget (POST /api/v1/risk/size). Pure arithmetic on the local engine: no market data,
no orders. The stop is an input and is never widened to fit; a stop too far away gives a smaller size."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..context import AppContext
from ..formatting import RISK_FIELDS as FIELDS, risk_request as to_request, risk_result_rows as result_rows
from ..formatting import validate_risk as validate
from ..widgets import ResultArea, buddy_label, card, kv_form, plain_label, scroll_page, status_label

class RiskView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(plain_label("Size a position so that hitting the stop loses a fixed share of capital. "
                                 "Nothing is sent to an exchange.", muted=True))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.edits: dict[str, QLineEdit] = {}
        self.errors = ResultArea("risk-errors")
        for key, label, hint in FIELDS:
            e = QLineEdit("0.8" if key == "risk_pct" else "")
            e.setPlaceholderText(hint)
            e.setObjectName(f"risk-{key}")
            e.setAccessibleName(label)
            e.returnPressed.connect(self.calculate)
            self.edits[key] = e
            form.addRow(buddy_label(label, e), e)
        il.addLayout(form)
        bar = QHBoxLayout()
        self.calc_btn = QPushButton("&Calculate")
        self.calc_btn.clicked.connect(self.calculate)
        bar.addWidget(self.calc_btn)
        bar.addStretch(1)
        il.addLayout(bar)
        il.addWidget(self.errors)
        self.result = ResultArea("risk-result")
        il.addWidget(self.result)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)

    def show_tab(self) -> None:
        pass  # no autofocus: this also runs when arrow keys move along the tab bar, and would trap them

    def values(self) -> dict[str, str]:
        return {k: e.text() for k, e in self.edits.items()}

    def calculate(self) -> None:
        v = self.values()
        errors = validate(v)
        for key, edit in self.edits.items():  # tie each message to its field for screen readers
            edit.setAccessibleDescription(errors.get(key, ""))
            edit.setProperty("invalid", key in errors)
            edit.style().unpolish(edit)
            edit.style().polish(edit)
        if errors:
            labels = {k: label for k, label, _ in FIELDS}
            self.errors.set(*[status_label("error", f"{labels[k]}: {msg}") for k, msg in errors.items()])
            self.result.set(None)
            self.edits[next(k for k, _, _ in FIELDS if k in errors)].setFocus()
            return
        self.errors.set(None)
        self.result.set(status_label("loading", "Calculating…"))
        self.ctx.run(lambda: self.ctx.client.risk_size(to_request(v)), self.render,
                     lambda e: self.result.set(self.ctx.error_widget(e)))

    def render(self, r: dict[str, Any]) -> None:
        box, lay = card("Position size")
        box.setObjectName("risk-card")
        lay.addWidget(kv_form(result_rows(r)))
        if str(r.get("quantity")) in ("0", "0.0"):
            lay.addWidget(status_label("error", str(r.get("reason") or "The stop is too far for this budget.")))
        rr = r.get("reward_risk")
        if rr is not None and Decimal(str(rr)) < 2:
            lay.addWidget(plain_label("Reward is less than twice the risk; both playbooks look for about 2–3 : 1.",
                                      muted=True))
        lay.addWidget(plain_label(f"{r.get('note', '')} Fees and slippage are not included.", muted=True))
        self.result.set(box)
