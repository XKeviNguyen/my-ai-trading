import math

import pytest
from hypothesis import given, settings, strategies as st

from my_ai_trading.fixtures import candles_from_closes
from my_ai_trading.indicators import (atr_sma_tr, atr_wilder, ema, prior_high, prior_low, rsi_wilder,
                                      sma, sma_series, true_ranges, volume_ratio)

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
