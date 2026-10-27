import math

import pytest
from hypothesis import given, settings, strategies as st

from my_ai_trading.fixtures import candles_from_closes
from my_ai_trading.domain import Candle
from my_ai_trading.indicators import (adx, atr_sma_tr, atr_wilder, bollinger, ema, ema_series, macd,
                                      prior_high, prior_low, rsi_wilder, sma, sma_series, true_ranges,
                                      volume_ratio)

prices = st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False), min_size=1, max_size=300)


def test_sma_and_ema_known_values():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 3) is None
    # EMA(3): seed SMA(1,2,3)=2, alpha=0.5 -> 0.5*4 + 0.5*2 = 3
    assert ema([1, 2, 3, 4], 3) == 3.0
    with pytest.raises(ValueError):
        sma([1], 0)
    with pytest.raises(ValueError):
        sma([1], True)


@given(prices, st.integers(min_value=1, max_value=50))
@settings(max_examples=150)
def test_rolling_sma_matches_point_sma(values, period):
    rolled = sma_series(values, period)
    for i in range(len(values)):
        point = sma(values[: i + 1], period)
        if point is None:
            assert rolled[i] is None
        else:
            assert math.isclose(rolled[i], point, rel_tol=1e-9, abs_tol=1e-9)


def test_atr_conventions_differ_and_are_both_available():
    cs = candles_from_closes([10, 11, 10.5, 12, 11.5, 13, 12.2, 12.8, 14, 13.1, 13.9, 15, 14.2, 15.5, 16, 15.1])
    tr = true_ranges(cs)
    assert atr_sma_tr(cs, 14) == pytest.approx(sum(tr[-14:]) / 14)
    seed = sum(tr[:14]) / 14
    assert atr_wilder(cs, 14) == pytest.approx((seed * 13 + tr[14]) / 14)
    assert atr_wilder(cs[:14], 14) is None


def test_rsi_bounds_and_flat():
    assert rsi_wilder([1.0] * 20) == 50.0
    assert rsi_wilder([float(i) for i in range(1, 21)]) == 100.0
    v = rsi_wilder([1, 2, 1.5, 2.5, 2, 3, 2.2, 2.8, 3.5, 3.1, 3.3, 3.9, 3.4, 4.1, 4.0])
    assert 0 < v < 100


@given(st.lists(st.floats(min_value=1, max_value=1e7, allow_nan=False), min_size=21, max_size=60),
       st.floats(min_value=0, max_value=1e9, allow_nan=False))
def test_volume_ratio_and_pivots_never_use_the_tested_bar(volumes, last_volume):
    """Changing the latest bar must not change the threshold it is compared against."""
    closes = [1.0 + 0.01 * i for i in range(len(volumes))]
    cs = candles_from_closes(closes, volumes=volumes)
    base = sum(volumes[-21:-1]) / 20
    cs2 = cs[:-1] + [candles_from_closes(closes, volumes=volumes[:-1] + [last_volume])[-1]]
    assert volume_ratio(cs2) == pytest.approx(last_volume / base)
    assert prior_high(cs) == prior_high(cs2) == max(c.high for c in cs[-21:-1])
    assert prior_low(cs) == min(c.low for c in cs[-21:-1])


# --- MACD / Bollinger / ADX -------------------------------------------------------------------

DAY = 86_400_000


def _bars(rows):
    """rows of (high, low, close); open = close for simplicity, daily bars from t=0."""
    return [Candle(i * DAY, (i + 1) * DAY - 1, c, h, lo, c, 1.0) for i, (h, lo, c) in enumerate(rows)]


def test_ema_series_matches_point_ema():
    values = [1.0, 2.0, 3.0, 4.0, 8.0]
    s = ema_series(values, 3)
    assert s[:2] == [None, None]
    for i in range(2, len(values)):
        assert s[i] == ema(values[: i + 1], 3)
    assert ema_series([1.0], 3) == [None]


def test_macd_known_values():
    # fast=2 (alpha 2/3), slow=3 (alpha 1/2), signal=2, closes 1,2,3,4,8:
    # EMA2 = 1.5, 2.5, 3.5, 6.5; EMA3 = 2, 3, 5.5 -> MACD line = 0.5, 0.5, 1.0
    # signal EMA2 of the line: seed (0.5+0.5)/2 = 0.5, then 2/3*1 + 1/3*0.5 = 5/6.
    out = macd([1, 2, 3, 4, 8], fast=2, slow=3, signal=2)
    assert out["macd"] == pytest.approx(1.0)
    assert out["signal"] == pytest.approx(5 / 6)
    assert out["histogram"] == pytest.approx(1 / 6)
    assert macd([1, 2, 3, 4], fast=2, slow=3, signal=2) == pytest.approx(
        {"macd": 0.5, "signal": 0.5, "histogram": 0.0})
    assert macd([1, 2, 3], fast=2, slow=3, signal=2) is None  # needs slow + signal - 1 = 4
    assert macd([1.0] * 33) is None and macd([1.0] * 34) is not None  # default 12/26/9
    with pytest.raises(ValueError):
        macd([1.0] * 50, fast=26, slow=12)


def test_bollinger_known_values_population_std():
    out = bollinger([1, 2, 3, 4, 5], period=5, k=2)
    sd = math.sqrt(2)  # population variance (4+1+0+1+4)/5 = 2
    assert out["middle"] == pytest.approx(3)
    assert out["upper"] == pytest.approx(3 + 2 * sd)
    assert out["lower"] == pytest.approx(3 - 2 * sd)
    assert out["bandwidth"] == pytest.approx(4 * sd / 3)
    assert out["percent_b"] == pytest.approx((5 - (3 - 2 * sd)) / (4 * sd))
    flat = bollinger([2.0] * 20)
    assert flat["bandwidth"] == 0 and flat["percent_b"] is None
    assert bollinger([1.0] * 19) is None
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            bollinger([1.0] * 20, k=bad)


def test_adx_known_values_wilder():
    # period 2: TR = 2, 2, 2; +DM = 1, 1, 0; -DM = 0, 0, 1
    # first sums: TR 4, +DM 2, -DM 0 -> +DI 50, -DI 0, DX 100
    # next: TR 4-2+2 = 4, +DM 2-1+0 = 1, -DM 0-0+1 = 1 -> +DI 25, -DI 25, DX 0; ADX = mean(100, 0)
    cs = _bars([(10, 8, 9), (11, 9, 10), (12, 10, 11), (11, 9, 9.5)])
    assert adx(cs, 2) == pytest.approx({"adx": 50.0, "plus_di": 25.0, "minus_di": 25.0})
    assert adx(cs[:3], 2) is None  # needs 2 * period candles
    assert adx(candles_from_closes([1.0] * 27), 14) is None
    assert adx(candles_from_closes([1.0] * 28), 14) is not None


def test_adx_trend_direction_and_flat_market():
    up = adx(candles_from_closes([10 * 1.01 ** i for i in range(60)]))
    assert up["plus_di"] > up["minus_di"] and up["adx"] > 50
    down = adx(candles_from_closes([10 * 0.99 ** i for i in range(60)]))
    assert down["minus_di"] > down["plus_di"] and down["adx"] > 50
    flat = adx(_bars([(5.0, 5.0, 5.0)] * 30))  # zero true range: DI and DX defined as 0
    assert flat == {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}


@given(prices, st.integers(min_value=1, max_value=40), st.floats(min_value=0.1, max_value=5))
@settings(max_examples=150)
def test_bollinger_band_order(values, period, k):
    out = bollinger(values, period, k)
    if len(values) < period:
        assert out is None
        return
    assert out["lower"] <= out["middle"] <= out["upper"]
    assert out["bandwidth"] >= 0


@given(st.lists(st.floats(min_value=0.5, max_value=2.0, allow_nan=False), min_size=2, max_size=120),
       st.integers(min_value=1, max_value=20))
@settings(max_examples=150)
def test_adx_and_di_bounds(steps, period):
    closes = [100.0]
    for f in steps:
        closes.append(min(max(closes[-1] * f, 1e-3), 1e9))
    out = adx(candles_from_closes(closes), period)
    if len(closes) < 2 * period:
        assert out is None
        return
    for v in out.values():
        assert -1e-9 <= v <= 100 + 1e-9


@given(prices)
@settings(max_examples=100)
def test_macd_histogram_identity(values):
    out = macd(values, 3, 6, 4)
    if len(values) < 9:
        assert out is None
    else:
        assert out["histogram"] == pytest.approx(out["macd"] - out["signal"], abs=1e-6)
