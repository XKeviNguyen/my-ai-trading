import random
import time

import pytest
from hypothesis import given, settings, strategies as st

from my_ai_trading.domain import Candle, CandleSeries, Timeframe
from my_ai_trading.fixtures import DEMO_INSTRUMENT, candles_from_closes, series_from_closes
from my_ai_trading.patterns import (NOTE, PATTERN_KINDS, SWING_RIGHT, detect_candles, detect_patterns,
                                    swing_points)

DAY = 86_400_000
FLAT = [100.0] * 30  # warm-up so ATR exists before the shape starts
KEYS = {"pattern", "state", "start_bar_open_ms", "end_bar_open_ms", "key_levels", "conditions_met",
        "parameters", "invalidation", "rule_version", "note"}
STATES = {"FORMING", "BREAKOUT_CONFIRMED", "BREAKDOWN_CONFIRMED", "FAILED"}


def path(points):
    """Piecewise-linear closes through (bar, price) waypoints."""
    out = []
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        out.extend(p0 + (p1 - p0) * (i - i0) / (i1 - i0) for i in range(i0, i1))
    out.append(points[-1][1])
    return FLAT + out


def of(kind, closes, **kw):
    return [d for d in detect_patterns(series_from_closes(closes, **kw)) if d["pattern"] == kind]


def random_walk(n, seed):
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
    return closes, [rng.uniform(5e5, 2e6) for _ in closes]


SHAPES = {
    "HEAD_AND_SHOULDERS": (path([(0, 100), (8, 110), (16, 100), (26, 120), (36, 100), (44, 110), (52, 100),
                                 (60, 90)]), {}, "BREAKDOWN_CONFIRMED"),
    "INVERSE_HEAD_AND_SHOULDERS": (path([(0, 100), (8, 90), (16, 100), (26, 80), (36, 100), (44, 90),
                                         (52, 100), (60, 110)]), {}, "BREAKOUT_CONFIRMED"),
    "VCP": (path([(0, 100), (10, 130), (20, 104), (30, 129), (38, 116), (46, 128), (52, 122), (58, 128.5),
                  (64, 135)]), {"volumes": [1e6] * 76 + [5e5] * 19}, "BREAKOUT_CONFIRMED"),
    "CUP_AND_HANDLE": (path([(0, 100), (5, 130), (15, 105), (25, 100), (35, 104), (45, 128), (50, 122),
                             (54, 125), (58, 136)]), {}, "BREAKOUT_CONFIRMED"),
    "BULL_FLAG": (path([(0, 100), (10, 100), (18, 140), (24, 134), (27, 136), (32, 145)]), {},
                  "BREAKOUT_CONFIRMED"),
    "BEAR_FLAG": (path([(0, 140), (10, 140), (18, 100), (24, 106), (27, 104), (32, 95)]), {},
                  "BREAKDOWN_CONFIRMED"),
    "ASCENDING_TRIANGLE": (path([(0, 100), (6, 120), (12, 105), (18, 120), (24, 110), (30, 120), (33, 116),
                                 (38, 126)]), {}, "BREAKOUT_CONFIRMED"),
    "DESCENDING_TRIANGLE": (path([(0, 100), (6, 80), (12, 95), (18, 80), (24, 90), (30, 80), (33, 84),
                                  (38, 74)]), {}, "BREAKDOWN_CONFIRMED"),
    "SYMMETRICAL_TRIANGLE": (path([(0, 100), (6, 130), (12, 104), (18, 124), (24, 110), (28, 118),
                                   (34, 108)]), {}, "BREAKDOWN_CONFIRMED"),
    "RISING_WEDGE": (path([(0, 100), (6, 110), (12, 106), (18, 118), (24, 116), (30, 124), (34, 122),
                           (40, 112)]), {}, "BREAKDOWN_CONFIRMED"),
    "FALLING_WEDGE": (path([(0, 130), (6, 120), (12, 124), (18, 112), (24, 114), (30, 106), (34, 108),
                            (40, 118)]), {}, "BREAKOUT_CONFIRMED"),
}


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_synthetic_shape_is_detected_with_explicit_evidence(kind):
    closes, kw, state = SHAPES[kind]
    found = of(kind, closes, **kw)
    assert [d["state"] for d in found] == [state]
    d = found[0]
    assert set(d) == KEYS
    assert d["note"] == NOTE and d["rule_version"] == f"{kind.lower()}/1"
    assert d["start_bar_open_ms"] < d["end_bar_open_ms"]
    assert d["conditions_met"]["resolution_bar_open_ms"] <= d["end_bar_open_ms"]
    assert d["conditions_met"]["structure_confirmed_bar_open_ms"] <= d["end_bar_open_ms"]
    assert d["parameters"] and d["key_levels"] and d["invalidation"]


def test_vcp_is_labeled_as_a_generic_approximation_and_needs_volume_decline():
    closes, kw, _ = SHAPES["VCP"]
    d = of("VCP", closes, **kw)[0]
    assert "not a Minervini VCP" in d["conditions_met"]["definition"]
    assert d["conditions_met"]["depths"] == sorted(d["conditions_met"]["depths"], reverse=True)
    assert d["key_levels"]["pivot"] == pytest.approx(128 * 1.01)  # high of the last contraction
    assert of("VCP", closes) == []  # constant volume: no decline in the last contraction


def test_head_and_shoulders_levels():
    d = of("HEAD_AND_SHOULDERS", SHAPES["HEAD_AND_SHOULDERS"][0])[0]
    assert d["key_levels"]["head"] == pytest.approx(120 * 1.01)
    assert d["key_levels"]["neckline_point_1"] == pytest.approx(100 * 0.99)
    assert d["key_levels"]["neckline_at_end"] == pytest.approx(99.0)


def _bar(i, o, h, lo, c, v=1.0):
    return Candle(i * DAY, (i + 1) * DAY - 1, o, h, lo, c, v)


def test_breakout_needs_a_close_not_an_intrabar_wick():
    closes = SHAPES["ASCENDING_TRIANGLE"][0][:63]  # stop before the breakout leg
    cs = candles_from_closes(closes, start_ms=0)
    assert [d["state"] for d in detect_candles(cs, kinds={"ASCENDING_TRIANGLE"})] == ["FORMING"]
    last = cs[-1]
    wick = _bar(len(cs), last.close, 150.0, last.close * 0.99, last.close)  # high far above, close inside
    found = detect_candles(cs + [wick], kinds={"ASCENDING_TRIANGLE"})
    assert [d["state"] for d in found] == ["FORMING"]
    close_above = _bar(len(cs), last.close, 130.0, last.close * 0.99, 125.0)
    found = detect_candles(cs + [close_above], kinds={"ASCENDING_TRIANGLE"})
    assert [d["state"] for d in found] == ["BREAKOUT_CONFIRMED"]
    assert found[0]["end_bar_open_ms"] == close_above.open_time_ms


def _zone_candles(demand=True):
    """15 bars of range 2 (ATR ~2), a 2-bar tight base, an impulse of range 6, drift, then a return."""
    s = 1 if demand else -1
    cs = [_bar(i, 100, 101, 99, 100) for i in range(16)]
    cs += [_bar(16, 100, 100.3, 99.9, 100.1), _bar(17, 100.1, 100.3, 99.9, 100.0)]
    cs.append(_bar(18, 100, 106, 100, 105.5) if demand else _bar(18, 100, 100, 94, 94.5))
    price = 100 + s * 6
    for i in range(19, 25):
        cs.append(_bar(i, price, price + 1, price - 1, price))
    # return into the zone with a wick, close stays on the zone's near side
    cs.append(_bar(25, price, max(price, 100.2), min(price, 100.2), 100 + s * 0.5) if demand
              else _bar(25, price, max(price, 100.0), min(price, 100.0), 99.5))
    return cs


@pytest.mark.parametrize("demand", [True, False])
def test_supply_and_demand_zones(demand):
    kind = "DEMAND_ZONE" if demand else "SUPPLY_ZONE"
    cs = _zone_candles(demand)
    found = detect_candles(cs, kinds={kind})
    assert len(found) == 1
    d = found[0]
    assert d["state"] == "FORMING" and d["key_levels"]["returned"] is True
    assert d["key_levels"]["zone_high"] == pytest.approx(100.3)
    assert d["key_levels"]["zone_low"] == pytest.approx(99.9)
    assert d["conditions_met"]["base_bars"] == 2 and d["conditions_met"]["impulse_range_atr"] >= 2
    assert d["start_bar_open_ms"] == 16 * DAY
    # untouched zone before the return bar
    assert detect_candles(cs[:-1], kinds={kind})[0]["key_levels"]["returned"] is False
    # a close through the far side breaks the zone
    breaker = _bar(26, 100, 101, 98, 99.0) if demand else _bar(26, 100, 102, 99, 101.0)
    broken = detect_candles(cs + [breaker], kinds={kind})[0]
    assert broken["state"] == "FAILED" and broken["end_bar_open_ms"] == 26 * DAY


def test_swing_points_never_confirm_the_newest_bars():
    closes = path([(0, 100), (10, 130), (14, 120)])  # last bar region is a fresh low/high candidate
    cs = candles_from_closes(closes)
    sw = swing_points(cs)
    assert all(s.index <= len(cs) - 1 - SWING_RIGHT for s in sw)
    # Even an extreme newest bar is not a confirmed swing until SWING_RIGHT more bars close.
    spike = candles_from_closes(closes + [200.0])
    assert all(s.index < len(spike) - SWING_RIGHT for s in swing_points(spike))
    assert len(cs) - 1 not in {s.index for s in swing_points(spike)}
    with pytest.raises(ValueError):
        swing_points(cs, left=0)


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=30, max_value=299))
@settings(max_examples=60, deadline=None)
def test_swing_points_of_a_prefix_match_the_full_history(seed, k):
    closes, _ = random_walk(300, seed)
    cs = candles_from_closes(closes)
    full = [s for s in swing_points(cs) if s.index + SWING_RIGHT <= k]
    assert swing_points(cs[: k + 1]) == full


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=40, max_value=398))
@settings(max_examples=80, deadline=None)
def test_no_look_ahead_resolved_detections_are_stable(seed, k):
    """Detections ending at or before bar k are identical with or without later bars.

    FORMING detections are "as of" the newest bar by definition (their end bar is the newest bar),
    so in the longer history none can end at or before k; in the prefix they are excluded.
    """
    closes, vols = random_walk(400, seed)
    cs = candles_from_closes(closes, volumes=vols)
    cutoff = cs[k].open_time_ms
    full = [d for d in detect_candles(cs) if d["end_bar_open_ms"] <= cutoff]
    prefix = [d for d in detect_candles(cs[: k + 1]) if d["state"] != "FORMING"]
    assert all(d["state"] != "FORMING" for d in full)
    assert prefix == full


def test_no_look_ahead_on_every_prefix_of_each_shape():
    for closes, kw, _ in SHAPES.values():
        cs = candles_from_closes(closes, **kw)
        full = detect_candles(cs)
        for k in range(len(cs)):
            cutoff = cs[k].open_time_ms
            want = [d for d in full if d["end_bar_open_ms"] <= cutoff and d["state"] != "FORMING"]
            got = [d for d in detect_candles(cs[: k + 1]) if d["state"] != "FORMING"]
            assert got == want


def test_negative_controls():
    assert detect_patterns(series_from_closes([100.0] * 300)) == []
    assert detect_patterns(series_from_closes([100.0 + 0.01 * i for i in range(300)])) == []
    assert detect_patterns(series_from_closes([100.0])) == []
    total = 0
    for seed in range(5):
        closes, vols = random_walk(1000, seed)
        total += len(detect_patterns(series_from_closes(closes, volumes=vols)))
    # Geometric detectors do fire on random walks; this pins the measured base rate as an upper bound.
    assert total / 5000 < 0.10


def test_kinds_filter_and_unknown_kind():
    closes, kw, _ = SHAPES["HEAD_AND_SHOULDERS"]
    s = series_from_closes(closes, **kw)
    only = detect_patterns(s, kinds={"HEAD_AND_SHOULDERS"})
    assert only and {d["pattern"] for d in only} == {"HEAD_AND_SHOULDERS"}
    assert detect_patterns(s, kinds=set()) == []
    with pytest.raises(ValueError):
        detect_patterns(s, kinds={"DOUBLE_TOP"})
    assert len(PATTERN_KINDS) == 13


def test_detections_never_span_a_gap():
    closes, kw, _ = SHAPES["HEAD_AND_SHOULDERS"]
    cs = candles_from_closes(closes, start_ms=0)
    gap_at = 60  # inside the pattern: drop one bar
    gapped = CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), tuple(cs[:gap_at] + cs[gap_at + 1:]))
    assert gapped.gaps
    found = detect_patterns(gapped)
    assert all(d["start_bar_open_ms"] >= cs[gap_at + 1].open_time_ms for d in found)
    assert not [d for d in found if d["pattern"] == "HEAD_AND_SHOULDERS"]


def test_states_and_schema_on_random_walk():
    closes, vols = random_walk(1500, 7)
    for d in detect_patterns(series_from_closes(closes, volumes=vols)):
        assert set(d) == KEYS and d["state"] in STATES and d["pattern"] in PATTERN_KINDS
        assert d["start_bar_open_ms"] <= d["end_bar_open_ms"]


def test_runtime_bound_1500_bars():
    closes, vols = random_walk(1500, 3)
    s = series_from_closes(closes, volumes=vols)
    detect_patterns(s)  # warm up
    best = min(_timed(lambda: detect_patterns(s)) for _ in range(5))
    assert best < 0.5  # generous CI bound; target is < 50 ms on the development machine


def _timed(fn):
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t
