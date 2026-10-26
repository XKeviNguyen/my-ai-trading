"""Dependency-free indicators over CLOSED candles.

Conventions (PRD F02 requires them stated):
- ``sma(n)``: arithmetic mean of the last n closes; ``None`` until n values exist.
- ``ema(n)``: alpha = 2/(n+1), seeded with the SMA of the first n values; needs n values.
- ``atr_wilder(n)``: Wilder smoothing of true range, seeded with the mean of the first n TRs;
  needs n+1 candles. ``atr_sma_tr(n)`` is the plain mean of the last n TRs (the V0.1 formula).
- ``rsi_wilder(n)``: Wilder RSI, needs n+1 closes.
- Volume ratio and prior high/low compare the latest bar against the n bars *before* it,
  so the bar being tested never contributes to its own threshold (no look-ahead).
"""
from __future__ import annotations

from typing import Sequence

from .domain import Candle


def _check(period: int) -> None:
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("Period must be a positive integer")


def sma(values: Sequence[float], period: int) -> float | None:
    _check(period)
    return sum(values[-period:]) / period if len(values) >= period else None


def sma_series(values: Sequence[float], period: int) -> list[float | None]:
    """Rolling SMA aligned to ``values`` (O(n))."""
    _check(period)
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    _check(period)
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = alpha * v + (1 - alpha) * value
    return value


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    return [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            for p, c in zip(candles, candles[1:])]


def atr_sma_tr(candles: Sequence[Candle], period: int = 14) -> float | None:
    _check(period)
    if len(candles) < period + 1:
        return None
    return sum(true_ranges(candles[-period - 1:])) / period


def atr_wilder(candles: Sequence[Candle], period: int = 14) -> float | None:
    _check(period)
    tr = true_ranges(candles)
    if len(tr) < period:
        return None
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value


def rsi_wilder(closes: Sequence[float], period: int = 14) -> float | None:
    _check(period)
    if len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def volume_ratio(candles: Sequence[Candle], period: int = 20) -> float | None:
    """Latest bar volume divided by the mean volume of the ``period`` bars before it."""
    _check(period)
    if len(candles) < period + 1:
        return None
    prev_avg = sum(c.volume for c in candles[-period - 1:-1]) / period
    return candles[-1].volume / prev_avg if prev_avg > 0 else None


def prior_high(candles: Sequence[Candle], period: int = 20) -> float | None:
    _check(period)
    if len(candles) < period + 1:
        return None
    return max(c.high for c in candles[-period - 1:-1])


def prior_low(candles: Sequence[Candle], period: int = 20) -> float | None:
    _check(period)
    if len(candles) < period + 1:
        return None
    return min(c.low for c in candles[-period - 1:-1])
