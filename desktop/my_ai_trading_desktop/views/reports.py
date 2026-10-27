"""Saved analysis reports: save the current analysis, list reports, export Markdown to a file you choose."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import formatting as F
from ..context import AppContext
from ..widgets import ResultArea, card, heading, plain_label, scroll_page, status_label

MAX_MARKDOWN_CHARS = 4 * 1024 * 1024


def write_text_file(path: Path, content: str) -> None:
    """Write UTF-8 text with owner-only permissions on a new file (reports may describe private research)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class ReportsView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        # Replaceable in tests so no file dialog blocks the event loop.
        self.choose_save_path = self._dialog_save_path
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.save_btn = QPushButton("&Save current analysis as report")
        self.save_btn.clicked.connect(self.save_current)
        reload_btn = QPushButton("Reload reports")
        reload_btn.clicked.connect(self.load)
        bar.addWidget(self.save_btn)
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        outer.addLayout(bar)
        outer.addWidget(plain_label("A report stores the engine's analysis for the instrument and analysis timeframe "
                                    "selected above, as of the moment you save it. Export writes Markdown to a file "
                                    "you choose.", muted=True))
        self.msg = ResultArea("reports-msg")
        outer.addWidget(self.msg)
        self.list_box = ResultArea("reports-list")
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(heading("Saved reports"))
        il.addWidget(self.list_box)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)

    def show_tab(self) -> None:
        self.load()

    def save_current(self) -> None:
        inst = self.ctx.instrument()
        if not inst:
            return self.msg.set(status_label("error", "Enter a symbol above first."))
        atf = self.ctx.analysis_tf
        self.save_btn.setEnabled(False)
        self.msg.set(status_label("loading", f"Saving a report for {inst[0]} ({atf})…"))

        def ok(r: dict[str, Any]) -> None:
            self.save_btn.setEnabled(True)
            self.msg.set(status_label("ok", f"Saved report {F.text(r.get('id'))}: {F.text(r.get('title'))}"))
            self.load()

        def err(e: Exception) -> None:
            self.save_btn.setEnabled(True)
            self.msg.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.save_report(inst[0], inst[1], atf), ok, err)

    def load(self) -> None:
        self.list_box.set(status_label("loading", "Loading reports…"))

        def ok(r: dict[str, Any]) -> None:
            reports = [x for x in r.get("reports") or [] if isinstance(x, dict)]
            if not reports:
                return self.list_box.set(plain_label("No saved reports yet.", muted=True))
            self.list_box.set(*(self._item(x) for x in reports))
        self.ctx.run(self.ctx.client.reports, ok, lambda e: self.list_box.set(self.ctx.error_widget(e)))

    def _item(self, x: dict[str, Any]) -> QWidget:
        rid = F.text(x.get("id"), "")
        box, lay = card(F.text(x.get("title"), rid))
        box.setObjectName("report-item")
        lay.addWidget(plain_label(f"id {rid} · {F.text(x.get('instrument_key'))} · saved {F.fmt_ms(x.get('created_at'))}",
                                  muted=True))
        b = QPushButton("Export Markdown…")
        b.setAccessibleName(f"Export report {rid} as Markdown")
        b.clicked.connect(lambda: self.export(rid))
        lay.addWidget(b)
        return box

    def _dialog_save_path(self, suggested: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "Export report as Markdown",
                                              str(Path.home() / suggested), "Markdown (*.md)")
        return path

    def export(self, rid: str) -> None:
        path = self.choose_save_path(F.report_filename(rid))
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".md":
            target = target.with_suffix(target.suffix + ".md") if target.suffix else target.with_suffix(".md")
        self.msg.set(status_label("loading", f"Exporting report {rid}…"))

        def job() -> str:
            md = self.ctx.client.report_markdown(rid)
            if len(md) > MAX_MARKDOWN_CHARS:
                raise ValueError("report too large")
            return md

        def ok(md: str) -> None:
            try:
                write_text_file(target, md)
            except OSError as exc:
                self.msg.set(status_label("error", f"Could not write {target}: {exc.strerror or 'I/O error'}"))
                return
            self.msg.set(status_label("ok", f"Exported report {rid} to {target}"))
        self.ctx.run(job, ok, lambda e: self.msg.set(self.ctx.error_widget(e)))
