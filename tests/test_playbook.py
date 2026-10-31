from my_ai_trading.domain import CandleSeries
from my_ai_trading.fixtures import candles_from_closes, series_from_closes
from my_ai_trading.patterns import detect_candles
from my_ai_trading.playbook import (PROFILES, assess_detections, breakout_quality, pattern_signals, verdict,
                                    weekly_from_daily, swing_trend)

FLAT = [100.0] * 30
BP, EM = PROFILES["brandt"], PROFILES["edwards_magee"]


def path(points, flat=FLAT):
    out = []
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        out.extend(p0 + (p1 - p0) * (i - i0) / (i1 - i0) for i in range(i0, i1))
    out.append(points[-1][1])
    return list(flat) + out


RANGE = [(0, 100), (6, 120), (12, 100), (18, 120), (24, 100), (30, 120), (36, 100), (42, 120), (48, 100),
         (52, 110)]


def one(kind, closes, volumes=None):
    cs = candles_from_closes(closes, volumes=volumes)
    found = [d for d in detect_candles(cs) if d["pattern"] == kind and d["state"] != "FORMING"]
    assert len(found) == 1
    return found[0], cs


def test_slow_rectangle_break_that_stalls_below_3pct_meets_brandt_not_edwards_magee():
    d, cs = one("RECTANGLE", path(RANGE + [(56, 123)]) + [123.0] * 8)
    bq, eq = breakout_quality(d, cs, BP), breakout_quality(d, cs, EM)
    assert (bq["direction"], bq["boundary_kind"], bq["beyond_defining_swing"]) == ("up", "HORIZONTAL", True)
    assert bq["duration_weeks"] >= 6 and bq["completion"] == "MET"
    assert verdict(d, bq, BP)["status"] == "COMPLETED"
    assert eq["completion"] == "NOT_MET" and 0 < eq["close_beyond_pct"] < 0.03
    assert verdict(d, eq, EM)["unmet"] == ["NOT_COMPLETED", "UNCONFIRMED_VOLUME"]


def test_edwards_magee_completes_on_a_later_close_past_3pct():
    # Breaker finding: only the first close beyond the line was judged. 122 (+0.7%) then 126 (+4%).
    closes = path(RANGE + [(53, 122), (54, 126)]) + [127.0] * 5
    vols = [1e6] * len(closes)
    vols[30 + 54] = 3e6
    d, cs = one("RECTANGLE", closes, vols)
    q = breakout_quality(d, cs, EM)
    assert q["completion"] == "MET" and q["signal_bar_open_ms"] == cs[30 + 54].open_time_ms
    assert q["breakout_bar_open_ms"] == cs[30 + 53].open_time_ms
    assert verdict(d, q, EM)["status"] == "COMPLETED"


def test_close_back_inside_ends_the_completion_search():
    closes = path(RANGE + [(53, 122), (54, 118), (55, 130)]) + [130.0] * 5
    d, cs = one("RECTANGLE", closes)
    q = breakout_quality(d, cs, EM)
    assert q["completion"] == "BACK_INSIDE" and verdict(d, q, EM)["status"] == "NOT_COMPLETED"


def test_short_diagonal_triangle_is_too_short_for_brandt_standalone():
    closes = path([(0, 100), (6, 130), (12, 104), (18, 124), (24, 110), (28, 118), (34, 108)])
    d, cs = one("SYMMETRICAL_TRIANGLE", closes)
    q = breakout_quality(d, cs, BP)
    assert (q["direction"], q["boundary_kind"]) == ("down", "DIAGONAL")
    assert q["apex_fraction"] is not None and 0 < q["apex_fraction"] < 1
    assert "TOO_SHORT" in verdict(d, q, BP)["unmet"]
    assert "UNCONFIRMED_VOLUME" not in verdict(d, breakout_quality(d, cs, EM), EM)["unmet"]


def test_signal_never_precedes_structure_confirmation():
    # Breaker repro: a crash bar resolved the double top two bars before its second top was a confirmed swing.
    closes = path([(0, 100), (20, 120), (45, 100), (70, 119)], flat=[]) + [90.0] + [90.0] * 10
    s = series_from_closes(closes)
    for profile in ("brandt", "edwards_magee"):
        full, _ = pattern_signals(s, profile, "short")
        for k in range(60, len(closes)):
            cut = CandleSeries(s.instrument, s.timeframe, s.candles[:k + 1])
            part, _ = pattern_signals(cut, profile, "short")
            assert part == full[:k + 1], (profile, k)


def test_assess_skips_zones_and_marks_forming():
    cs = candles_from_closes(path(RANGE))
    out = assess_detections(detect_candles(cs), cs, BP)
    assert all(x["pattern"] not in ("DEMAND_ZONE", "SUPPLY_ZONE") for x in out)
    rect = [x for x in out if x["pattern"] == "RECTANGLE"]
    assert rect and all(x["verdict"]["status"] == "FORMING" for x in rect)


def test_quality_uses_no_bars_after_the_signal_bar():
    cs = candles_from_closes(path(RANGE + [(58, 126)]))
    d = [x for x in detect_candles(cs) if x["pattern"] == "RECTANGLE"][0]
    longer = cs + candles_from_closes([126.0, 60.0, 300.0], start_ms=cs[-1].close_time_ms + 1)
    assert breakout_quality(d, cs, BP) == breakout_quality(d, longer, BP)


def test_wilson_interval_known_values():
    from my_ai_trading.playbook import wilson_interval
    lo, hi = wilson_interval(5, 10)
    assert round(lo, 3) == 0.237 and round(hi, 3) == 0.763
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 5)[0] == 0.0 and wilson_interval(5, 5)[1] == 1.0


def test_double_top_two_month_rule_boundary_per_profile():
    # Tops 61 bars apart (8.7 weeks) pass Brandt's ">= 2 months"; 58 bars (8.3 weeks) do not. EM needs > 1 month.
    for gap, bp_ok in ((61, True), (58, False)):
        d, cs = one("DOUBLE_TOP", path([(0, 100), (30, 120), (30 + gap // 2, 100), (30 + gap, 120),
                                        (40 + gap, 95)]) + [95.0] * 8)
        bp = verdict(d, breakout_quality(d, cs, BP), BP)
        em = verdict(d, breakout_quality(d, cs, EM), EM)
        assert ("EXTREMES_TOO_CLOSE" not in bp["unmet"]) is bp_ok, gap
        assert "EXTREMES_TOO_CLOSE" not in em["unmet"]
        assert "TOO_SHALLOW" not in bp["unmet"]  # 120 -> 99 is 17.5% deep


def test_double_bottom_needs_six_weeks_for_edwards_magee():
    d, cs = one("DOUBLE_BOTTOM", path([(0, 100), (10, 80), (30, 98), (50, 80), (60, 102)]) + [102.0] * 8)
    v = verdict(d, breakout_quality(d, cs, EM), EM)
    assert "EXTREMES_TOO_CLOSE" in v["unmet"]  # 40 bars = 5.7 weeks < 6
    assert v["unmet_labels"][v["unmet"].index("EXTREMES_TOO_CLOSE")] == "Tops or bottoms too close in time"


def test_shallow_double_top_is_too_shallow():
    d, cs = one("DOUBLE_TOP", path([(0, 100), (30, 120), (61, 106), (92, 120), (102, 100)]) + [100.0] * 8)
    assert "TOO_SHALLOW" in verdict(d, breakout_quality(d, cs, BP), BP)["unmet"]  # 12% deep


def test_failed_pattern_is_pattern_failure_whatever_else_holds():
    d, cs = one("DOUBLE_TOP", path([(0, 100), (30, 120), (60, 100), (90, 120), (95, 112), (100, 130)]) + [131.0] * 6)
    assert d["state"] == "FAILED"
    for cfg in (BP, EM):
        v = verdict(d, breakout_quality(d, cs, cfg), cfg)
        assert v["status"] == "PATTERN_FAILURE" and v["pattern_failure"] is True
        assert v["label"] == "Pattern failed (broke the other way)"


def test_edwards_magee_volume_may_come_on_the_breakout_bar():
    # Breaker round 2: 3x volume on the first close beyond the line, completion two bars later on normal volume.
    closes = path(RANGE + [(53, 122), (54, 126)]) + [127.0] * 5
    vols = [1e6] * len(closes)
    vols[30 + 53] = 3e6
    d, cs = one("RECTANGLE", closes, vols)
    q = breakout_quality(d, cs, EM)
    assert q["completion"] == "MET" and q["volume_ratio"] == 3.0
    assert verdict(d, q, EM)["status"] == "COMPLETED"


def test_reversal_patterns_need_eight_weeks_for_brandt():
    closes = path([(0, 100), (8, 110), (16, 100), (26, 120), (36, 100), (44, 110), (52, 100), (60, 90)]) + [90.0] * 6
    d, cs = one("HEAD_AND_SHOULDERS", closes)
    q = breakout_quality(d, cs, BP)
    assert q["duration_weeks"] < 8 and "TOO_SHORT" in verdict(d, q, BP)["unmet"]


def test_track_record_counts_stop_first_target_first_and_unresolved():
    from my_ai_trading.playbook import TRACK_MAX_BARS, track_record
    # Same completed upside rectangle break, three futures: target first, stop first, neither.
    def series(tail_points):
        closes = path(RANGE + [(53, 130)] + tail_points)
        v = [1e6] * len(closes)
        v[30 + 53] = 3e6
        return series_from_closes(closes, volumes=v)
    win = track_record(series([(70, 150)]), "brandt")
    lose = track_record(series([(56, 131), (60, 90)]), "brandt")
    flat = track_record(series([(54, 131), (54 + TRACK_MAX_BARS + 5, 131.5)]), "brandt")
    assert (win["target_first"], win["stop_first"]) == (1, 0)
    assert (lose["target_first"], lose["stop_first"]) == (0, 1)
    assert flat["resolved"] == 0 and flat["unresolved_or_recent"] == 1 and flat["target_first_rate"] is None


def test_no_signal_when_the_measured_move_is_used_up_at_the_signal_close():
    # Verifier: a target already reached by the signal close used to leave a target-less trade behind.
    closes = path(RANGE + [(53, 150)]) + [150.0] * 6  # one bar jumps past the measured target (143.4)
    vols = [1e6] * len(closes)
    vols[30 + 53] = 3e6
    sig, plans = pattern_signals(series_from_closes(closes, volumes=vols), "brandt", "long")
    assert "MATCH" not in sig and not any(plans)


def test_pivot_sensitivity_uses_no_bars_after_the_detection():
    from my_ai_trading.playbook import pivot_sensitivity
    closes = path(RANGE + [(58, 126)])
    cs = candles_from_closes(closes)
    d = [x for x in detect_candles(cs) if x["pattern"] == "RECTANGLE"][0]
    end = next(i for i, c in enumerate(cs) if c.open_time_ms == d["end_bar_open_ms"])
    longer = cs + candles_from_closes([126.0 + i for i in range(30)], start_ms=cs[-1].close_time_ms + 1)
    early = pivot_sensitivity(longer[:end + 5], d)
    assert early["state"] == "PENDING" and early["robust"] is None  # a 4-bar pivot needs bars that have not closed
    done = [pivot_sensitivity(longer[:k], d) for k in range(end + 6, len(longer) + 1, 7)]
    assert all(x == done[0] for x in done) and done[0]["state"] == "DONE" and 3 in done[0]["found_with"]


def test_weekly_resample_aggregates_and_skips_incomplete_weeks():
    cs = candles_from_closes([100.0 + i for i in range(30)], volumes=[float(i + 1) for i in range(30)])
    weeks = weekly_from_daily(cs)  # starts on a Wednesday: the first full week is days 5..11
    w = weeks[0]
    assert (w.open, w.close) == (cs[5].open, cs[11].close)
    assert w.high == max(c.high for c in cs[5:12]) and w.low == min(c.low for c in cs[5:12])
    assert w.volume == sum(c.volume for c in cs[5:12])
    holed = cs[:8] + cs[9:]  # a missing day inside the first week drops that week
    assert weekly_from_daily(holed)[0].open_time_ms == cs[12].open_time_ms


def test_swing_trend_states():
    zig = path([(0, 100), (5, 110), (10, 105), (15, 120), (20, 112), (25, 130), (30, 125), (35, 140)], flat=[])
    assert swing_trend(candles_from_closes(zig))["state"] == "UP"
    broken = zig + [100.0]  # a close below the last swing low before new swings confirm
    assert swing_trend(candles_from_closes(broken))["state"] == "TRANSITION"
    assert swing_trend(candles_from_closes([100.0] * 30))["state"] == "UNKNOWN"


def test_a_clean_rectangle_is_robust_to_pivot_settings():
    # Matching by start bar called this rectangle fragile: 2-bar pivots see more swings, so the detector's
    # 8-swing window starts later. The overlap rule keeps it robust.
    from my_ai_trading.playbook import pivot_sensitivity
    cs = candles_from_closes(path(RANGE + [(53, 130), (60, 134)]))
    d = [x for x in detect_candles(cs) if x["pattern"] == "RECTANGLE" and x["state"] != "FORMING"][0]
    assert pivot_sensitivity(cs, d) == {"pivot_bars_tried": [2, 3, 4], "state": "DONE", "found_with": [2, 3, 4],
                                        "robust": True}
