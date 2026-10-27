"""Methodology selection: all profiles, enable/disable any (including none) and choose a mode."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import formatting as F
from ..client import PreconditionFailedError
from ..context import AppContext
from ..widgets import ResultArea, bullet_list, buddy_label, card, collapsible, plain_label, scroll_page, status_label


class MethodsView(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.rows: dict[str, tuple[QCheckBox, QComboBox]] = {}
        self.profiles: list[dict[str, Any]] = []
        self.form_etag: str | None = None
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.save_btn = QPushButton("&Save selection")
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setEnabled(False)
        none = QPushButton("Disable all")
        none.clicked.connect(self.disable_all)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(lambda: self.load())
        for b in (self.save_btn, none, reload_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        outer.addLayout(bar)
        self.msg = ResultArea("methods-msg")
        outer.addWidget(self.msg)
        self.list_box = ResultArea("methods-list")
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(self.list_box)
        il.addStretch(1)
        outer.addWidget(scroll_page(inner), 1)

    def show_tab(self) -> None:
        self.load()

    def load(self, notice: QWidget | None = None) -> None:
        self.list_box.set(status_label("loading", "Loading methodologies…"))
        self.msg.set(notice if isinstance(notice, QWidget) else None)

        def job() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], str | None]]:
            profiles = self.ctx.profiles_cache or self.ctx.client.methodologies().get("profiles") or []
            return profiles, self.ctx.client.get_settings_versioned()

        def ok(result: tuple[list[dict[str, Any]], tuple[dict[str, Any], str | None]]) -> None:
            profiles, (settings, etag) = result
            self.ctx.profiles_cache = profiles
            self.ctx.settings_cache, self.ctx.settings_etag = settings, etag
            self.form_etag = etag  # the checkboxes show this version; a save sends it as If-Match
            self.render([p for p in profiles if isinstance(p, dict)], settings)
        self.ctx.run(job, ok, lambda e: self.list_box.set(self.ctx.error_widget(e)))

    def render(self, profiles: list[dict[str, Any]], settings: dict[str, Any]) -> None:
        self.profiles = profiles
        selected = {s.get("methodology_id"): s.get("mode") for s in settings.get("selected_methodologies") or []
                    if isinstance(s, dict)}
        self.rows.clear()
        intro = plain_label(f"{len(profiles)} research profiles. Each selected profile is reported on its own; there "
                            "is no vote across them. Selecting none is fine.")
        self.list_box.set(intro, *(self._row(p, selected.get(p.get("methodology_id"))) for p in profiles))
        self.save_btn.setEnabled(True)

    def _row(self, p: dict[str, Any], mode: Any) -> QWidget:
        mid = F.text(p.get("methodology_id"), "")
        name = F.text(p.get("name"), mid)
        box, lay = card(f"{name} · {F.text(p.get('implementation_maturity'))} · evidence: {F.text(p.get('evidence_quality'))}")
        box.setObjectName("method-card")
        enabled = QCheckBox(f"Enable {name}")
        enabled.setChecked(mode is not None)
        lay.addWidget(enabled)
        doc = p.get("documented_timeframes")
        tfs = "/".join(F.str_list(doc)) if isinstance(doc, list) else F.text(doc)
        needs = "price-only" if p.get("price_only") else f"needs {', '.join(F.str_list(p.get('required_data')))}"
        lay.addWidget(plain_label(f"{F.text(p.get('focus'))} · native {tfs} · {needs}"))
        row = QHBoxLayout()
        combo = QComboBox()
        for m in F.MODES:
            combo.addItem(m, m)
        combo.setCurrentIndex(F.MODES.index(mode) if mode in F.MODES else 0)
        combo.setAccessibleName(f"Mode for {name}")
        hint = plain_label(F.MODE_HINT[combo.currentData()], name="mode-hint")
        combo.currentIndexChanged.connect(lambda _i, c=combo, h=hint: h.setText(F.MODE_HINT[c.currentData()]))
        row.addWidget(buddy_label("Mode", combo))
        row.addWidget(combo)
        row.addWidget(hint, 1)
        lay.addLayout(row)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(plain_label(F.text(p.get("provenance"))))
        il.addWidget(bullet_list(F.str_list(p.get("limitations")), "No limitations listed."))
        lay.addWidget(collapsible("Provenance and limitations", inner))
        self.rows[mid] = (enabled, combo)
        return box

    def disable_all(self) -> None:
        for cb, _ in self.rows.values():
            cb.setChecked(False)
        self.msg.set(status_label("info", "All disabled. Press Save selection to apply."))

    def save(self) -> None:
        enabled = {mid: combo.currentData() for mid, (cb, combo) in self.rows.items() if cb.isChecked()}
        order = [F.text(p.get("methodology_id"), "") for p in self.profiles]
        self.save_btn.setEnabled(False)
        self.msg.set(status_label("loading", "Saving…"))

        def mutate(s: dict[str, Any]) -> None:
            s["selected_methodologies"] = F.build_selections(s.get("selected_methodologies") or [], order, enabled)

        def ok(s: dict[str, Any]) -> None:
            self.form_etag = self.ctx.settings_etag
            self.save_btn.setEnabled(True)
            n = len(s.get("selected_methodologies") or [])
            self.msg.set(status_label("ok", f"Saved: {n} methodolog{'y' if n == 1 else 'ies'} selected."))

        def err(e: Exception) -> None:
            if isinstance(e, PreconditionFailedError):
                return self.load(self.ctx.error_widget(e))  # show the newer selection; nothing was saved
            self.save_btn.setEnabled(True)
            self.msg.set(self.ctx.error_widget(e))
        self.ctx.update_settings_async(mutate, ok, err, base_etag=self.form_etag)
