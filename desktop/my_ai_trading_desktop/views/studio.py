"""Strategy Studio: compile prompts → preview → dry-run / save draft → explicit rule_hash approval."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import formatting as F
from ..client import TIMEFRAMES
from ..context import AppContext
from ..widgets import (
    ResultArea, bullet_list, buddy_label, card, explained_check, collapsible, heading, kv_form, plain_label, scroll_page, status_label,
)


def dry_run_widget(r: dict[str, Any]) -> QWidget:
    c = r.get("current") if isinstance(r.get("current"), dict) else {}
    replay = r.get("replay") if isinstance(r.get("replay"), dict) else {}
    box, lay = card("Dry-run")
    box.setObjectName("dry-run")
    lay.addWidget(plain_label(f"Current: {F.text(c.get('result'))} · action {F.text(c.get('action'))} · "
                              f"as of {F.text(c.get('as_of_utc'))}", bold=True))
    bct = c.get("bar_close_times") if isinstance(c.get("bar_close_times"), dict) else {}
    lay.addWidget(plain_label("Bar close times: " + (" · ".join(f"{k} {F.text(v)}" for k, v in bct.items()) or F.DASH)))
    leaves = [x for x in c.get("leaves") or [] if isinstance(x, dict)]
    if leaves:
        lines = []
        for x in leaves:
            line = f"{F.text(x.get('condition'), '')} → {F.fmt_json(x.get('result'))}"
            if "left" in x:
                line += f" (left {F.fmt_json(x.get('left'))}, right {F.fmt_json(x.get('right'))})"
            lines.append(line)
        lay.addWidget(collapsible(f"Conditions ({len(leaves)})", bullet_list(lines)))
    if c.get("note"):
        lay.addWidget(plain_label(F.text(c.get("note")), muted=True))
    counts = replay.get("counts") if isinstance(replay.get("counts"), dict) else {}
    lay.addWidget(plain_label(f"Replay over {F.fmt_int(replay.get('bars') or 0)} closed bars", bold=True))
    lay.addWidget(plain_label(" · ".join(f"{k}: {F.fmt_json(v)}" for k, v in counts.items()) or F.DASH))
    matches = [m for m in replay.get("recent_matches") or [] if isinstance(m, str)]
    lay.addWidget(collapsible(f"Recent matches ({len(matches)})",
                              bullet_list(matches, "No matches in the replay window.")))
    if replay.get("note"):
        lay.addWidget(plain_label(F.text(replay.get("note")), muted=True))
    return box


class StudioView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.loaded = False
        self.last_proposal: dict[str, Any] | None = None
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.addWidget(self._composer())
        head = QHBoxLayout()
        head.addWidget(heading("My strategies"), 1)
        reload_btn = QPushButton("Reload strategies")
        reload_btn.clicked.connect(self.load_list)
        head.addWidget(reload_btn)
        lay.addLayout(head)
        self.list_box = ResultArea("strategy-list")
        lay.addWidget(self.list_box)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll_page(inner))

    def show_tab(self) -> None:
        if not self.loaded:
            self.load_list()

    # ---- compose ----
    def _composer(self) -> QWidget:
        box, lay = card("Strategy Studio")
        form = QFormLayout()
        self.name = QLineEdit("Custom strategy")
        self.name.setMaxLength(80)
        form.addRow(buddy_label("Strategy &name", self.name), self.name)
        form.addRow(plain_label("Letters, digits, spaces and . , : ( ) + / - only.", muted=True))
        self.prompts = QPlainTextEdit()
        self.prompts.setTabChangesFocus(True)
        self.prompts.setPlaceholderText("One prompt per line, e.g.\nalert me when the 1h close is above SMA50 and RSI "
                                        "is below 70\nalso require the 1d trend is up")
        self.prompts.setFixedHeight(self.prompts.fontMetrics().lineSpacing() * 4 + 16)
        form.addRow(buddy_label("&Prompts", self.prompts), self.prompts)
        form.addRow(plain_label("Up to 10 lines. Rules are data only; nothing runs until you approve.", muted=True))
        self.default_tf = QComboBox()
        self.default_tf.addItem("None (prompts must state it)", "")
        for tf in TIMEFRAMES:
            self.default_tf.addItem(tf, tf)
        form.addRow(buddy_label("&Default timeframe for prompts without one", self.default_tf), self.default_tf)
        lay.addLayout(form)
        self.use_llm = explained_check(lay, "If unresolved, ask the AI",
                                       "The AI proposes a rule for your review; uses tokens and needs AI consent.")
        self.compile_btn = QPushButton("&Compile preview")
        self.compile_btn.clicked.connect(self.compile)
        lay.addWidget(self.compile_btn)
        self.compile_out = ResultArea("compile-out")
        lay.addWidget(self.compile_out)
        return box

    def compile(self) -> None:
        prompts = F.split_prompts(self.prompts.toPlainText())
        name = self.name.text().strip() or "Custom strategy"
        problem = F.validate_compile_input(prompts, name)
        if problem:
            return self.compile_out.set(status_label("error", problem))
        tf = self.default_tf.currentData() or None
        use_llm = self.use_llm.isChecked()
        self.compile_btn.setEnabled(False)
        self.compile_out.set(status_label("loading", "Compiling…"))

        def ok(r: dict[str, Any]) -> None:
            self.compile_btn.setEnabled(True)
            self.last_proposal = r
            self.compile_out.set(self.proposal(r, prompts, "Proposal"))

        def err(e: Exception) -> None:
            self.compile_btn.setEnabled(True)
            self.compile_out.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.compile(prompts, name, tf, use_llm), ok, err)

    def proposal(self, r: dict[str, Any], prompts: list[str], title: str) -> QWidget:
        box, lay = card(f"{title} · {F.text(r.get('status'))}")
        box.setObjectName("proposal")
        if r.get("model"):
            lay.addWidget(plain_label(f"Proposed by model {F.text(r.get('model'))} — review every condition", bold=True))
        if r.get("reason"):
            lay.addWidget(plain_label(F.text(r.get("reason"))))
        if isinstance(r.get("preview"), dict):
            lay.addWidget(kv_form(F.preview_rows(r["preview"])))
        clauses = [c for c in r.get("clauses") or [] if isinstance(c, dict)]
        if clauses:
            lay.addWidget(collapsible("Recognized clauses", bullet_list(
                f"{F.text(c.get('kind'), '')}: {F.text(c.get('text'), '')}" for c in clauses)))
        lay.addWidget(plain_label("Assumptions", bold=True, selectable=False))
        lay.addWidget(bullet_list(F.str_list(r.get("assumptions"))))
        for label, key in (("Unresolved", "unresolved"), ("Rejected", "rejected_reasons")):
            items = F.str_list(r.get(key))
            if items:
                lay.addWidget(plain_label(label, bold=True, selectable=False))
                lay.addWidget(bullet_list(items))
        if r.get("activation"):
            lay.addWidget(plain_label(f"Activation: {F.text(r.get('activation'))}", muted=True))
        out = ResultArea("proposal-out")
        rule = r.get("rule")
        if isinstance(rule, dict):
            row = QHBoxLayout()
            dry = QPushButton("Dry-run on current symbol")
            dry.clicked.connect(lambda: self.dry_run(rule, out))
            save = QPushButton("Save draft")
            save.clicked.connect(lambda: self.save_draft(rule, prompts, out, save))
            row.addWidget(dry)
            row.addWidget(save)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addWidget(out)
        llm = r.get("llm_proposal")
        if isinstance(llm, dict):
            lay.addWidget(self.proposal(llm, prompts, "AI proposal (validated)"))
        return box

    def dry_run(self, rule: dict[str, Any], out: ResultArea) -> None:
        inst = self.ctx.instrument()
        if not inst:
            return out.set(status_label("error", "Enter a symbol above first."))
        out.set(status_label("loading", f"Dry-running on {inst[0]}…"))
        self.ctx.run(lambda: self.ctx.client.dry_run(rule, inst[0], inst[1]),
                     lambda r: out.set(dry_run_widget(r)), lambda e: out.set(self.ctx.error_widget(e)))

    def save_draft(self, rule: dict[str, Any], prompts: list[str], out: ResultArea, btn: QPushButton) -> None:
        btn.setEnabled(False)
        out.set(status_label("loading", "Saving draft…"))

        def ok(r: dict[str, Any]) -> None:
            preview = r.get("preview") if isinstance(r.get("preview"), dict) else {}
            approve = None
            sid, version, rule_hash = r.get("strategy_id"), r.get("version"), preview.get("rule_hash")
            if isinstance(sid, str) and isinstance(version, int) and isinstance(rule_hash, str):
                approve = self.approve_button(sid, version, rule_hash, F.text(preview.get("name"), "strategy"), preview, out)
            out.set(status_label("ok", f"Saved strategy {F.text(sid)} v{F.text(version)} as {F.text(r.get('status'))}. "
                                       f"{F.text(r.get('next'), '')}"), approve)
            self.load_list()

        def err(e: Exception) -> None:
            btn.setEnabled(True)
            out.set(self.ctx.error_widget(e))
        self.ctx.run(lambda: self.ctx.client.create_strategy(rule, prompts), ok, err)

    # ---- approval ----
    def approve_button(self, sid: str, version: int, rule_hash: str, name: str, preview: dict[str, Any],
                       out: ResultArea) -> QPushButton:
        b = QPushButton(f"Approve v{version}…")
        b.setAccessibleName(f"Approve {name} version {version}")
        b.clicked.connect(lambda: self.approve(sid, version, rule_hash, name, preview, out))
        return b

    def approve(self, sid: str, version: int, rule_hash: str, name: str, preview: dict[str, Any],
                out: ResultArea) -> None:
        detail = "\n".join(f"{k}: {v}" for k, v in F.preview_rows(preview) if k != "rule_hash")
        accepted = self.ctx.confirm(
            self, "Approve strategy",
            f'Activate monitoring for "{name}" v{version}?\n\nOnly confirm if this rule_hash matches the preview '
            f"you reviewed:\n\nrule_hash {rule_hash}",
            detail, f"Approve rule_hash {rule_hash}")
        if not accepted:
            out.add(status_label("info", "Approval cancelled."))
            return

        def ok(_r: dict[str, Any]) -> None:
            out.set(status_label("ok", f"{name} v{version} is ACTIVE; the local monitor evaluates it on closed bars."))
            self.load_list()
        self.ctx.run(lambda: self.ctx.client.approve(sid, version, rule_hash), ok,
                     lambda e: out.add(self.ctx.error_widget(e)))

    # ---- list ----
    def load_list(self) -> None:
        self.list_box.set(status_label("loading", "Loading strategies…"))

        def ok(r: dict[str, Any]) -> None:
            self.loaded = True
            strategies = [s for s in r.get("strategies") or [] if isinstance(s, dict)]
            if not strategies:
                return self.list_box.set(plain_label("No saved strategies yet.", muted=True))
            self.list_box.set(*(self.strategy_card(s) for s in strategies))
        self.ctx.run(self.ctx.client.strategies, ok, lambda e: self.list_box.set(self.ctx.error_widget(e)))

    def strategy_card(self, s: dict[str, Any]) -> QWidget:
        versions = [v for v in s.get("versions") or [] if isinstance(v, dict)]
        active = any(v.get("status") == "ACTIVE" for v in versions)
        name = F.text(s.get("name"), "strategy")
        box, lay = card(f"{name} · {'ACTIVE' if active else 'INACTIVE'}")
        box.setObjectName("strategy-card")
        lay.addWidget(plain_label(F.text(s.get("label")), bold=True))
        parent = f" · parent {F.text(s.get('parent_methodology'))}" if s.get("parent_methodology") else ""
        lay.addWidget(plain_label(f"id {F.text(s.get('id'))} · {F.text(s.get('kind'))}{parent}", muted=True))
        msg = ResultArea()
        sid = s.get("id")
        for v in versions:
            rule = v.get("rule") if isinstance(v.get("rule"), dict) else {}
            vbox, vl = card(f"v{F.text(v.get('version'))} · {F.text(v.get('status'))} · {F.text(v.get('rule_hash'))}")
            vl.addWidget(plain_label(f"trigger {F.fmt_json(rule.get('trigger_timeframe'))} · "
                                     f"{F.fmt_json(rule.get('description') or '', 160)}"))
            if v.get("approved_at"):
                vl.addWidget(plain_label(f"approved {F.fmt_ms(v.get('approved_at'))}", muted=True))
            preview = v.get("preview") if isinstance(v.get("preview"), dict) else {}
            if preview:
                vl.addWidget(collapsible("Preview", kv_form(F.preview_rows(preview))))
            if (v.get("status") == "DRAFT" and isinstance(sid, str) and isinstance(v.get("version"), int)
                    and isinstance(v.get("rule_hash"), str)):
                vl.addWidget(self.approve_button(sid, v["version"], v["rule_hash"], name, preview, msg))
            lay.addWidget(vbox)
        if active and isinstance(sid, str):
            d = QPushButton("Deactivate")
            d.setAccessibleName(f"Deactivate {name}")

            def deactivate() -> None:
                d.setEnabled(False)

                def err(e: Exception) -> None:
                    d.setEnabled(True)
                    msg.set(self.ctx.error_widget(e))
                self.ctx.run(lambda: self.ctx.client.deactivate(sid), lambda _r: self.load_list(), err)
            d.clicked.connect(deactivate)
            lay.addWidget(d)
        lay.addWidget(msg)
        return box
