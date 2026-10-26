from hypothesis import given, settings, strategies as st

from my_ai_trading.domain import CandleSeries, Timeframe
from my_ai_trading.engine import analyze
from my_ai_trading.fixtures import (DEMO_INSTRUMENT, candles_from_closes, downtrend_closes,
                                    series_from_closes, uptrend_closes)


def _now(s):
    return s.candles[-1].close_time_ms + 1


def _types(report):
    return {e["type"] for e in report["events"]}


def test_breakout_requires_close_above_prior_high_and_volume():
    closes = [1.0] * 30 + [1.2]
    vols = [100.0] * 30 + [300.0]
    s = series_from_closes(closes, volumes=vols)
    r = analyze(s, as_of_ms=_now(s))
    assert r["status"] == "OK"
    ev = next(e for e in r["events"] if e["type"] == "BREAKOUT")
    assert ev["conditions_met"]["volume_ratio_20"] == 3.0
    assert "back below" in ev["invalidation"]
    # Same price move without volume: no breakout.
    s2 = series_from_closes(closes, volumes=[100.0] * 31)
    assert "BREAKOUT" not in _types(analyze(s2, as_of_ms=_now(s2)))


def test_breakdown_and_failed_breakout():
    s = series_from_closes([1.0] * 30 + [0.8], volumes=[100.0] * 30 + [300.0])
    assert "BREAKDOWN" in _types(analyze(s, as_of_ms=_now(s)))
    s = series_from_closes([1.0] * 30 + [1.2, 1.0], volumes=[100.0] * 30 + [300.0, 100.0])
    assert "FAILED_BREAKOUT" in _types(analyze(s, as_of_ms=_now(s)))


def test_unusual_volume_and_ma_reclaim():
    closes = [2.0 - 0.01 * i for i in range(60)] + [2.0]
    s = series_from_closes(closes, volumes=[100.0] * 60 + [500.0])
    t = _types(analyze(s, as_of_ms=_now(s)))
    assert {"UNUSUAL_VOLUME", "SMA25_RECLAIM", "SMA50_RECLAIM"} <= t


def test_volatility_contraction():
    wide = [1.0 + (0.05 if i % 2 else -0.05) for i in range(60)]
    tight = [1.0 + (0.002 if i % 2 else -0.002) for i in range(10)]
    s = series_from_closes(wide + tight, spread=0.0)
    assert "VOLATILITY_CONTRACTION" in _types(analyze(s, as_of_ms=_now(s)))


def test_trend_states():
    up = series_from_closes(uptrend_closes())
    assert analyze(up, as_of_ms=_now(up))["trend"]["state"] == "UP"
    down = series_from_closes(downtrend_closes())
    assert analyze(down, as_of_ms=_now(down))["trend"]["state"] == "DOWN"
    short = series_from_closes(uptrend_closes(100))
    assert analyze(short, as_of_ms=_now(short))["trend"]["state"] == "UNKNOWN"


def test_stale_data_suppresses_events():
    s = series_from_closes([1.0] * 30 + [1.2], volumes=[100.0] * 30 + [300.0])
    r = analyze(s, as_of_ms=_now(s) + 3 * 86_400_000)
    assert r["status"] == "STALE_DATA" and r["events"] == []
    assert any("STALE_DATA" in m for m in r["missing_inputs"])


def test_gap_restricts_indicators_to_contiguous_tail():
    cs = candles_from_closes(uptrend_closes(300))
    s = CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), tuple(cs[:200] + cs[205:]))
    r = analyze(s, as_of_ms=_now(s))
    assert r["data_quality"]["missing_bars"] == 5
    assert r["data_quality"]["contiguous_bars"] == 95
    assert r["indicator_values"]["sma200"] is None
    assert any("missing bar" in m for m in r["missing_inputs"])


def test_empty_and_short():
    empty = CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), ())
    assert analyze(empty, as_of_ms=0)["status"] == "INSUFFICIENT_DATA"
    s = series_from_closes([1.0] * 10)
    assert analyze(s, as_of_ms=_now(s))["status"] == "INSUFFICIENT_DATA"


def test_no_preset_is_applied_by_the_engine():
    s = series_from_closes(uptrend_closes())
    r = analyze(s, as_of_ms=_now(s))
    assert not any("minervini" in k.lower() for k in r)


@given(st.integers(min_value=40, max_value=120), st.lists(st.floats(min_value=0.5, max_value=2.0),
                                                           min_size=1, max_size=20))
@settings(max_examples=60, deadline=None)
def test_no_lookahead_future_bars_do_not_change_past_report(cut, future):
    """Report as of bar t is identical whether or not later bars exist in the series."""
    closes = uptrend_closes(cut) + [c * uptrend_closes(cut)[-1] for c in future]
    full = series_from_closes(closes)
    as_of = full.candles[cut - 1].close_time_ms + 1
    past_only = series_from_closes(closes[:cut])
    assert analyze(full.up_to(as_of), as_of_ms=as_of) == analyze(past_only, as_of_ms=as_of)
