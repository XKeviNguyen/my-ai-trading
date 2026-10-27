"""Freshness, multi-timeframe summary and methodology perspectives (GET /api/v1/analysis).

Perspectives are shown one by one: no BUY/SELL badge, no vote or aggregate across frameworks."""
from __future__ import annotations

import time
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import formatting as F
from ..context import AppContext
from ..widgets import (
    ResultArea, bullet_list, card, collapsible, heading, kv_form, plain_label, scroll_page, status_label,
)


def alignment_widget(a: Any) -> QWidget:
    return plain_label("\n".join(F.alignment_lines(a)), name="alignment")


def events_widget(events: Any) -> QWidget:
    lines = F.event_lines(events)
    return plain_label("\n".join(lines) if lines else "No events on the last closed bar.", muted=not lines)


class AnalysisView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self._seq = 0
        self.loaded_key = ""
        self.last_data: dict[str, Any] | None = None
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("&Refresh analysis")
        self.refresh_btn.setAccessibleName("Refresh analysis")
        self.refresh_btn.clicked.connect(self.load)
        bar.addWidget(self.refresh_btn)
        bar.addStretch(1)
        outer.addLayout(bar)
        self.result = ResultArea("analysis-result")
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(self.result)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)
        self.result.set(plain_label("Enter a symbol above and press Load.", muted=True))
        ctx.instrument_changed.connect(self.on_instrument)

    def key(self) -> str:
        return f"{self.ctx.symbol}|{self.ctx.market}|{self.ctx.analysis_tf}" if self.ctx.symbol else ""

    def on_instrument(self) -> None:
        if self.isVisible():
            self.load()
        else:
            self.loaded_key = ""

    def show_tab(self) -> None:
        if self.key() and self.key() != self.loaded_key:
            self.load()

    def load(self) -> None:
        inst = self.ctx.instrument()
        if not inst:
            self.result.set(status_label("error", "Enter a valid symbol above first."))
            return
        symbol, market = inst
        atf = self.ctx.analysis_tf
        self._seq += 1
        my = self._seq
        self.loaded_key = self.key()
        self.result.set(status_label("loading", f"Loading {symbol} analysis…"))

        def ok(data: dict[str, Any]) -> None:
            if my == self._seq:
                self.last_data = data
                self.render(data)

        def err(e: Exception) -> None:
            if my == self._seq:
                self.loaded_key = ""
                self.result.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.analysis(symbol, market, atf), ok, err)

    # ---- rendering ----
    def render(self, data: dict[str, Any]) -> None:
        now = time.time() * 1000
        snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        persp_block = data.get("perspectives") if isinstance(data.get("perspectives"), dict) else {}
        atf = F.text(persp_block.get("analysis_timeframe"), self.ctx.analysis_tf)
        widgets: list[QWidget] = [self._header(snap, atf, now), heading("Multi-timeframe summary"),
                                  alignment_widget(snap.get("alignment"))]
        tfs = snap.get("timeframes") if isinstance(snap.get("timeframes"), dict) else {}
        for tf, r in tfs.items():
            if isinstance(r, dict):
                widgets.append(self._timeframe_card(str(tf), r, now))
        persp = [p for p in persp_block.get("perspectives") or [] if isinstance(p, dict)]
        widgets.append(heading(f"Methodology perspectives ({len(persp)})"))
        if not persp:
            empty = QWidget()
            el = QHBoxLayout(empty)
            el.setContentsMargins(0, 0, 0, 0)
            el.addWidget(plain_label("No methodology perspectives selected. That is a valid choice."), 1)
            b = QPushButton("Choose methodologies")
            b.clicked.connect(lambda: self.ctx.select_tab("methods"))
            el.addWidget(b)
            widgets.append(empty)
        for p in persp:
            widgets.append(self._perspective_card(p, atf))
        if persp_block.get("aggregation"):
            widgets.append(plain_label(f"Aggregation: {F.text(persp_block.get('aggregation'))}", muted=True))
        self.result.set(*widgets)

    def _header(self, snap: dict[str, Any], atf: str, now: float) -> QWidget:
        inst = snap.get("instrument") if isinstance(snap.get("instrument"), dict) else {}
        reports = [r for r in (snap.get("timeframes") or {}).values() if isinstance(r, dict)]
        stale = sum(1 for r in reports if r.get("status") == "STALE_DATA")
        errors = sum(1 for r in reports if r.get("status") == "PROVIDER_ERROR")
        title = f"{F.text(inst.get('symbol'))} · {F.market_label(inst.get('market_type'))}"
        if stale:
            title += f" · STALE ×{stale}"
        if errors:
            title += f" · PROVIDER_ERROR ×{errors}"
        box, lay = card(title)
        box.setObjectName("instrument-header")
        age = F.fmt_age(snap.get("as_of_ms"), now)
        lay.addWidget(kv_form([
            ("Venue", F.text(inst.get("venue"))),
            ("As of", f"{F.text(snap.get('as_of_utc'))}" + (f" ({age})" if age else "")),
            ("Analysis timeframe", atf),
            ("Source", F.text(snap.get("source"))),
        ]))
        return box

    def _timeframe_card(self, tf: str, r: dict[str, Any], now: float) -> QWidget:
        trend = r.get("trend") if isinstance(r.get("trend"), dict) else {}
        title = f"{tf} · {F.text(r.get('status'))}" + (f" · trend {F.text(trend.get('state'))}" if trend.get("state") else "")
        box, lay = card(title)
        box.setObjectName(f"tf-card-{tf}")
        if r.get("status") == "PROVIDER_ERROR":
            lay.addWidget(status_label("error", F.text(r.get("error"), "Provider error for this timeframe.")))
            return box
        lay.addWidget(kv_form(F.timeframe_rows(r, now)))
        if trend.get("reason") or trend.get("definition"):
            lay.addWidget(plain_label(F.text(trend.get("reason") or trend.get("definition")), muted=True))
        lay.addWidget(plain_label("Levels: " + F.levels_text(r.get("levels"))))
        lay.addWidget(plain_label("Events", bold=True, selectable=False))
        lay.addWidget(events_widget(r.get("events")))
        missing = F.str_list(r.get("missing_inputs"))
        if missing:
            lay.addWidget(collapsible(f"Data notes ({len(missing)})", bullet_list(missing)))
        return box

    def _perspective_card(self, p: dict[str, Any], atf: str) -> QWidget:
        a = p.get("assessment") if isinstance(p.get("assessment"), dict) else {}
        ev = p.get("evaluation") if isinstance(p.get("evaluation"), dict) else {}
        box, lay = card(f"{F.text(a.get('name'))} · {F.text(a.get('status'))}")
        box.setObjectName("perspective-card")
        lay.addWidget(plain_label(F.text(a.get("label")), bold=True, name="mode-label"))
        lay.addWidget(kv_form([
            ("Requested mode", F.text(a.get("requested_mode"))),
            ("Evaluation timeframe", F.text(a.get("evaluation_timeframe"), "not evaluated")),
            ("Analysis timeframe", F.text(a.get("analysis_timeframe"), atf)),
            ("Evaluation state", F.text(ev.get("state"))),
            ("Evidence / maturity", f"{F.text(p.get('evidence_quality'))} / {F.text(p.get('implementation_maturity'))}"),
        ]))
        if ev.get("reason"):
            lay.addWidget(plain_label(F.text(ev.get("reason"))))
        if ev.get("bar_close_time_utc"):
            lay.addWidget(plain_label(f"Evaluated bar closed {F.text(ev.get('bar_close_time_utc'))}", muted=True))
        if ev.get("invalidation"):
            lay.addWidget(plain_label(f"Invalidation: {F.text(ev.get('invalidation'))}"))
        checks = ev.get("checks") if isinstance(ev.get("checks"), dict) else {}
        if checks:
            lay.addWidget(collapsible(f"Checks ({len(checks)})",
                                      bullet_list(f"{k}: {F.fmt_json(v)}" for k, v in checks.items())))
        unevaluated = F.str_list(ev.get("unevaluated_criteria"))
        if unevaluated:
            lay.addWidget(collapsible("Unevaluated criteria", bullet_list(unevaluated)))
        tl = p.get("tactical_layer")
        if isinstance(tl, dict):
            tbox, tlay = card(F.text(tl.get("label"), "Custom tactical layer"))
            trend = tl.get("trend") if isinstance(tl.get("trend"), dict) else {}
            tlay.addWidget(plain_label(f"{F.text(tl.get('timeframe'), '')} · {F.text(tl.get('status'), '')} · "
                                       f"trend {F.text(trend.get('state'))}"))
            tlay.addWidget(events_widget(tl.get("events")))
            lay.addWidget(tbox)
        lay.addWidget(plain_label("Reasons", bold=True, selectable=False))
        lay.addWidget(bullet_list(F.str_list(a.get("reasons"))))
        if a.get("requires_user_approval"):
            lay.addWidget(plain_label("This adaptation requires a preview and your approval before it runs."))
        routes = [r for r in a.get("offered_routes") or []
                  if isinstance(r, dict) and r.get("mode") != a.get("requested_mode")]
        if routes:
            lay.addWidget(plain_label("Offered routes", bold=True, selectable=False))
            msg = QWidget()
            msg_lay = QVBoxLayout(msg)
            msg_lay.setContentsMargins(0, 0, 0, 0)
            for r in routes:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                mode = F.text(r.get("mode"))
                rl.addWidget(plain_label(f"{mode}: {F.text(r.get('description'))}"), 1)
                if mode == "CUSTOM":
                    b = QPushButton("Open Strategy Studio")
                    b.clicked.connect(lambda: self.ctx.select_tab("studio"))
                    rl.addWidget(b)
                elif mode in F.MODES:
                    b = QPushButton(f"Use {mode} mode")
                    b.setAccessibleName(f"Use {mode} mode for {F.text(a.get('name'))}")
                    mid = a.get("methodology_id")
                    b.clicked.connect(lambda _=False, m=mode, i=mid, w=msg_lay: self.switch_mode(i, m, w))
                    rl.addWidget(b)
                lay.addWidget(row)
            lay.addWidget(msg)
        if p.get("provenance"):
            inner = QWidget()
            il = QVBoxLayout(inner)
            il.setContentsMargins(0, 0, 0, 0)
            il.addWidget(plain_label(F.text(p.get("provenance"))))
            il.addWidget(bullet_list(F.str_list(p.get("limitations")), "No limitations listed."))
            lay.addWidget(collapsible("Provenance and limitations", inner))
        return box

    def switch_mode(self, mid: Any, mode: str, msg_layout: QVBoxLayout) -> None:
        if mode not in F.MODES or not isinstance(mid, str):
            return
        msg_layout.addWidget(status_label("loading", "Saving mode…"))

        def mutate(s: dict[str, Any]) -> None:
            for sel in s.get("selected_methodologies", []):
                if sel.get("methodology_id") == mid:
                    sel["mode"] = mode

        def err(e: Exception) -> None:
            msg_layout.addWidget(self.ctx.error_widget(e))
        self.ctx.update_settings_async(mutate, lambda _s: self.load(), err)
