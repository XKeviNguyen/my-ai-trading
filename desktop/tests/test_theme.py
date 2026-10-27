"""The desktop theme uses the side panel's design tokens and fonts, stays legible, and yields to OS high contrast."""
from __future__ import annotations

import hashlib
import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPalette  # noqa: E402

from conftest import repo_root  # noqa: E402
from my_ai_trading_desktop import theme  # noqa: E402

TEXT = ("ink", "ink_2", "muted", "persp", "ai", "risk", "event", "fault", "fresh")


def extension_static():
    path = repo_root() / "extension" / "static"
    if not (path / "sidepanel.css").exists():
        pytest.skip("extension sources not found")
    return path


def contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_tokens_match_the_side_panel_stylesheet():
    css = (extension_static() / "sidepanel.css").read_text()
    block = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    theirs = {k.replace("-", "_"): v for k, v in re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", block)}
    shared = {k: v for k, v in theirs.items() if k in theme.NEON}
    assert shared == {k: theme.NEON[k] for k in shared}
    assert set(TEXT) <= set(shared)


def test_bundled_fonts_are_the_same_files_as_the_extension():
    ours = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in theme.FONTS_DIR.iterdir() if p.is_file()}
    theirs = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (extension_static() / "fonts").iterdir() if p.is_file()}
    assert ours == theirs
    assert {"OFL-ChakraPetch.txt", "OFL-ShareTechMono.txt"} <= set(ours)  # licenses travel with the fonts


def test_every_text_color_meets_wcag_aa_on_every_background():
    for fg in TEXT:
        for bg in ("ground", "surface", "raised"):
            assert contrast(theme.NEON[fg], theme.NEON[bg]) >= 4.5, (fg, bg)
    assert contrast(theme.NEON["event"], theme.NEON["ground"]) >= 4.5  # default (primary) button text


def test_fonts_load_and_apply_sets_palette_lanes_and_font(qapp, monkeypatch):
    saved = (qapp.palette(), qapp.styleSheet(), qapp.font())
    styles: list[str] = []
    set_style = qapp.setStyle
    monkeypatch.setattr(qapp, "setStyle", lambda name: (styles.append(name), set_style(name)))
    try:
        assert {theme.UI_FONT, theme.DATA_FONT} <= set(theme.load_fonts())
        assert theme.apply(qapp) is theme.NEON
        assert styles == ["Fusion"]  # an app stylesheet wraps the style in a proxy, so check the request
        assert qapp.palette().color(QPalette.ColorRole.Window).name() == theme.NEON["ground"]
        assert qapp.font().family() == theme.UI_FONT
        sheet = qapp.styleSheet()
        for name, key in theme.LANES.items():
            assert f"Card#{name} {{ border-left: 3px solid {theme.NEON[key]}; }}" in sheet
        assert f'QLabel#kv-value, QLabel#instrument-close, QTableWidget {{ font-family: "{theme.DATA_FONT}"; }}' in sheet
    finally:
        set_style("fusion")
        qapp.setPalette(saved[0])
        qapp.setStyleSheet(saved[1])
        qapp.setFont(saved[2])


def test_high_contrast_keeps_the_system_palette(qapp, monkeypatch):
    monkeypatch.setattr(theme, "high_contrast_requested", lambda _app: True)
    before = (qapp.styleSheet(), qapp.palette().color(QPalette.ColorRole.Window).name(), qapp.font().family())
    assert theme.apply(qapp) is None
    assert (qapp.styleSheet(), qapp.palette().color(QPalette.ColorRole.Window).name(), qapp.font().family()) == before
