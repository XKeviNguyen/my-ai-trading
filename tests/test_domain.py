from dataclasses import replace

import pytest

from my_ai_trading.domain import (DAY_MS, Candle, CandleSeries, InstrumentIdentity, MarketType,
                                  Timeframe)
from my_ai_trading.fixtures import DEMO_INSTRUMENT, candles_from_closes, series_from_closes


def test_timeframe_allowlist_and_durations():
    assert Timeframe("4h").duration_ms == 4 * 3_600_000
    assert Timeframe("1w").duration_ms == 7 * DAY_MS
    for bad in ("1M", "2d", "1H", "", "1h; DROP"):
        with pytest.raises(ValueError):
            Timeframe(bad)


def test_same_ticker_different_market_is_different_instrument():
    perp = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT")
    spot = InstrumentIdentity("binance", MarketType.SPOT, "SANDUSDT")
    assert perp.key != spot.key


@pytest.mark.parametrize("change", [
    {"high": 0.5}, {"close": float("nan")}, {"close": float("inf")}, {"open": -1.0},
    {"volume": -1.0}, {"volume": float("nan")}, {"close_time_ms": 0},
])
def test_candle_rejects_invalid_values(change):
    c = candles_from_closes([1.0, 1.1])[1]
    with pytest.raises(ValueError):
        replace(c, **change)


def test_candle_rejects_float_and_bool_timestamps():
    row = [1.7e12, "1", "1", "1", "1", "1", 1_700_086_399_999, "1", 0, "0", "0", "0"]
    with pytest.raises(ValueError):
        Candle.from_binance(row)
    with pytest.raises(ValueError):
        Candle(True, 5, 1, 1, 1, 1, 1)


def test_bar_is_not_closed_at_its_last_millisecond_and_respects_safety_margin():
    c = candles_from_closes([1.0])[0]
    assert not c.is_closed(c.close_time_ms)
    assert c.is_closed(c.close_time_ms + 1)
    assert not c.is_closed(c.close_time_ms + 1000, safety_ms=1500)


def test_from_candles_drops_forming_bar():
    cs = candles_from_closes([1, 2, 3, 4])
    s = CandleSeries.from_candles(DEMO_INSTRUMENT, "1d", cs, now_ms=cs[-1].close_time_ms - 10)
    assert s.candles == tuple(cs[:-1])


def test_series_rejects_duplicates_disorder_and_wrong_interval_label():
    cs = candles_from_closes([1, 2, 3])
    with pytest.raises(ValueError, match="strictly ordered"):
        CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), (cs[0], cs[0]))
    with pytest.raises(ValueError, match="strictly ordered"):
        CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), (cs[1], cs[0]))
    # Daily bars labeled weekly or hourly must fail (audit H3).
    for label in ("1w", "1h"):
        with pytest.raises(ValueError, match="duration"):
            CandleSeries(DEMO_INSTRUMENT, Timeframe(label), tuple(cs))


def test_gaps_are_recorded_not_filled_and_tail_is_contiguous():
    cs = candles_from_closes(list(range(1, 31)))
    gapped = tuple(cs[:10] + cs[15:])
    s = CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), gapped)
    assert len(s.gaps) == 1 and s.missing_bars() == 5
    assert s.contiguous_tail() == tuple(cs[15:])
    assert len(s.candles) == 25  # nothing invented


def test_staleness():
    s = series_from_closes([1.0] * 5)
    last_close = s.candles[-1].close_time_ms
    assert not s.is_stale(last_close + 1)
    assert not s.is_stale(last_close + DAY_MS)  # next bar only just closed: grace applies
    assert s.is_stale(last_close + DAY_MS + 6 * 60_000)
    assert CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), ()).is_stale(0)


def test_up_to_excludes_bars_not_closed_at_as_of():
    s = series_from_closes([1, 2, 3, 4, 5])
    cut = s.up_to(s.candles[2].close_time_ms + 1)
    assert cut.candles == s.candles[:3]
    assert s.up_to(s.candles[2].close_time_ms).candles == s.candles[:2]
