import pytest

from my_ai_trading.fixtures import downtrend_closes, series_from_closes, uptrend_closes
from my_ai_trading.strategies import (darvas_box, minervini_trend_template, turtle_breakout, weinstein_stage,
                                      williams_volatility_breakout, raschke_holy_grail,
                                      brandt_classical_patterns)


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
    assert r["state"] in ("PATTERNS_PRESENT", "NO_RECENT_PATTERN") and "discretionary" in r["note"]
    for p in r["patterns"]:
        assert p["invalidation"] and p["pattern"] in {"HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS",
                                                      "ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE",
                                                      "SYMMETRICAL_TRIANGLE", "RISING_WEDGE", "FALLING_WEDGE",
                                                      "BULL_FLAG", "BEAR_FLAG"}
    assert brandt_classical_patterns(series_from_closes(uptrend_closes(10)))["state"] == "INSUFFICIENT_DATA"
