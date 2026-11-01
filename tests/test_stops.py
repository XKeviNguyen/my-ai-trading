import pytest

from my_ai_trading.fixtures import candles_from_ohlc
from my_ai_trading.stops import basing_points, last_day_rule, trailing_three_day

# Pattern top at 100 for the LDR fixtures.
INSIDE = [(96, 99, 95, 98), (98, 99.5, 96, 97)]


def test_ldr_normal_breakout_uses_breakout_bar_low():
    c = candles_from_ohlc(INSIDE + [(98, 102, 97, 101)])  # 2 of 5 range points beyond 100
    s = last_day_rule(c, 2, "long", 100)
    assert (s.price, s.case, s.source_bar_open_ms) == (97, "BREAKOUT_BAR", c[2].open_time_ms)


def test_ldr_bar_mostly_outside_uses_prior_bar_low():
    c = candles_from_ohlc(INSIDE + [(99.5, 106, 99, 105)])  # 6 of 7 range points beyond 100
    s = last_day_rule(c, 2, "long", 100)
    assert (s.price, s.case) == (96, "PRIOR_BAR_MOSTLY_OUTSIDE")


def test_ldr_gap_breakout_uses_close_before_gap():
    c = candles_from_ohlc(INSIDE + [(101, 104, 100.5, 103)])  # low 100.5 > prior high 99.5
    s = last_day_rule(c, 2, "long", 100)
    assert (s.price, s.case) == (97, "GAP_CLOSE_BEFORE_GAP")


def test_ldr_short_mirrors():
    c = candles_from_ohlc([(104, 105, 101, 102), (102, 104, 100.5, 103), (102, 103, 98, 99)])
    s = last_day_rule(c, 2, "short", 100)  # 2 of 5 range points below 100
    assert (s.price, s.case) == (103, "BREAKOUT_BAR")


def test_ldr_rejects_bar_that_did_not_close_beyond():
    c = candles_from_ohlc(INSIDE + [(98, 102, 97, 99.9)])
    with pytest.raises(ValueError):
        last_day_rule(c, 2, "long", 100)
    with pytest.raises(ValueError):
        last_day_rule(c, 5, "long", 100)


def test_trailing_three_day_peak_setup_trigger():
    c = candles_from_ohlc([
        (100, 101, 99, 100.5),   # 0 entry
        (100.5, 104, 100, 103),  # 1 peak (high 104, low 100)
        (103, 103.5, 99, 99.5),  # 2 setup: close 99.5 < peak low 100; trigger = low 99
        (99.5, 101, 99.2, 100),  # 3 holds above 99
    ])
    r = trailing_three_day(c, 0, "long")
    assert r["state"] == "ARMED" and r["trigger"]["price"] == 99
    assert r["peak_bar_open_ms"] == c[1].open_time_ms
    hit = trailing_three_day(c + candles_from_ohlc([(100, 100.5, 98.5, 99)], start_ms=c[-1].close_time_ms + 1),
                             0, "long")
    assert hit["state"] == "TRIGGERED" and hit["triggered_bar_index"] == 4


def test_trailing_new_peak_resets_setup():
    c = candles_from_ohlc([(100, 101, 99, 100.5), (100.5, 104, 100, 103), (103, 103.5, 99, 99.5),
                           (99.5, 105, 99.5, 104.5)])  # new high before the trigger breaks
    r = trailing_three_day(c, 0, "long")
    assert r["state"] == "TRACKING_PEAK" and r["peak_bar_open_ms"] == c[3].open_time_ms


def test_trailing_short_mirrors():
    c = candles_from_ohlc([(100, 101, 99, 99.5), (99.5, 100, 96, 97), (97, 101, 96.5, 100.5),
                           (100.5, 101.5, 100, 101)])
    r = trailing_three_day(c, 0, "short")
    assert r["state"] == "TRIGGERED" and r["trigger"]["price"] == 101 and r["triggered_bar_index"] == 3


def test_basing_point_three_bars_away():
    c = candles_from_ohlc([
        (100, 110, 99, 109),     # 0 top
        (109, 109.5, 103, 104),  # 1
        (104, 105, 101, 102),    # 2 candidate low 101, high 105
        (106, 108, 105.5, 107),  # 3 away 1
        (107, 109, 106, 108),    # 4 away 2
        (108, 109.8, 107, 109),  # 5 away 3 -> basing point 101
    ])
    bp = basing_points(c, "long")
    assert [(b.price, b.source_bar_open_ms) for b in bp] == [(101, c[2].open_time_ms)]


def test_basing_point_resets_on_lower_low():
    c = candles_from_ohlc([(100, 110, 99, 109), (104, 105, 101, 102), (106, 108, 105.5, 107),
                           (104, 106, 100, 101), (101, 102, 100.5, 101.5)])  # lower low before 3 bars
    assert basing_points(c, "long") == []


def test_basing_point_counts_away_bars_that_make_new_highs():
    # Breaker repro: a strong rally away from the low made new highs and the basing point was lost.
    c = candles_from_ohlc([(100, 110, 99, 109), (109, 109.5, 104, 105), (105, 106, 101, 102), (102, 103, 100, 102.5),
                           (103.5, 107, 103.2, 106), (106, 112, 105, 111), (111, 115, 110, 114),
                           (114, 116, 113, 115)])
    assert [b.price for b in basing_points(c, "long")] == [100]


def test_ldr_bar_opening_at_the_level_uses_prior_bar():
    c = candles_from_ohlc([(96, 99, 95, 98), (98, 101, 95, 99), (100.5, 104, 96, 103)])
    s = last_day_rule(c, 2, "long", 100)
    assert (s.price, s.case) == (95, "PRIOR_BAR_OPENED_AT_LEVEL")


def test_trailing_new_peak_drops_the_old_trigger():
    c = candles_from_ohlc([(100, 101, 99, 100.5), (100.5, 105, 100, 104), (104, 104.5, 98, 99.5),
                           (102, 110, 101.5, 109), (109, 109.8, 97.5, 109.5)])
    r = trailing_three_day(c, 0, "long")
    assert r["state"] == "TRACKING_PEAK" and r["trigger"] is None


def test_basing_point_survives_a_new_high_that_overlaps_the_low_bar():
    # Breaker round 2: a new high that still overlapped the low bar's range cancelled the candidate.
    c = candles_from_ohlc([(100, 110, 99, 109), (109, 109.5, 104, 105), (104, 105, 101, 102),
                           (104.6, 111, 104.5, 110), (110, 112, 106, 111), (111, 113, 107, 112), (112, 114, 108, 113),
                           (113, 115, 109, 114)])
    assert [b.price for b in basing_points(c, "long")] == [101]


def test_basing_points_only_tighten_and_short_mirrors():
    c = candles_from_ohlc([(100, 101, 90, 91), (91, 96, 90.5, 95), (95, 99, 94, 98), (98, 98.5, 92, 93),
                           (93, 93.5, 89, 90), (90, 91, 88, 89), (89, 90, 87, 88), (88, 89, 86, 87)])
    bps = [b.price for b in basing_points(c, "short")]
    assert bps and all(b2 < b1 for b1, b2 in zip(bps, bps[1:]))


def test_a_higher_reaction_low_inside_the_counting_window_becomes_the_next_basing_point():
    c = candles_from_ohlc([(100, 110, 99, 109), (109, 109.5, 104, 105), (104, 105, 101, 102),
                           (106, 112, 105.5, 111), (111, 111.5, 107, 108), (108, 113, 108, 112),
                           (113, 116, 112, 115), (115, 117, 113, 116), (116, 118, 114, 117)])
    assert [b.price for b in basing_points(c, "long")] == [101, 107]
