"""Small widget helpers. Every piece of API/model/user text goes through these as PLAIN text."""
from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QLabel, QLayout, QMessageBox, QPlainTextEdit, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

STATUS_PREFIX = {"loading": "Working: ", "ok": "Done: ", "error": "Error: ", "info": "Note: "}


def plain_label(text: str, *, wrap: bool = True, selectable: bool = True, name: str | None = None,
                muted: bool = False, bold: bool = False) -> QLabel:
    lab = QLabel()
    lab.setTextFormat(Qt.TextFormat.PlainText)  # never interpret API strings as markup
    lab.setText(text)
    lab.setWordWrap(wrap)
    lab.setOpenExternalLinks(False)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                    | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    if name:
        lab.setObjectName(name)
    if muted:
        f = lab.font()
        f.setItalic(True)
        lab.setFont(f)
    if bold:
        f = lab.font()
        f.setBold(True)
        lab.setFont(f)
    lab.setAccessibleName(text[:200])
    return lab


def buddy_label(text: str, buddy: QWidget) -> QLabel:
    """A field label with an Alt+letter mnemonic (`&Name`) that moves focus to `buddy`."""
    lab = QLabel()
    lab.setTextFormat(Qt.TextFormat.PlainText)
    lab.setText(text)
    lab.setBuddy(buddy)
    if not buddy.accessibleName():
        buddy.setAccessibleName(text.replace("&", ""))
    return lab


def explained_check(layout: QLayout, label: str, explanation: str) -> QCheckBox:
    """Checkbox with a short label plus a wrapping explanation (checkbox text itself never wraps)."""
    cb = QCheckBox(label)
    cb.setAccessibleDescription(explanation)
    layout.addWidget(cb)
    note = plain_label(explanation, muted=True)
    note.setIndent(24)
    layout.addWidget(note)
    return cb


def heading(text: str, scale: float = 1.15) -> QLabel:
    lab = plain_label(text, selectable=False)
    f = QFont(lab.font())
    if f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * scale)  # relative to the system font: scales with it
    f.setBold(True)
    lab.setFont(f)
    lab.setAccessibleName(text[:200])
    return lab


def status_label(kind: str, message: str) -> QLabel:
    lab = plain_label(STATUS_PREFIX.get(kind, "") + message, name=f"status-{kind}")
    lab.setProperty("statusKind", kind)
    return lab


def bullet_list(items: Iterable[str], empty: str = "None.") -> QLabel:
    lines = [f"• {i}" for i in items if i]
    return plain_label("\n".join(lines) if lines else empty, muted=not lines)


def kv_form(rows: Iterable[tuple[str, str]]) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setContentsMargins(0, 0, 0, 0)
    for k, v in rows:
        key = plain_label(k, selectable=False, bold=True)
        val = plain_label(v)
        key.setBuddy(val)
        form.addRow(key, val)
    return w


class Card(QFrame):
    """A framed section whose title is a wrapping plain-text label.

    (A QGroupBox title never wraps, so long API-provided titles would force a wide window.)"""

    def __init__(self, title: str):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title = title
        self.setAccessibleName(title[:200])
        self.body = QVBoxLayout(self)
        self.title_label = plain_label(title, bold=True, name="card-title")
        self.body.addWidget(self.title_label)

    def title(self) -> str:
        return self._title


def card(title: str) -> tuple[Card, QVBoxLayout]:
    box = Card(title)
    return box, box.body


def model_text(text: str, name: str = "model-text") -> QPlainTextEdit:
    """Read-only plain-text box for untrusted model output."""
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setPlainText(text)
    box.setObjectName(name)
    box.setAccessibleName("Model output (unverified)")
    box.setTabChangesFocus(True)
    box.setMinimumHeight(80)
    box.setMaximumHeight(320)
    return box


def collapsible(title: str, content: QWidget) -> QWidget:
    """A keyboard-accessible disclosure button that shows/hides `content`."""
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    btn = QToolButton()
    btn.setText(title.replace("&", "&&"))
    btn.setCheckable(True)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    btn.setArrowType(Qt.ArrowType.RightArrow)
    btn.setAccessibleName(title[:200])
    content.setVisible(False)

    def toggle(on: bool) -> None:
        content.setVisible(on)
        btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
    btn.toggled.connect(toggle)
    lay.addWidget(btn)
    lay.addWidget(content)
    return wrap


def clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class ResultArea(QWidget):
    """A vertical container whose content is replaced as a whole (like setChildren in the extension)."""

    def __init__(self, name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        if name:
            self.setObjectName(name)

    def set(self, *widgets: QWidget | None) -> None:
        clear_layout(self.box)
        for w in widgets:
            if w is not None:
                self.box.addWidget(w)

    def add(self, w: QWidget) -> None:
        self.box.addWidget(w)

    def texts(self) -> str:
        """All visible label/plain-text content, for tests and accessibility checks."""
        out = []
        for lab in self.findChildren(QLabel):
            out.append(lab.text())
        for edit in self.findChildren(QPlainTextEdit):
            out.append(edit.toPlainText())
        return "\n".join(out)


def scroll_page(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(inner)
    return sa


def confirm(parent: QWidget, title: str, text: str, detail: str = "", yes_label: str = "Confirm") -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    for lab in box.findChildren(QLabel):  # informative text has its own label: keep it plain too
        lab.setTextFormat(Qt.TextFormat.PlainText)
    yes = box.addButton(yes_label, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is yes
