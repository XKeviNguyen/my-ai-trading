import pytest

from my_ai_trading.fixtures import downtrend_closes, series_from_closes, uptrend_closes
from my_ai_trading.strategies import (darvas_box, minervini_trend_template, turtle_breakout, weinstein_stage,
                                      williams_volatility_breakout, raschke_holy_grail,
                                      brandt_classical_patterns, edwards_magee_trend_and_patterns, BRANDT_KINDS)


def test_minervini_price_criteria_partial_and_failed_and_insufficient():
    up = minervini_trend_template(series_from_closes(uptrend_closes(400)))
    assert up["state"] == "PRICE_CRITERIA_MET_PARTIAL"
    assert any("relative_strength" in c for c in up["unevaluated_criteria"])
    assert minervini_trend_template(series_from_closes(downtrend_closes(400)))["state"] == "TEMPLATE_FAILED"
    assert minervini_trend_template(series_from_closes(uptrend_closes(100)))["state"] in (
        "INSUFFICIENT_DATA", "TEMPLATE_FAILED")
    with pytest.raises(ValueError):
        minervini_trend_template(series_from_closes(uptrend_closes(400), "1h"))


def test_weinstein_requires_weekly_and_classifies_stages():
    assert weinstein_stage(series_from_closes(uptrend_closes(80, growth=0.02), "1w"))["state"] == "STAGE_2_ADVANCING"
    assert weinstein_stage(series_from_closes(downtrend_closes(80, decay=0.02), "1w"))["state"] == "STAGE_4_DECLINING"
    base = downtrend_closes(60, decay=0.02) + [downtrend_closes(60, decay=0.02)[-1]] * 30
    assert weinstein_stage(series_from_closes(base, "1w"))["state"] in ("STAGE_1_BASING", "STAGE_4_DECLINING")
    assert weinstein_stage(series_from_closes(uptrend_closes(30), "1w"))["state"] == "INSUFFICIENT_DATA"
    with pytest.raises(ValueError, match="weekly"):
        weinstein_stage(series_from_closes(uptrend_closes(300), "1h"))


def test_turtle_breakouts_and_2n_reference():
    closes = [1.0 + 0.001 * (i % 3) for i in range(70)] + [1.2]
    r = turtle_breakout(series_from_closes(closes))
    assert {"SYSTEM1_20D_HIGH_BREAKOUT", "SYSTEM2_55D_HIGH_BREAKOUT"} <= set(r["signals"])
    assert r["volatility_reference"]["two_N"] == pytest.approx(2 * r["values"]["N"])
    flat = turtle_breakout(series_from_closes([1.0 + 0.001 * (i % 3) for i in range(70)]))
    assert flat["state"] == "NO_BREAKOUT"


def test_darvas_box_breakout_and_inside():
    rise = [1.0 + 0.02 * i for i in range(15)]
    box = [1.28, 1.25, 1.22, 1.20, 1.21, 1.22, 1.23, 1.22, 1.24]
    inside = darvas_box(series_from_closes(rise + box, spread=0.0))
    assert inside["state"] == "INSIDE_BOX" and inside["box"] == {"top": 1.28, "bottom": 1.2}
    out = darvas_box(series_from_closes(rise + box + [1.40], spread=0.0))
    assert out["state"] == "BREAKOUT_ABOVE_BOX" and out["box"]["top"] == 1.28
    assert "volume confirmation" in out["unevaluated_criteria"]
    assert darvas_box(series_from_closes(rise + box + [1.10], spread=0.0))["state"] == "BREAK_BELOW_BOX"


def test_williams_volatility_breakout_levels():
    from dataclasses import replace
    s = series_from_closes([1.0, 1.0, 1.0], spread=0.05)
    last = s.candles[-1]
    prev = s.candles[-2]
    rng = prev.high - prev.low
    up = replace(last, high=last.open + 0.7 * rng, close=last.open + 0.5 * rng)
    r = williams_volatility_breakout(type(s)(s.instrument, s.timeframe, s.candles[:-1] + (up,)))
    assert r["state"] == "UPSIDE_LEVEL_EXCEEDED" and r["values"]["upside_level"] == pytest.approx(last.open + 0.6 * rng)
    assert williams_volatility_breakout(s)["state"] == "NO_LEVEL_EXCEEDED"
    with pytest.raises(ValueError):
        williams_volatility_breakout(series_from_closes([1.0] * 5, "1h"))


def test_raschke_holy_grail_needs_strong_rising_trend_and_ema_touch():
    from dataclasses import replace
    from my_ai_trading.indicators import ema
    s = series_from_closes(uptrend_closes(120, growth=0.02))
    r = raschke_holy_grail(s)
    assert r["checks"]["adx_above_30"] and r["state"] in ("SETUP_PRESENT_LONG_SIDE", "NO_SETUP")
    e = ema([c.close for c in s.candles[:-1]] + [s.candles[-1].close], 20)
    last = s.candles[-1]
    touched = replace(last, low=min(last.low, e * 0.999), open=max(last.open, e * 0.999))
    s2 = type(s)(s.instrument, s.timeframe, s.candles[:-1] + (touched,))
    r2 = raschke_holy_grail(s2)
    assert r2["checks"]["bar_touches_ema20"]
    assert r2["state"] == ("SETUP_PRESENT_LONG_SIDE" if r2["checks"]["adx_rising"] else "NO_SETUP")
    assert raschke_holy_grail(series_from_closes(uptrend_closes(20)))["state"] == "INSUFFICIENT_DATA"
    with pytest.raises(ValueError):
        raschke_holy_grail(series_from_closes(uptrend_closes(100), "1h"))


def test_brandt_classical_patterns_report_shape():
    r = brandt_classical_patterns(series_from_closes(uptrend_closes(300)))
    assert r["state"] in ("COMPLETED_BREAKS", "PATTERNS_PRESENT", "NO_RECENT_PATTERN") and "discretionary" in r["note"]
    assert r["method_version"] == "2" and r["profile"]["profile_id"] == "brandt"
    for p in r["patterns"]:
        assert p["invalidation"] and p["pattern"] in BRANDT_KINDS and p["verdict"]["profile"] == "brandt"
    assert brandt_classical_patterns(series_from_closes(uptrend_closes(10)))["state"] == "INSUFFICIENT_DATA"


def _rectangle_break(volume_spike=True):
    pts = [(0, 100), (6, 120), (12, 100), (18, 120), (24, 100), (30, 120), (36, 100), (42, 120), (48, 100),
           (52, 110), (53, 130), (56, 134)]
    closes = [100.0] * 30
    for (i0, p0), (i1, p1) in zip(pts, pts[1:]):
        closes.extend(p0 + (p1 - p0) * (i - i0) / (i1 - i0) for i in range(i0, i1))
    closes.append(pts[-1][1])
    vols = [1e6] * len(closes)
    if volume_spike:
        vols[30 + 53] = 3e6
    return series_from_closes(closes, volumes=vols)


def test_brandt_completed_rectangle_has_ldr_scenario():
    r = brandt_classical_patterns(_rectangle_break())
    rect = [p for p in r["patterns"] if p["pattern"] == "RECTANGLE" and p["verdict"]["status"] == "COMPLETED"]
    assert r["state"] == "COMPLETED_BREAKS" and len(rect) == 1
    sc = rect[0]["scenario"]
    assert sc["side"] == "long" and sc["stop_rule"]["rule"] == "LAST_DAY_RULE"
    assert sc["stop"] < sc["level"] < sc["target"]
    assert rect[0]["signal_class"] in ("MAJOR_BREAKOUT", "MINOR")
    assert rect[0]["invalidation"].startswith("a close back below")
    assert "buy" not in str(r).lower() and "sell" not in str(r).lower()


def test_edwards_magee_needs_volume_and_reports_pullback_zone():
    r = edwards_magee_trend_and_patterns(_rectangle_break())
    rect = [p for p in r["patterns"] if p["pattern"] == "RECTANGLE"]
    assert rect and rect[0]["verdict"]["status"] == "COMPLETED"
    sc = rect[0]["scenario"]
    lo, hi = sc["pullback_zone_40_50"]
    top = max(c.high for c in _rectangle_break().candles)
    bottom = rect[0]["key_levels"]["lower"]  # the leg runs from the range floor to the high since the break
    assert (lo, hi) == pytest.approx((top - 0.5 * (top - bottom), top - 0.4 * (top - bottom)))
    assert sc["throwback_level"] == sc["level"]
    assert r["primary_trend"]["state"] in ("UP", "DOWN", "MIXED", "UNKNOWN")
    assert any("beta" in u for u in r["unevaluated_criteria"]) and "extension" in r["note"]
    quiet = edwards_magee_trend_and_patterns(_rectangle_break(volume_spike=False))
    q = [p for p in quiet["patterns"] if p["pattern"] == "RECTANGLE"]
    assert q and "UNCONFIRMED_VOLUME" in q[0]["verdict"]["unmet"]
    with pytest.raises(ValueError):
        edwards_magee_trend_and_patterns(series_from_closes(uptrend_closes(100), "1w"))


def test_brandt_signal_class_is_fixed_at_the_signal_bar():
    # Breaker round 2: the class used a weekly bar that closes after the daily signal (look-ahead).
    from my_ai_trading.domain import CandleSeries
    full = _rectangle_break()
    r = brandt_classical_patterns(full)
    rect = [p for p in r["patterns"] if p["pattern"] == "RECTANGLE" and p["verdict"]["status"] == "COMPLETED"][0]
    assert rect["weekly_close_confirmed"] in (True, False, "PENDING")
    k = next(i for i, c in enumerate(full.candles) if c.open_time_ms >= rect["quality"]["signal_bar_open_ms"])
    early = brandt_classical_patterns(CandleSeries(full.instrument, full.timeframe, full.candles[:k + 1]))
    first = [p for p in early["patterns"] if p["pattern"] == "RECTANGLE" and p["verdict"]["status"] == "COMPLETED"][0]
    assert first["signal_class"] == rect["signal_class"]


def test_edwards_magee_pullback_leg_starts_at_a_real_swing():
    closes = [100.0] * 30
    pts = [(0, 100), (6, 130), (12, 104), (18, 124), (24, 110), (28, 118), (31, 112), (34, 135), (40, 138)]
    for (i0, p0), (i1, p1) in zip(pts, pts[1:]):
        closes.extend(p0 + (p1 - p0) * (i - i0) / (i1 - i0) for i in range(i0, i1))
    closes.append(pts[-1][1])
    r = edwards_magee_trend_and_patterns(series_from_closes(closes, volumes=[1e6] * 63 + [4e6] + [1e6] * (len(closes) - 64)))
    for p in r["patterns"]:
        sc = p.get("scenario")
        if sc:
            lo, hi = sc["pullback_zone_40_50"]
            assert lo > min(closes[30:]) * 0.99  # never below a price the leg did not trade
