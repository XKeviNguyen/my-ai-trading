"""Pure helper tests (no Qt)."""
from __future__ import annotations

import math

from my_ai_trading_desktop import formatting as F


def test_numbers():
    assert F.fmt_num(1.95735) == "1.95735"
    assert F.fmt_num(100.0, 4) == "100"
    assert F.fmt_num(0.098841393, 6) == "0.0988414"
    assert F.fmt_num(2e9) == "2.000e+09"
    assert F.fmt_num(None) == F.DASH
    assert F.fmt_num(math.nan) == F.DASH
    assert F.fmt_num(True) == F.DASH
    assert F.fmt_int(1234567.4) == "1,234,567"
    assert F.fmt_int("x") == F.DASH
    assert F.fmt_usd(0.004) == "$0.004000"
    assert F.fmt_usd(1.5) == "$1.50"
    assert F.fmt_usd(None) == "unknown"
    assert F.fmt_rate(0.25) == "25.0%"
    assert F.fmt_rate(None) == "n/a (no AI calls)"


def test_times():
    assert F.fmt_ms(1770076800000) == "2026-02-03 00:00 UTC"
    assert F.fmt_ms(0) == F.DASH
    now = 1770076800000
    assert F.fmt_age("2026-02-03T00:00:00Z", now) == "0 s ago"
    assert F.fmt_age(now - 600_000, now) == "10 min ago"
    assert F.fmt_age(now - 3 * 3600_000, now) == "3 h ago"
    assert F.fmt_age(now - 5 * 86400_000, now) == "5 d ago"
    assert F.fmt_age(now + 10_000, now) == "in the future"
    assert F.fmt_age("garbage", now) == ""


def test_fmt_json_and_text():
    assert F.fmt_json(True) == "true"
    assert F.fmt_json(None) == F.DASH
    assert F.fmt_json({"b": 1, "a": [1, 2]}) == '{"a": [1, 2], "b": 1}'
    assert F.fmt_json("x" * 300).endswith("…")
    assert F.text(None) == F.DASH
    assert F.text("", "fallback") == "fallback"
    assert F.text(5) == "5"
    assert F.str_list(["a", 1, None]) == ["a", "1", ""]
    assert F.str_list("nope") == []


def test_symbols_and_markets():
    assert F.normalize_symbol(" sand/usdt ") == "SANDUSDT"
    assert F.normalize_symbol("SAND_USDT") == "SANDUSDT"
    assert F.normalize_symbol("ab") is None
    assert F.normalize_symbol("<script>") is None
    assert F.market_label("spot") == "Spot"
    assert F.market_label("usdm_perpetual") == "USD-M perpetual"
    assert F.symbol_from_key("binance:usdm_perpetual:SANDUSDT") == "SANDUSDT"


def test_mode_labels_are_exact():
    assert F.MODE_HINT == {
        "NATIVE": "Methodology-inspired / native scope",
        "HYBRID": "Hybrid: native context + user timing",
        "ADAPTED": "User-adapted — unvalidated; not the trader's original system",
    }


def test_build_selections_keeps_order_and_allows_none():
    prev = [{"methodology_id": "darvas", "mode": "NATIVE"}, {"methodology_id": "minervini", "mode": "NATIVE"}]
    out = F.build_selections(prev, ["minervini", "brandt", "darvas"], {"minervini": "HYBRID", "brandt": "ADAPTED",
                                                                        "darvas": "NATIVE"})
    assert out == [{"methodology_id": "darvas", "mode": "NATIVE"}, {"methodology_id": "minervini", "mode": "HYBRID"},
                   {"methodology_id": "brandt", "mode": "ADAPTED"}]
    assert F.build_selections(prev, ["minervini"], {}) == []


def test_watchlist_and_prompts():
    items, errors = F.clean_watchlist([("sandusdt", "usdm_perpetual"), ("SANDUSDT", "usdm_perpetual"),
                                       ("btc usdt", "spot"), ("", "spot"), ("??", "spot"), ("ETHUSDT", "weird")])
    assert items == [{"symbol": "SANDUSDT", "market": "usdm_perpetual"}, {"symbol": "BTCUSDT", "market": "spot"},
                     {"symbol": "ETHUSDT", "market": "usdm_perpetual"}]
    assert errors == ['"??" is not a valid symbol']
    many, errs = F.clean_watchlist([(f"SYM{i:03d}", "spot") for i in range(51)])
    assert len(many) == 51 and errs == ["At most 50 watchlist entries."]
    assert F.split_prompts("a\n\n  b  \r\nc") == ["a", "b", "c"]
    assert F.validate_compile_input([], "x") is not None
    assert F.validate_compile_input(["p"] * 11, "x") is not None
    assert F.validate_compile_input(["p"], "bad<name>") == "Name contains unsupported characters."
    assert F.validate_compile_input(["p"], "My rule (v2)") is None


def test_check_image():
    assert F.check_image("a.PNG", 10) == ("image/png", None)
    assert F.check_image("a.jpeg", 10) == ("image/jpeg", None)
    assert F.check_image("a.gif", 10)[1] is not None
    assert F.check_image("a.png", 0)[1] == "The file is empty."
    assert F.check_image("a.webp", 6 * 1024 * 1024)[1] == "Image is larger than 5 MB."


def _alert(seq, quiet=False, et="STRATEGY_MATCH", **payload):
    return {"seq": seq, "instrument_key": "binance:spot:SANDUSDT", "timeframe": "1h", "source": "strategy",
            "event_type": et, "bar_open_time_ms": 1770076800000, "priority": 2, "created_at": 1770076800000,
            "payload": {"quiet": quiet, "name": "R", "version": 1, **payload}}


def test_alert_feed_baseline_then_notifies_only_non_quiet():
    feed = F.AlertFeed()
    assert feed.apply({"alerts": [_alert(3), _alert(2)], "next_after_seq": 3}) == []  # baseline
    assert feed.last_seq == 3 and len(feed.alerts) == 2
    loud = feed.apply({"alerts": [_alert(4), _alert(5, quiet=True), _alert(3)], "next_after_seq": 5})
    assert [a["seq"] for a in loud] == [4]
    assert feed.last_seq == 5
    assert [a["seq"] for a in feed.alerts] == [5, 4, 3, 2]
    assert feed.apply({"alerts": [], "next_after_seq": 5}) == []
    burst = feed.apply({"alerts": [_alert(i) for i in range(6, 16)], "next_after_seq": 15})
    assert len(burst) == F.MAX_NOTIFICATIONS_PER_POLL and burst[0]["seq"] == 6
    feed.reset()
    assert feed.last_seq == 0 and not feed.baseline_done


def test_alert_summaries_and_notification_text_are_markup_free():
    assert F.alert_summary(_alert(1)) == 'Strategy "R" v1 matched (custom strategy, closed bar)'
    a = _alert(1, et="METHODOLOGY_STATE", methodology_id="m", **{"from": "A", "to": "B", "label": "L"})
    assert F.alert_summary(a) == "m: A → B (L)"
    assert F.alert_summary(_alert(1, et="STALE_DATA")) == "Data is stale"
    ev = _alert(1, et="BREAKOUT", event={"type": "BREAKOUT", "invalidation": "close < 1"})
    assert F.alert_summary(ev) == "Invalidated by close < 1"
    evil = _alert(1, name='<b>x</b> & <a href="http://e">y</a>\n')
    title, body = F.notification_text(evil)
    assert title == "SANDUSDT 1h · STRATEGY_MATCH"
    for ch in "<>&\n":
        assert ch not in body
    assert len(body) <= 200


def test_analysis_helpers():
    r = {"bar_close_time_utc": "2026-02-02T23:00:00Z", "last_close": 1.9598,
         "indicator_values": {"sma50": 1.95735, "sma200": None, "rsi14_wilder": 100.0, "volume_ratio_20": 1.0}}
    rows = dict(F.timeframe_rows(r, 1770076800000))
    assert rows["Bar close"] == "2026-02-02T23:00:00Z (60 min ago)"
    assert rows["SMA200"] == F.DASH and rows["RSI14 (Wilder)"] == "100"
    assert F.levels_text({"support_prior_low_20": 1.5}) == "support prior low 20 1.5"
    assert F.levels_text({}) == "No levels reported."
    lines = F.event_lines([{"type": "BREAKOUT", "bar_close_time_utc": "t", "invalidation": "close < x"}, "junk"])
    assert lines == ["BREAKOUT · bar closed t\n    Invalidation: close < x"]
    assert F.alignment_lines(None) == ["No alignment data."]
    assert F.alignment_lines({"verdict": "PARTIAL", "trend_by_timeframe": {"1h": "UP"}, "note": "n"}) == \
        ["Alignment: PARTIAL", "1h: UP", "n"]


def test_model_meta_and_fallback():
    assert F.model_meta({"model": "m", "input_tokens": 10, "output_tokens": 2, "estimated_cost_usd": 0.5,
                         "latency_ms": 1200}) == "model m · tokens in 10 / out 2 · est. cost $0.50 · 1,200 ms"
    assert F.fallback_text(None, None) is None
    assert F.fallback_text(None, "no consent") == "Route: deterministic. AI interpretation not used: no consent"


def test_usage_and_report_helpers():
    rows = dict(F.usage_rows({"total_tokens": 50, "monthly_ai_token_budget": 200, "total_cost_usd": 0,
                              "monthly_ai_budget_usd": 0, "calls_with_unknown_cost": 0, "cache_hits": 3,
                              "cache_hit_rate": 0.375}))
    assert rows["Tokens"] == "50 / 200 (25% of budget)"
    assert rows["Estimated cost"] == "$0.00 / no USD cap (no limit set)"
    assert rows["Cache hits"] == "3" and rows["Cache hit rate"] == "37.5%"
    assert dict(F.usage_rows({}))["Cache hit rate"] == "n/a (no AI calls)"
    assert F.usage_table_row({"feature": "ask", "provider": "p", "model": "m", "n": 2, "i": 10, "o": None, "c": 0.5,
                              "unknown_cost": 1, "lat": 300}) == \
        ["ask", "p/m", "2", "10", "0", "$0.50 + 1 unknown", "300"]
    assert F.report_filename("0123456789ab") == "report-0123456789ab.md"
    assert F.report_filename("../../x") == "report-report.md"


def test_preview_rows():
    rows = dict(F.preview_rows({"name": "R", "rule_hash": "0123456789abcdef", "timeframes_used": ["1h", "4h"],
                                "cooldown_bars": 0}))
    assert rows["rule_hash"] == "0123456789abcdef"
    assert rows["Timeframes used"] == "1h, 4h"
    assert rows["Cooldown bars"] == "0"
    assert F.preview_rows(None) == []
