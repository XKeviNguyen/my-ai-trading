"""End-to-end smoke tests: the offscreen main window talks to the REAL core API (synthetic fake exchange)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, QThread  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QLabel, QPlainTextEdit, QPushButton, QTableWidget, QToolButton,
)

from conftest import repo_root, wait_until  # noqa: E402
from my_ai_trading_desktop.app import TAB_ORDER, MainWindow  # noqa: E402
from my_ai_trading_desktop.client import ApiClient  # noqa: E402
from my_ai_trading_desktop.widgets import Card  # noqa: E402


@pytest.fixture()
def window(qapp, engine, tmp_path):
    port, token, _store = engine
    qs = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    win = MainWindow(port=port, qsettings=qs, start_polling=False)
    win.ctx.tokens.set_pasted(token)
    win.resize(420, 600)  # small window must still work
    win.show()
    yield win
    win.close()
    wait_until(lambda: win.ctx.runner.pending == 0, 10)
    win.deleteLater()


def texts(widget) -> str:
    parts = [lab.text() for lab in widget.findChildren(QLabel)]
    parts += [e.toPlainText() for e in widget.findChildren(QPlainTextEdit)]
    return "\n".join(parts)


def select_methodologies(port, token, selections):
    c = ApiClient(lambda: token, port=port)
    c.update_settings(lambda s: s.update(selected_methodologies=selections))


def test_tabs_and_accessibility_basics(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == \
        ["Analysis", "Copilot", "Strategy Studio", "Accounts", "Alerts", "Reports", "Methods", "Usage", "Settings"]
    assert len(TAB_ORDER) == 9
    assert window.symbol_edit.accessibleName() == "Symbol"
    assert window.tf_combo.findData("1M") >= 0
    assert window.minimumWidth() <= 360


def test_analysis_load_shows_timeframes_and_perspectives(window, engine):
    port, token, _ = engine
    select_methodologies(port, token, [{"methodology_id": "minervini", "mode": "NATIVE"},
                                       {"methodology_id": "darvas", "mode": "HYBRID"}])
    main_thread = QApplication.instance().thread()
    seen_threads = []
    view = window.views["analysis"]
    original = view.render
    view.render = lambda data: (seen_threads.append(QThread.currentThread() is main_thread), original(data))
    window.symbol_edit.setText("sandusdt")
    window.apply_instrument()
    assert wait_until(lambda: view.last_data is not None)
    assert seen_threads == [True]  # widgets updated on the GUI thread only
    out = texts(view.result)
    header = view.result.findChild(Card, "instrument-header")
    assert header is not None and header.title().startswith("SANDUSDT · USD-M perpetual")
    assert "binance public REST" in out
    assert "Multi-timeframe summary" in out
    assert "Alignment:" in out
    titles = [g.title() for g in view.result.findChildren(Card)]
    assert any(t.startswith("1h · OK") for t in titles)
    assert any(t.startswith("Mark Minervini · SUPPORTED_NATIVE") for t in titles)
    assert "Methodology-inspired / native scope" in out
    assert "Hybrid: native context + user timing" in out
    assert "Methodology perspectives (2)" in out
    assert "Invalidation:" in out  # minervini evaluation carries an invalidation condition
    assert "Levels: resistance prior high 20" in out
    import re
    assert not re.search(r"\b(BUY|SELL)\b", out)  # perspectives never become trade signals
    assert "Aggregation: none" in out  # the engine reports no vote across frameworks


def test_compile_save_and_approve_flow(window):
    studio = window.views["studio"]
    window.select_tab("studio")
    studio.prompts.setPlainText("alert me when the 1h close is above SMA50 and RSI is below 70")
    studio.compile()
    assert wait_until(lambda: studio.last_proposal is not None)
    out = texts(studio.compile_out)
    assert "CLOSE on 1h is above SMA(50) on 1h" in out
    assert studio.last_proposal["preview"]["rule_hash"] in out
    assert "RSI period not given; assumed 14 (Wilder)" in out

    # Save the draft, then approve it through the confirmation dialog (stubbed: no modal in tests).
    asked = []
    window.ctx.confirm = lambda parent, title, text, detail="", yes_label="": asked.append((text, yes_label)) or True
    save = next(b for b in studio.compile_out.findChildren(QPushButton) if b.text() == "Save draft")
    save.click()
    assert wait_until(lambda: "Saved strategy" in texts(studio.compile_out))
    approve = next(b for b in studio.compile_out.findChildren(QPushButton) if b.text().startswith("Approve v1"))
    approve.click()
    rule_hash = studio.last_proposal["preview"]["rule_hash"]
    assert asked and rule_hash in asked[0][0] and rule_hash in asked[0][1]
    assert wait_until(lambda: "is ACTIVE" in texts(studio.compile_out))
    assert wait_until(lambda: "ACTIVE" in " ".join(
        g.title() for g in studio.list_box.findChildren(Card)))
    assert "Preview" in [b.text() for b in studio.list_box.findChildren(
        QToolButton)]

    # Dry-run on the current symbol.
    window.ctx.set_instrument("SANDUSDT", "usdm_perpetual", "1h")
    dry = next(b for b in studio.compile_out.findChildren(QPushButton) if b.text() == "Dry-run on current symbol")
    dry.click()
    assert wait_until(lambda: "Replay over 200 closed bars" in texts(studio.compile_out))


def test_approval_cancel_does_not_activate(window, engine):
    port, token, _ = engine
    studio = window.views["studio"]
    studio.prompts.setPlainText("alert me when the 4h close is above SMA50")
    studio.last_proposal = None
    studio.compile()
    assert wait_until(lambda: studio.last_proposal is not None)
    window.ctx.confirm = lambda *a, **k: False
    next(b for b in studio.compile_out.findChildren(QPushButton) if b.text() == "Save draft").click()
    assert wait_until(lambda: "Saved strategy" in texts(studio.compile_out))
    next(b for b in studio.compile_out.findChildren(QPushButton) if b.text().startswith("Approve v1")).click()
    assert "Approval cancelled." in texts(studio.compile_out)
    strategies = ApiClient(lambda: token, port=port).strategies()["strategies"]
    rh = studio.last_proposal["preview"]["rule_hash"]
    mine = [v for s in strategies for v in s["versions"] if v["rule_hash"] == rh]
    assert mine and all(v["status"] == "DRAFT" for v in mine)


def test_copilot_ask_and_challenge(window):
    window.ctx.set_instrument("SANDUSDT", "usdm_perpetual", "1h")
    cop = window.views["copilot"]
    cop.question.setPlainText("Is the trend up?")
    cop.ask()
    assert wait_until(lambda: "Observed facts" in texts(cop.ask_out))
    assert cop.ask_out.findChild(Card, "facts").title() == "Facts (deterministic)"
    assert "AI interpretation not used" in texts(cop.ask_out)
    cop.thesis.setPlainText("Bullish: 4h breakout continues")
    cop.challenge()
    assert wait_until(lambda: "falsify" in " ".join(
        g.title() for g in cop.ch_out.findChildren(Card)))
    out = texts(cop.ch_out)
    assert "prior 20-bar low" in out
    assert "Only closed bars are used" in out


def test_vision_upload_shows_local_analysis(window, tmp_path):
    sys.path.insert(0, str(repo_root() / "tests"))
    try:
        from fakes import tiny_png
    finally:
        sys.path.pop(0)
    img = tmp_path / "chart.png"
    img.write_bytes(tiny_png())
    window.ctx.set_instrument("SANDUSDT", "usdm_perpetual", "1h")
    cop = window.views["copilot"]
    cop.image_path.setText(str(img))
    cop.analyze()
    assert wait_until(lambda: "Local image analysis" in texts(cop.vision_out) or "Error" in texts(cop.vision_out))
    out = texts(cop.vision_out)
    assert "Local image analysis" in out
    assert "Processed in memory" in out


def test_methods_usage_settings_reports_alerts(window, engine, tmp_path):
    port, token, _ = engine
    methods = window.views["methods"]
    window.select_tab("methods")
    assert wait_until(lambda: len(methods.rows) == 20)
    assert len(methods.list_box.findChildren(QCheckBox)) == 20
    methods.disable_all()
    methods.save()
    assert wait_until(lambda: "Saved: 0 methodologies selected." in texts(methods.msg))
    assert ApiClient(lambda: token, port=port).get_settings()["selected_methodologies"] == []

    usage = window.views["usage"]
    window.select_tab("usage")
    assert wait_until(lambda: "Cache hit rate" in texts(usage.result))
    assert "estimates" in texts(usage.result)
    assert usage.result.findChildren(QTableWidget) == []  # no AI calls with the deterministic engine
    assert "No AI calls this month" in texts(usage.result)

    settings = window.views["settings"]
    window.select_tab("settings")
    assert wait_until(lambda: hasattr(settings, "save_btn") and settings.prefs.findChild(QCheckBox) is not None)
    settings.add_watch_row("btc usdt", "spot")
    settings.paused.setChecked(True)
    settings.save()
    assert wait_until(lambda: "Settings saved." in texts(settings.save_out))
    saved = ApiClient(lambda: token, port=port).get_settings()
    assert {"symbol": "BTCUSDT", "market": "spot"} in saved["watchlist"] and saved["alerts_paused"] is True
    settings.test_connection()
    assert wait_until(lambda: "token accepted" in texts(settings.conn_out))
    assert token not in texts(window)

    reports = window.views["reports"]
    window.ctx.set_instrument("SANDUSDT", "usdm_perpetual", "4h")
    window.select_tab("reports")
    reports.save_current()
    assert wait_until(lambda: "Saved report" in texts(reports.msg))
    assert wait_until(lambda: "Export Markdown…" in [b.text() for b in reports.list_box.findChildren(
        QPushButton)])
    target = tmp_path / "exported"
    reports.choose_save_path = lambda suggested: str(target)
    rid = ApiClient(lambda: token, port=port).reports()["reports"][0]["id"]
    reports.export(rid)
    md_path = Path(str(target) + ".md")
    assert wait_until(lambda: md_path.exists() and "Exported report" in texts(reports.msg))
    assert md_path.read_text(encoding="utf-8").strip()
    assert (md_path.stat().st_mode & 0o077) == 0

    alerts = window.views["alerts"]
    alerts.poll()
    assert wait_until(lambda: alerts.last_status is not None)
    assert alerts.last_status[1] is True and alerts.feed.baseline_done


def test_wrong_token_shows_unauthorized_with_settings_shortcut(window):
    window.ctx.tokens.set_pasted("w" * 40)
    usage = window.views["usage"]
    usage.load()
    assert wait_until(lambda: "API token missing or invalid" in texts(usage.result))
    assert any(b.text() == "Open Settings" for b in usage.result.findChildren(QPushButton))
    assert "w" * 40 not in texts(window)


def test_alert_poll_baseline_then_notifies_non_quiet(qapp, engine, tmp_path):
    from my_ai_trading_desktop.views.alerts import AlertsView
    port, token, store = engine
    win = MainWindow(port=port, qsettings=QSettings(str(tmp_path / "a.ini"), QSettings.Format.IniFormat),
                     start_polling=False)
    win.ctx.tokens.set_pasted(token)
    sent = []
    view = AlertsView(win.ctx, notify=lambda t, b, p: sent.append((t, b, p)), tray_available=True)
    view.notify_box.setChecked(True)

    def add(eid, quiet, name):
        store.record_alert(eid, user="local", instrument_key="binance:usdm_perpetual:SANDUSDT", timeframe="1h",
                           source="strategy", event_type="STRATEGY_MATCH", bar_open_time_ms=1770069600000,
                           priority=2, payload={"name": name, "version": 1, "quiet": quiet})
    add("desk-old", False, "Old")
    view.poll()
    assert wait_until(lambda: view.feed.baseline_done and not view.inflight)
    assert sent == []  # first poll only sets a baseline
    add("desk-loud", False, "<b>Loud</b>")
    add("desk-quiet", True, "Quiet")
    view.poll()
    assert wait_until(lambda: not view.inflight and len(view.feed.alerts) >= 3)
    assert len(sent) == 1 and "Loud" in sent[0][1] and "<" not in sent[0][1]
    assert "quiet hours" in " ".join(g.title() for g in view.list_box.findChildren(Card))
    view.deleteLater()
    win.close()


def test_engine_down_message(qapp, tmp_path):
    from conftest import free_port
    qs = QSettings(str(tmp_path / "d.ini"), QSettings.Format.IniFormat)
    win = MainWindow(port=free_port(), qsettings=qs, start_polling=False)
    win.ctx.tokens.set_pasted("z" * 40)
    usage = win.views["usage"]
    usage.load()
    assert wait_until(lambda: "my-ai-trading serve" in texts(usage.result))
    win.close()


def test_saving_methodology_settings_invalidates_the_analysis(window, engine):
    port, token, _ = engine
    select_methodologies(port, token, [{"methodology_id": "minervini", "mode": "NATIVE"}])
    view = window.views["analysis"]
    window.symbol_edit.setText("sandusdt")
    window.apply_instrument()
    assert wait_until(lambda: view.last_data is not None)
    assert "Methodology perspectives (1)" in texts(view.result)
    window.select_tab("methods")
    done = []
    window.ctx.update_settings_async(lambda s: s.update(selected_methodologies=[]), done.append, done.append)
    assert wait_until(lambda: done)
    view.last_data = None
    window.select_tab("analysis")
    assert wait_until(lambda: view.last_data is not None)
    assert "Methodology perspectives (0)" in texts(view.result)
