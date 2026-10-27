"""Static guard: the desktop source must not render rich text/HTML or execute code/shell commands."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "my_ai_trading_desktop"

FORBIDDEN = [
    ("setHtml", re.compile(r"setHtml")),
    ("insertHtml/appendHtml", re.compile(r"(insert|append)Html")),
    ("Qt.RichText", re.compile(r"RichText")),
    ("Qt.AutoText", re.compile(r"AutoText")),
    ("builtin eval(", re.compile(r"(?<![\w.])eval\(")),
    # Method calls such as QApplication.exec() / QMessageBox.exec() are Qt event loops, not code execution.
    ("builtin exec(", re.compile(r"(?<![\w.])exec\(")),
    ("subprocess", re.compile(r"subprocess")),
    ("shell=True", re.compile(r"shell\s*=\s*True")),
    ("os.system", re.compile(r"os\.system")),
    ("pickle", re.compile(r"\bpickle\b")),
    ("__import__", re.compile(r"__import__")),
]


def sources() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "desktop sources not found"
    return files


def test_no_forbidden_constructs():
    hits = []
    for path in sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, rx in FORBIDDEN:
                if rx.search(line):
                    hits.append(f"{path.name}:{n}: {label}")
    assert hits == []


def test_labels_are_created_only_by_plain_text_helpers():
    """QLabel defaults to auto-detecting rich text, so every label must come from widgets.py helpers."""
    offenders = [p.name for p in sources() if p.name != "widgets.py" and re.search(r"\bQLabel\(", p.read_text())]
    assert offenders == []
    lines = (SRC / "widgets.py").read_text().splitlines()
    created = [i for i, line in enumerate(lines) if "QLabel()" in line]
    assert created
    for i in created:  # each label switches to plain text right after construction
        assert "setTextFormat(Qt.TextFormat.PlainText)" in lines[i + 1], lines[i]


def test_message_boxes_use_plain_text():
    for p in sources():
        src = p.read_text()
        if "QMessageBox(" in src:
            assert "setTextFormat(Qt.TextFormat.PlainText)" in src, p.name
        assert "QMessageBox.information(" not in src and "QMessageBox.warning(" not in src, p.name
        assert "setToolTip(" not in src or p.name == "app.py", p.name  # tooltips can render rich text


def test_only_loopback_urls_in_source():
    urls = set()
    for p in sources():
        urls.update(re.findall(r"https?://[^\s\"'`)]+", p.read_text()))
    assert urls <= {"http://127.0.0.1:8765", "http://{HOST}:{port}", "http://127.0.0.1:8765.", "http://127.0.0.1:<port>"}, urls
