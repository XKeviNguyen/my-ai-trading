"""Regression tests for the 2026-10-27 six-breaker audit fixes in the desktop client: FLOW-3 (journal actions and
verification), FLOW-8 (settings compare-and-set), ENV-3 (XDG paths), ENV-7 (high-contrast API), ENV-9 (input
border contrast) and SIMP-4 (dead methods)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton  # noqa: E402

from conftest import TOKEN, wait_until  # noqa: E402
from my_ai_trading_desktop import client as C  # noqa: E402
from my_ai_trading_desktop import formatting as F  # noqa: E402
from my_ai_trading_desktop import theme  # noqa: E402
from my_ai_trading_desktop.app import MainWindow  # noqa: E402
from my_ai_trading_desktop.context import TokenSource  # noqa: E402
from my_ai_trading_desktop.widgets import Card  # noqa: E402

COID = "mat0123456789abcdef"
STATES = ("SENDING", "UNKNOWN", "NOT_FOUND", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL",
          "FILLED", "CANCELED", "REJECTED", "EXPIRED", "USER_RESOLVED")


def texts(widget) -> str:
    return "\n".join(lab.text() for lab in widget.findChildren(QLabel))


def visible_buttons(widget) -> list[str]:
    return [b.text() for b in widget.findChildren(QPushButton) if not b.isHidden()]


# ---------------------------------------------------------------- FLOW-3: journal rules
def test_cancel_is_offered_for_every_non_final_state():
    assert [s for s in STATES if F.journal_can_cancel({"state": s})] == \
        ["SENDING", "UNKNOWN", "NOT_FOUND", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"]
    assert F.journal_can_cancel({"state": "NEW", "acknowledged": 1, "verification": "not_listed"})


def test_mark_resolved_only_for_never_acknowledged_unsettled_orders():
    for s in STATES:
        assert F.journal_can_resolve({"state": s, "acknowledged": 0}) == (s in ("SENDING", "UNKNOWN", "NOT_FOUND")), s
        assert not F.journal_can_resolve({"state": s, "acknowledged": 1}), s


def test_verification_lines_show_not_listed_and_times():
    t = int(datetime(2026, 10, 27, 13, 5, tzinfo=timezone.utc).timestamp() * 1000)
    lines = F.journal_verification_lines({"verification": "not_listed", "acknowledged": 0, "last_checked_at": t,
                                          "last_seen_at": None, "lookups": 2})
    assert lines[0] == "Exchange did not list this order at the last check — do not place it again."
    assert "Last checked: 2026-10-27 13:05 UTC" in lines
    assert "Last seen by the exchange: never" in lines
    assert "Not listed in 2 spaced checks." in lines
    ack = F.journal_verification_lines({"verification": "not_listed", "acknowledged": 1, "last_checked_at": t,
                                        "last_seen_at": t - 60_000, "lookups": 1})
    assert any("confirmed this order before" in x for x in ack)
    assert "Last seen by the exchange: 2026-10-27 13:04 UTC" in ack
    assert "check failed" in F.journal_verification_lines({"verification": "lookup_failed"})[0]
    assert F.journal_verification_lines({"verification": None}) == []


def test_journal_texts_never_suggest_placing_the_order_again():
    all_text = [F.NOT_LISTED_TEXT, F.RESOLVE_CONFIRM_TEXT, F.RESOLVE_CONFIRM_DETAIL, F.CANCEL_CONFIRM_TEXT,
                F.CANCEL_CONFIRM_DETAIL]
    for v in ("not_listed", "lookup_failed", "confirmed"):
        for a in (0, 1):
            all_text += F.journal_verification_lines({"verification": v, "acknowledged": a, "last_checked_at": 1,
                                                      "last_seen_at": 1, "lookups": 3})
    for line in all_text:
        for m in re.finditer(r"\b(place|resend|re-?submit|new order)\b", line, re.IGNORECASE):
            assert re.search(r"(do not|don't|never)\s*$", line[max(0, m.start() - 12):m.start()].lower()), line
    assert "checked this order in the exchange's order history" in F.RESOLVE_CONFIRM_DETAIL
    assert "does not cancel anything" in F.RESOLVE_CONFIRM_DETAIL


def test_client_resolve_order_validates_id_and_confirms(monkeypatch):
    c = C.ApiClient(lambda: "t" * 40, port=1)
    seen = []
    monkeypatch.setattr(c, "request", lambda *a, **k: seen.append((a, k)) or {"state": "USER_RESOLVED"})
    assert c.resolve_order(COID)["state"] == "USER_RESOLVED"
    assert seen == [(("POST", f"/api/v1/trading/orders/{COID}/resolve"), {"json_body": {"confirm": True}})]
    for bad in ("mat123", "../settings", "MAT0123456789abcdef"):
        with pytest.raises(C.ValidationError):
            c.resolve_order(bad)
    assert len(seen) == 1


# ---------------------------------------------------------------- window fixtures
@pytest.fixture()
def window(qapp, engine, tmp_path):
    port, token, store = engine
    qs = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    win = MainWindow(port=port, qsettings=qs, start_polling=False)
    win.ctx.tokens.set_pasted(token)
    win.resize(420, 600)
    win.show()
    before = store.settings()
    yield win, store
    win.close()
    wait_until(lambda: win.ctx.runner.pending == 0, 10)
    win.deleteLater()
    store.save_settings(before)  # the engine is shared by the session: leave settings as found


def test_unsettled_journal_row_offers_cancel_and_resolve_and_shows_verification(window, monkeypatch):
    win, _ = window
    view = win.views["accounts"]
    row = {"client_order_id": COID, "state": "UNKNOWN", "acknowledged": 0, "verification": "not_listed",
           "last_checked_at": 1_761_570_300_000, "last_seen_at": None, "lookups": 1, "message": None,
           "request_json": '{"side": "buy", "quantity": "0.1", "symbol": "ETHUSDT", "type": "limit", "price": "99.5"}'}
    box = view._journal_row(row, True)
    assert isinstance(box, Card) and box.title() == "buy 0.1 ETHUSDT limit @ 99.5 · UNKNOWN · needs a status check"
    assert visible_buttons(box) == ["Check status", "Cancel order", "Mark resolved…"]
    out = texts(box)
    assert "Exchange did not list this order at the last check — do not place it again." in out
    assert "Last checked: 2025-10-27" in out and "Last seen by the exchange: never" in out
    assert "new order" not in out.lower()

    asked = []
    monkeypatch.setattr(win.ctx.client, "resolve_order",
                        lambda coid: {"client_order_id": coid, "state": "USER_RESOLVED",
                                      "note": "Marked resolved by you."})
    win.ctx.confirm = lambda parent, title, text, detail="", yes_label="": asked.append((text, detail)) or False
    next(b for b in box.findChildren(QPushButton) if b.text() == "Mark resolved…").click()
    assert asked and asked[0][0] == "Mark this order resolved?" and "order history" in asked[0][1]
    assert box.title().endswith("UNKNOWN · needs a status check")  # declined: nothing sent
    win.ctx.confirm = lambda *a, **k: True
    next(b for b in box.findChildren(QPushButton) if b.text() == "Mark resolved…").click()
    assert wait_until(lambda: "USER_RESOLVED — Marked resolved by you." in texts(box))
    assert box.title() == "buy 0.1 ETHUSDT limit @ 99.5 · USER_RESOLVED"
    assert visible_buttons(box) == ["Check status"]


def test_acknowledged_order_not_listed_keeps_cancel_but_never_resolve(window):
    win, _ = window
    box = win.views["accounts"]._journal_row(
        {"client_order_id": COID, "state": "NEW", "acknowledged": 1, "verification": "not_listed",
         "last_checked_at": 2, "last_seen_at": 1, "lookups": 3, "request_json": "{}"}, True)
    assert visible_buttons(box) == ["Check status", "Cancel order"]
    assert "confirmed this order before" in texts(box)


# ---------------------------------------------------------------- FLOW-8: settings compare-and-set
def test_stale_settings_form_gets_412_reloads_and_never_overwrites(window):
    from my_ai_trading.store import UserSettings
    win, store = window
    store.save_settings(UserSettings(llm_data_consent=True))
    win.select_tab("settings")
    view = win.views["settings"]
    view.load_prefs()
    assert wait_until(lambda: win.ctx.runner.pending == 0 and hasattr(view, "consent") and view.consent.isChecked())
    assert view.form_etag == store.settings_etag()

    # Elsewhere (the side panel, another window): consent revoked after this form was loaded.
    store.save_settings(UserSettings(llm_data_consent=False, analysis_timeframe="1h"))
    view.atf.setCurrentIndex(view.atf.findData("4h"))
    view.save_btn.click()
    assert wait_until(lambda: "changed elsewhere" in texts(view.prefs))
    msg = texts(view.prefs)
    assert "nothing was overwritten" in msg and "reloaded" in msg
    assert store.settings().llm_data_consent is False, "a revoked consent must not be restored"
    assert store.settings().analysis_timeframe == "1h"
    # the form now shows the newer settings and their tag
    assert not view.consent.isChecked()
    assert view.form_etag == store.settings_etag() == win.ctx.settings_etag

    # Saving again from the reloaded form succeeds and keeps the revocation.
    view.atf.setCurrentIndex(view.atf.findData("4h"))
    view.save_btn.click()
    assert wait_until(lambda: "Settings saved." in texts(view.prefs))
    assert store.settings().analysis_timeframe == "4h" and store.settings().llm_data_consent is False
    assert view.form_etag == store.settings_etag()


def test_methodology_selection_conflict_reloads(window):
    from my_ai_trading.store import UserSettings
    win, store = window
    win.select_tab("methods")
    view = win.views["methods"]
    view.load()
    assert wait_until(lambda: win.ctx.runner.pending == 0 and bool(view.rows))
    store.save_settings(UserSettings(selected_methodologies=[]))  # changed elsewhere
    first = next(iter(view.rows.values()))[0]
    first.setChecked(not first.isChecked())
    view.save_btn.click()
    assert wait_until(lambda: "changed elsewhere" in texts(view.msg) and win.ctx.runner.pending == 0)
    assert store.settings().selected_methodologies == []
    assert view.form_etag == store.settings_etag()
    assert not any(cb.isChecked() for cb, _ in view.rows.values() if isinstance(cb, QCheckBox))


def test_client_sends_if_match_and_maps_412(engine):
    port, token, store = engine
    before = store.settings()
    try:
        c = C.ApiClient(lambda: token, port=port)
        s, tag = c.get_settings_versioned()
        assert tag == store.settings_etag()
        saved, new_tag = c.put_settings_versioned(s, if_match=tag)
        assert new_tag == store.settings_etag()
        with pytest.raises(C.PreconditionFailedError) as ei:
            c.put_settings(s, if_match='"0000000000000000"')
        assert ei.value.kind == "changed_elsewhere" and ei.value.status == 412
        assert "changed elsewhere" in str(ei.value) and "reloaded" in str(ei.value)
        with pytest.raises(C.ValidationError):
            c.put_settings(s, if_match='"x"\r\nX-Evil: 1')
        # update_settings uses the tag of its own GET (or the form's), so it never overwrites blindly
        assert c.update_settings(lambda cur: cur.update(alerts_paused=True))["alerts_paused"] is True
        with pytest.raises(C.PreconditionFailedError):
            c.update_settings_versioned(lambda cur: cur.update(alerts_paused=False), base_etag='"ffffffffffffffff"')
        assert store.settings().alerts_paused is True
    finally:
        store.save_settings(before)


# ---------------------------------------------------------------- ENV-3, ENV-7, ENV-9, SIMP-4
@pytest.mark.parametrize("value", ["", "relative/config", "./x"])
def test_token_path_ignores_empty_and_relative_xdg(monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert C.token_path() == Path.home() / ".config" / "my-ai-trading" / "api_token"


def test_token_path_uses_absolute_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert C.token_path() == tmp_path / "my-ai-trading" / "api_token"


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_input_borders_reach_3_to_1_non_text_contrast():
    sheet = theme.stylesheet(theme.NEON)
    rule = re.search(r"QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox \{([^}]*)\}", sheet)
    assert rule, "input rule missing"
    color = re.search(r"border: 1px solid (#[0-9a-f]{6})", rule.group(1)).group(1)
    assert color == theme.NEON["control_edge"]
    for bg in ("ground", "surface", "raised"):
        assert _contrast(color, theme.NEON[bg]) >= 3, bg
    assert _contrast(theme.NEON["rule_strong"], theme.NEON["ground"]) < 3  # the old border was the defect


class _Hints:
    pass


class _App:
    def __init__(self, hints):
        self._hints = hints

    def styleHints(self):  # noqa: N802 - Qt naming
        return self._hints


def test_high_contrast_detection_never_crashes_when_the_api_is_missing(monkeypatch):
    assert theme.high_contrast_requested(_App(_Hints())) is False  # Qt < 6.10: no accessibility()

    class Broken:
        def accessibility(self):
            raise RuntimeError("platform plugin has no accessibility hints")
    assert theme.high_contrast_requested(_App(Broken())) is False
    monkeypatch.setattr(theme, "Qt", object())  # a Qt without ContrastPreference

    class Hints:
        def accessibility(self):
            raise AssertionError("must not be called without Qt.ContrastPreference")
    assert theme.high_contrast_requested(_App(Hints())) is False


def test_high_contrast_detection_reads_the_preference(qapp):
    from PySide6.QtCore import Qt

    class Acc:
        def __init__(self, pref):
            self.pref = pref

        def contrastPreference(self):  # noqa: N802 - Qt naming
            return self.pref

    class Hints:
        def __init__(self, pref):
            self.acc = Acc(pref)

        def accessibility(self):
            return self.acc
    if not hasattr(Qt, "ContrastPreference"):
        pytest.skip("Qt < 6.10")
    assert theme.high_contrast_requested(_App(Hints(Qt.ContrastPreference.HighContrast))) is True
    assert theme.high_contrast_requested(_App(Hints(Qt.ContrastPreference.NoPreference))) is False
    assert isinstance(theme.high_contrast_requested(qapp), bool)  # the real API on this Qt


def test_pyproject_requires_a_qt_with_the_high_contrast_api():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"PySide6-Essentials>=6.10,<7"' in text


def test_dead_methods_are_gone():
    assert not hasattr(TokenSource, "has_pasted")
    assert not hasattr(C.ApiClient, "report_json")
    t = TokenSource()
    t.set_pasted(TOKEN)
    assert t() == TOKEN and "hidden" in repr(t)
