"""Neon night theme shared with the Chrome side panel (extension/static/sidepanel.css).

Each lane color has ONE meaning in both clients: persp = methodology perspectives, ai = model output
(unverified), risk = invalidation and contradicting evidence, event = market events and supporting
evidence, fault = data faults, fresh = data is fresh. Red/green are not used for price direction because
the product never shows buy/sell signals. When the OS asks for high contrast, the system palette wins.

Fonts are bundled (SIL OFL 1.1, see fonts/): Chakra Petch for interface text, Share Tech Mono for numbers
(its digits are equal width, so values line up).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
UI_FONT, DATA_FONT = "Chakra Petch", "Share Tech Mono"

NEON = {
    "ground": "#0b0a1c", "surface": "#12112a", "raised": "#1b1a3a", "rule": "#262552", "rule_strong": "#3a3873",
    # Edges of text fields and drop-downs: at least 3:1 against ground, surface and raised (WCAG 1.4.11).
    "control_edge": "#6e6aae",
    "ink": "#eceaff", "ink_2": "#c3c0e8", "muted": "#9894c4",
    "persp": "#8aa4ff", "ai": "#ff6cf0", "risk": "#ffb23e", "event": "#36f1ff", "fault": "#ff5d7d", "fresh": "#a4ff6a",
    "on_ink": "#0b0a1c",
}
RAINBOW = ("#ff4d8d", "#ff8a3d", "#ffd23d", "#a4ff6a", "#36f1ff", "#8aa4ff", "#c46cff")

# Object names set by the views -> lane token. Kept here so the views stay free of styling.
LANES = {"perspective-card": "persp", "model-output": "ai", "facts": "ink_2",
         "supporting_evidence": "event", "contradicting_evidence": "risk"}


def load_fonts() -> list[str]:
    """Register the bundled fonts; returns the families Qt accepted (empty if the files are missing)."""
    families: list[str] = []
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families += QFontDatabase.applicationFontFamilies(font_id)
    return sorted(set(families))


def palette(t: dict[str, str]) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "ground", QPalette.ColorRole.WindowText: "ink",
        QPalette.ColorRole.Base: "surface", QPalette.ColorRole.AlternateBase: "raised",
        QPalette.ColorRole.Text: "ink", QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.Button: "surface", QPalette.ColorRole.ButtonText: "ink",
        QPalette.ColorRole.ToolTipBase: "raised", QPalette.ColorRole.ToolTipText: "ink",
        QPalette.ColorRole.Highlight: "event", QPalette.ColorRole.HighlightedText: "on_ink",
        QPalette.ColorRole.Link: "event", QPalette.ColorRole.Mid: "rule", QPalette.ColorRole.Dark: "rule_strong",
    }
    for role, key in roles.items():
        p.setColor(role, QColor(t[key]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["muted"]))
    return p


def stylesheet(t: dict[str, str]) -> str:
    lanes = "\n".join(f"Card#{name} {{ border-left: 3px solid {t[key]}; }}" for name, key in LANES.items())
    stops = ", ".join(f"stop:{i / (len(RAINBOW) - 1):.3f} {c}" for i, c in enumerate(RAINBOW))
    rainbow = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, {stops})"
    return f"""
QWidget {{ font-family: "{UI_FONT}"; }}
QLabel#kv-value, QLabel#instrument-close, QTableWidget {{ font-family: "{DATA_FONT}"; }}
QPlainTextEdit {{ font-family: system-ui, sans-serif; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabWidget::pane {{ border: 0; border-top: 1px solid {t['rule']}; }}
QTabBar::tab {{ padding: 7px 14px; border: 0; border-bottom: 2px solid transparent; color: {t['muted']}; background: transparent; }}
QTabBar::tab:selected {{ color: #ffffff; border-bottom: 2px solid {t['event']}; }}
QTabBar::tab:hover {{ color: {t['ink']}; }}
Card {{ background: {t['surface']}; border: 1px solid {t['rule_strong']}; border-radius: 6px; }}
{lanes}
Card#instrument-header {{ border: 1px solid {t['ai']}; background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #191744, stop:1 #100f26); }}
QFrame#rainbow-rule {{ border: 0; background: {rainbow}; }}
QLabel#card-title {{ color: #ffffff; }}
QLabel#mode-label {{ color: {t['persp']}; }}
QLabel#invalidation {{ color: {t['risk']}; }}
QLabel[muted="true"] {{ color: {t['muted']}; }}
QLabel[statusKind="error"] {{ color: {t['fault']}; }}
QLabel[statusKind="ok"] {{ color: {t['fresh']}; }}
QLabel[statusKind="loading"], QLabel[statusKind="info"] {{ color: {t['ink_2']}; }}
QPushButton {{ padding: 5px 14px; border: 1px solid {t['rule_strong']}; border-radius: 4px; background: {t['surface']}; }}
QPushButton:hover {{ background: {t['raised']}; }}
QPushButton:default {{ color: {t['event']}; border: 1px solid {t['event']}; background: transparent; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QCheckBox:focus {{
    outline: none; border: 2px solid {t['event']};
}}
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {t['ground']}; border: 1px solid {t['control_edge']}; border-radius: 4px; padding: 3px 6px;
}}
"""


def high_contrast_requested(app: QApplication) -> bool:
    """True when the OS asks for high contrast. Needs Qt >= 6.10 (QStyleHints.accessibility); on an older Qt, or
    if any part of the API is missing or fails, it answers False instead of crashing the app at start-up."""
    try:
        accessibility = getattr(app.styleHints(), "accessibility", None)
        preference = getattr(Qt, "ContrastPreference", None)
        if accessibility is None or preference is None:
            return False
        return accessibility().contrastPreference() == preference.HighContrast
    except (AttributeError, RuntimeError, TypeError):
        return False


def apply(app: QApplication) -> dict[str, str] | None:
    """Apply the neon theme. Returns the tokens used, or None when the OS asks for high contrast."""
    if high_contrast_requested(app):
        return None
    if UI_FONT in load_fonts():
        font = QFont(UI_FONT, app.font().pointSize())
        app.setFont(font)
    app.setStyle("Fusion")  # the only built-in style that honors a custom palette on every OS
    app.setPalette(palette(NEON))
    app.setStyleSheet(stylesheet(NEON))
    return NEON
