"""Copilot questions, Challenge My Thesis and optional chart-screenshot analysis.

Deterministic facts are always shown separately from model interpretation (untrusted text)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import formatting as F
from ..client import MAX_IMAGE_BYTES, TIMEFRAMES
from ..context import AppContext
from ..widgets import (
    ResultArea, bullet_list, buddy_label, card, explained_check, collapsible, kv_form, model_text, plain_label, scroll_page, status_label,
)
from .analysis import alignment_widget


def model_card(title: str, m: dict[str, Any]) -> QWidget:
    box, lay = card(title)
    box.setObjectName("model-output")
    lay.addWidget(plain_label(F.text(m.get("label"), "Model output — unverified"), bold=True))
    lay.addWidget(model_text(F.text(m.get("text"), "")))
    lay.addWidget(plain_label(F.model_meta(m), muted=True))
    return box


def fallback_widget(route: Any, reason: Any) -> QWidget | None:
    t = F.fallback_text(route, reason)
    return status_label("info", t) if t else None


def render_ask(r: dict[str, Any]) -> list[QWidget | None]:
    f = r.get("facts") if isinstance(r.get("facts"), dict) else {}
    box, lay = card("Facts (deterministic)")
    box.setObjectName("facts")
    lay.addWidget(plain_label(F.text(f.get("label"), "Observed facts"), bold=True))
    if r.get("snapshot_as_of_utc"):
        lay.addWidget(plain_label(f"Snapshot as of {F.text(r.get('snapshot_as_of_utc'))}", muted=True))
    rows = []
    for t in f.get("timeframes") or []:
        if not isinstance(t, dict):
            continue
        status = t.get("status")
        if t.get("error") or (status and status not in ("OK", "INSUFFICIENT_DATA", "STALE_DATA")):
            rows.append(f"{F.text(t.get('timeframe'))} · {F.text(status, '')} {F.text(t.get('error'), '')}")
            continue
        parts = [F.text(status, ""), f"bar closed {F.text(t.get('bar_close_time_utc'))}",
                 f"close {F.fmt_num(t.get('close'))}", f"trend {F.text(t.get('trend'))}",
                 f"SMA50 {F.fmt_num(t.get('sma50'))}", f"SMA200 {F.fmt_num(t.get('sma200'))}",
                 f"RSI14 {F.fmt_num(t.get('rsi14'), 4)}", f"vol ratio {F.fmt_num(t.get('volume_ratio_20'), 4)}"]
        line = f"{F.text(t.get('timeframe'))} · " + " · ".join(parts)
        ev = F.str_list(t.get("events"))
        if ev:
            line += f"\n    Events: {', '.join(ev)}"
        rows.append(line)
    lay.addWidget(bullet_list(rows, "No timeframe facts."))
    lay.addWidget(alignment_widget(f.get("alignment")))
    persp = [p for p in f.get("perspectives") or [] if isinstance(p, dict)]
    if persp:
        lay.addWidget(plain_label("Perspectives (each on its own)", bold=True, selectable=False))
        lay.addWidget(bullet_list(
            f"{F.text(p.get('methodology'), '')}: {F.text(p.get('status'), '')} — {F.text(p.get('label'), '')} — "
            f"eval {F.text(p.get('evaluation_timeframe'), 'none')} — {F.text(p.get('state'))}" for p in persp))
    interp = r.get("interpretation")
    second: QWidget = model_card("Interpretation", interp) if isinstance(interp, dict) else \
        plain_label(f"Route: {F.text(r.get('route'), 'deterministic')}", muted=True)
    return [box, second, fallback_widget(r.get("route"), r.get("fallback_reason"))]


def render_challenge(r: dict[str, Any]) -> list[QWidget | None]:
    a = r.get("audit") if isinstance(r.get("audit"), dict) else {}
    head, hl = card("Assessment")
    head.setObjectName("assessment")
    hl.addWidget(plain_label(f"{F.text(a.get('assessment'))} · direction: {F.text(a.get('direction'), 'unknown')} · "
                             f"focus {F.text(a.get('focus_timeframe'))}", bold=True))
    if a.get("assessment_note"):
        hl.addWidget(plain_label(F.text(a.get("assessment_note"))))
    if a.get("method"):
        hl.addWidget(plain_label(F.text(a.get("method")), muted=True))
    out: list[QWidget | None] = [head]
    for title, key in (("Supporting evidence", "supporting_evidence"),
                       ("Contradicting evidence", "contradicting_evidence"),
                       ("Assumptions", "assumptions"), ("Unknowns", "unknowns"),
                       ("Would falsify the thesis", "falsification_conditions"),
                       ("Proposed tests", "proposed_tests")):
        box, lay = card(title)
        box.setObjectName(key)
        lay.addWidget(bullet_list(F.str_list(a.get(key))))
        out.append(box)
    obs = F.str_list(a.get("observations"))
    out.append(collapsible(f"Observations ({len(obs)})", bullet_list(obs)))
    if isinstance(r.get("debate"), dict):
        out.append(model_card("Deep debate", r["debate"]))
    out.append(fallback_widget(r.get("route"), r.get("fallback_reason")))
    return out


def render_local_vision(l: Any) -> QWidget | None:
    if not isinstance(l, dict):
        return None
    box, lay = card("Local image analysis (0 tokens)")
    box.setObjectName("local-analysis")
    if l.get("label"):
        lay.addWidget(plain_label(F.text(l.get("label")), muted=True))
    if l.get("status") != "OK":
        lay.addWidget(plain_label(f"{F.text(l.get('status'), 'UNAVAILABLE')}: {F.text(l.get('reason'), '')}"))
        if l.get("privacy"):
            lay.addWidget(plain_label(F.text(l.get("privacy")), muted=True))
        return box
    g = l.get("geometry") if isinstance(l.get("geometry"), dict) else {}
    o = l.get("ocr") if isinstance(l.get("ocr"), dict) else {}
    p = o.get("parsed") if isinstance(o.get("parsed"), dict) else {}
    if g.get("state") == "DETECTED":
        geometry = (f"{F.fmt_int(g.get('candles_detected'))} candle shapes ({F.fmt_int(g.get('green'))} green / "
                    f"{F.fmt_int(g.get('red'))} red), visual drift {F.text(g.get('visual_drift'))}, "
                    f"last visible candle {F.text(g.get('last_visible_candle_color'))}")
    else:
        geometry = F.text(g.get("note") or g.get("state"), "not detected")
    if o.get("status") == "OK":
        ocr = [f"Symbols read: {', '.join(F.str_list(p.get('symbols'))) or 'none'}",
               f"Timeframes read: {', '.join(F.str_list(p.get('timeframes'))) or 'none'}",
               f"Indicator labels: {', '.join(F.str_list(p.get('indicator_labels'))) or 'none'}",
               "Price-axis labels: " + (", ".join(F.fmt_num(x) for x in p.get("price_axis_labels") or []) or "none"),
               f"Mean OCR confidence: {F.fmt_num(o.get('mean_confidence'))} "
               f"({F.text(o.get('confidence_note'), 'uncalibrated')})"]
    else:
        ocr = [f"OCR {F.text(o.get('status'), 'unavailable')}: {F.text(o.get('reason'), '')}"]
    lay.addWidget(kv_form([("Geometry", geometry)]))
    lay.addWidget(bullet_list(ocr))
    lay.addWidget(plain_label("Checks against exchange data", bold=True, selectable=False))
    checks = []
    for c in l.get("cross_checks") or []:
        if isinstance(c, dict):
            line = f"[{F.text(c.get('result'), '?')}] {F.text(c.get('check'), '')}"
            if c.get("reason"):
                line += f" — {F.text(c.get('reason'))}"
            if c.get("note"):
                line += f" — {F.text(c.get('note'))}"
            checks.append(line)
    lay.addWidget(bullet_list(checks, "No cross-checks."))
    amb = F.str_list(l.get("ambiguities"))
    if amb:
        lay.addWidget(plain_label("Ambiguities", bold=True, selectable=False))
        lay.addWidget(bullet_list(amb))
    if l.get("privacy"):
        lay.addWidget(plain_label(F.text(l.get("privacy")), muted=True))
    return box


def render_vision(r: dict[str, Any]) -> list[QWidget | None]:
    hyp = r.get("visual_hypotheses")
    return [render_local_vision(r.get("local_analysis")),
            model_card("Visual hypotheses", hyp) if isinstance(hyp, dict) else None,
            fallback_widget(r.get("route"), r.get("fallback_reason"))]


class CopilotView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self.inst_line = plain_label("", muted=True)
        lay.addWidget(self.inst_line)
        lay.addWidget(self._ask_section())
        lay.addWidget(self._challenge_section())
        lay.addWidget(self._vision_section())
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll_page(inner))
        ctx.instrument_changed.connect(self.on_instrument)
        self.on_instrument()

    def on_instrument(self) -> None:
        inst = self.ctx.instrument()
        self.inst_line.setText(f"Questions use {inst[0]} ({F.market_label(inst[1])}) and your saved timeframes."
                               if inst else "Enter a symbol above first.")

    @staticmethod
    def _text_box(placeholder: str, name: str, rows: int = 3) -> QPlainTextEdit:
        t = QPlainTextEdit()
        t.setPlaceholderText(placeholder)
        t.setAccessibleName(name)
        t.setTabChangesFocus(True)
        t.setFixedHeight(t.fontMetrics().lineSpacing() * rows + 16)
        return t

    def _ask_section(self) -> QWidget:
        box, lay = card("Copilot")
        form = QFormLayout()
        self.question = self._text_box("e.g. Is 4h momentum fading against the 1d trend?", "Question")
        q_label = buddy_label("&Question", self.question)
        form.addRow(q_label, self.question)
        lay.addLayout(form)
        self.ask_deep = explained_check(lay, "Deep analysis", "Higher effort, more tokens; needs the opt-in in Settings.")
        self.ask_btn = QPushButton("&Ask")
        self.ask_btn.clicked.connect(self.ask)
        lay.addWidget(self.ask_btn)
        self.ask_out = ResultArea("ask-out")
        lay.addWidget(self.ask_out)
        return box

    def ask(self) -> None:
        inst = self.ctx.instrument()
        question = self.question.toPlainText().strip()
        if not inst:
            return self.ask_out.set(status_label("error", "Enter a symbol above first."))
        if not question:
            return self.ask_out.set(status_label("error", "Type a question."))
        if len(question) > 4000:
            return self.ask_out.set(status_label("error", "The question is longer than 4000 characters."))
        deep = self.ask_deep.isChecked()
        self.ask_btn.setEnabled(False)
        self.ask_out.set(status_label("loading", "Asking…"))

        def ok(r: dict[str, Any]) -> None:
            self.ask_btn.setEnabled(True)
            self.ask_out.set(*render_ask(r))

        def err(e: Exception) -> None:
            self.ask_btn.setEnabled(True)
            self.ask_out.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.ask(inst[0], inst[1], question, deep), ok, err)

    def _challenge_section(self) -> QWidget:
        box, lay = card("Challenge My Thesis")
        form = QFormLayout()
        self.thesis = self._text_box("e.g. Bullish: 4h breakout will continue above the 1d range.", "Thesis")
        t_label = buddy_label("&Thesis", self.thesis)
        form.addRow(t_label, self.thesis)
        form.addRow(plain_label("State the direction (bullish/bearish) and what you expect.", muted=True))
        self.focus = QComboBox()
        self.focus.addItem("Auto (smallest)", "")
        for tf in TIMEFRAMES:
            self.focus.addItem(tf, tf)
        self.focus.setAccessibleName("Focus timeframe")
        f_label = buddy_label("&Focus timeframe", self.focus)
        form.addRow(f_label, self.focus)
        lay.addLayout(form)
        self.ch_deep = explained_check(lay, "Add deep bull/bear debate", "Uses tokens; needs the opt-in in Settings.")
        self.ch_btn = QPushButton("&Challenge my thesis")
        self.ch_btn.clicked.connect(self.challenge)
        lay.addWidget(self.ch_btn)
        self.ch_out = ResultArea("challenge-out")
        lay.addWidget(self.ch_out)
        return box

    def challenge(self) -> None:
        inst = self.ctx.instrument()
        thesis = self.thesis.toPlainText().strip()
        if not inst:
            return self.ch_out.set(status_label("error", "Enter a symbol above first."))
        if not thesis:
            return self.ch_out.set(status_label("error", "Write your thesis first."))
        if len(thesis) > 4000:
            return self.ch_out.set(status_label("error", "The thesis is longer than 4000 characters."))
        focus = self.focus.currentData() or None
        deep = self.ch_deep.isChecked()
        self.ch_btn.setEnabled(False)
        self.ch_out.set(status_label("loading", "Auditing evidence…"))

        def ok(r: dict[str, Any]) -> None:
            self.ch_btn.setEnabled(True)
            self.ch_out.set(*render_challenge(r))

        def err(e: Exception) -> None:
            self.ch_btn.setEnabled(True)
            self.ch_out.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.challenge(inst[0], inst[1], thesis, focus, deep), ok, err)

    def _vision_section(self) -> QWidget:
        box, lay = card("Chart screenshot (optional)")
        lay.addWidget(plain_label("Sends the image to the AI provider via the local engine only if AI consent is on. "
                                  "Visual output is a hypothesis, not exact OHLCV. You choose the file; this app "
                                  "never captures your screen.", muted=True))
        row = QHBoxLayout()
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("PNG/JPEG/WEBP, up to 5 MB")
        self.image_path.setAccessibleName("Screenshot file path")
        row.addWidget(self.image_path, 1)
        browse = QPushButton("&Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        lay.addLayout(row)
        self.vision_q = self._text_box("Optional question about the screenshot", "Screenshot question", rows=2)
        lay.addWidget(self.vision_q)
        self.with_symbol = explained_check(lay, "Compare with exchange data",
                                           "Cross-checks what the image shows against the current instrument.")
        self.with_symbol.setChecked(True)
        self.vision_btn = QPushButton("Analyze &screenshot")
        self.vision_btn.clicked.connect(self.analyze)
        lay.addWidget(self.vision_btn)
        self.vision_out = ResultArea("vision-out")
        lay.addWidget(self.vision_out)
        return box

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a chart screenshot", "",
                                              "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.image_path.setText(path)

    def analyze(self) -> None:
        raw = self.image_path.text().strip()
        if not raw:
            return self.vision_out.set(status_label("error", "Choose an image file."))
        path = Path(raw)
        try:
            size = path.stat().st_size if path.is_file() else -1
        except OSError:
            size = -1
        if size < 0:
            return self.vision_out.set(status_label("error", "That file does not exist or cannot be read."))
        ctype, problem = F.check_image(path.name, size)
        if problem or ctype is None:
            return self.vision_out.set(status_label("error", problem or "Choose an image file."))
        question = self.vision_q.toPlainText().strip()
        inst = self.ctx.instrument() if self.with_symbol.isChecked() else None
        self.vision_btn.setEnabled(False)
        self.vision_out.set(status_label("loading", "Uploading to the local engine…"))

        def job() -> dict[str, Any]:
            with open(path, "rb") as fh:
                data = fh.read(MAX_IMAGE_BYTES + 1)
            return self.ctx.client.vision(data, path.name, ctype, question,
                                          symbol=inst[0] if inst else None,
                                          market=inst[1] if inst else "usdm_perpetual")

        def ok(r: dict[str, Any]) -> None:
            self.vision_btn.setEnabled(True)
            self.vision_out.set(*render_vision(r))

        def err(e: Exception) -> None:
            self.vision_btn.setEnabled(True)
            self.vision_out.set(self.ctx.error_widget(e))
        self.ctx.run(job, ok, err)
