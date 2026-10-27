"""Dependency-free indicators over CLOSED candles.

Conventions (PRD F02 requires them stated):
- ``sma(n)``: arithmetic mean of the last n closes; ``None`` until n values exist.
- ``ema(n)``: alpha = 2/(n+1), seeded with the SMA of the first n values; needs n values.
- ``atr_wilder(n)``: Wilder smoothing of true range, seeded with the mean of the first n TRs;
  needs n+1 candles. ``atr_sma_tr(n)`` is the plain mean of the last n TRs (the V0.1 formula).
- ``rsi_wilder(n)``: Wilder RSI, needs n+1 closes.
- Volume ratio and prior high/low compare the latest bar against the n bars *before* it,
  so the bar being tested never contributes to its own threshold (no look-ahead).
- ``macd(fast, slow, signal)``: MACD line = EMA(fast) - EMA(slow) of closes, signal line = EMA(signal)
  of the MACD line, histogram = MACD - signal. Every EMA uses the ``ema`` convention above (SMA seed),
  and the MACD line exists from the bar where EMA(slow) exists, so it needs slow + signal - 1 closes.
- ``bollinger(n, k)``: middle = SMA(n); upper/lower = middle +/- k * POPULATION standard deviation
  (divide by n, not n - 1) of the same n closes. bandwidth = (upper - lower) / middle;
  percent_b = (close - lower) / (upper - lower), ``None`` when the bands have zero width. Needs n closes.
- ``adx(n)``: Welles Wilder (1978) definitions. +DM = high - previous high and -DM = previous low - low,
  each kept only when it is positive and larger than the other (else 0). TR, +DM and -DM are
  Wilder-smoothed (first value = sum of the first n, then S - S/n + x); +DI/-DI = 100 * DM_n / TR_n;
  DX = 100 * |+DI - -DI| / (+DI + -DI); ADX = mean of the first n DX values, then Wilder-smoothed.
  The first ADX therefore needs 2n candles (2n - 1 bar-to-bar differences). A zero smoothed TR gives
  DI = 0, and a zero DI sum gives DX = 0 (flat market, no directional movement).
"""
from __future__ import annotations

from math import sqrt
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


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    """Rolling EMA aligned to ``values``; same SMA-seed convention and arithmetic as ``ema``."""
    _check(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    out[period - 1] = value
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        out[i] = value
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float] | None:
    """Latest MACD, signal and histogram; ``None`` until slow + signal - 1 closes exist."""
    for p in (fast, slow, signal):
        _check(p)
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than the slow period")
    if len(closes) < slow + signal - 1:
        return None
    fast_s, slow_s = ema_series(closes, fast), ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_s[slow - 1:], slow_s[slow - 1:])]  # both defined from slow-1
    sig = ema(line, signal)
    assert sig is not None  # len(line) = len(closes) - slow + 1 >= signal
    return {"macd": line[-1], "signal": sig, "histogram": line[-1] - sig}


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0) -> dict[str, float | None] | None:
    """Bollinger Bands of the last ``period`` closes using the population standard deviation."""
    _check(period)
    if isinstance(k, bool) or not isinstance(k, (int, float)) or not k > 0 or k == float("inf"):
        raise ValueError("k must be a positive finite number")
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    sd = sqrt(sum((x - middle) ** 2 for x in window) / period)
    upper, lower = middle + k * sd, middle - k * sd
    width = upper - lower
    return {"middle": middle, "upper": upper, "lower": lower,
            "bandwidth": width / middle,
            "percent_b": (closes[-1] - lower) / width if width > 0 else None}


def adx(candles: Sequence[Candle], period: int = 14) -> dict[str, float] | None:
    """Wilder ADX with +DI/-DI for the latest bar; ``None`` until 2 * period candles exist."""
    _check(period)
    if len(candles) < 2 * period:
        return None
    tr = true_ranges(candles)
    pdm: list[float] = []
    mdm: list[float] = []
    for p, c in zip(candles, candles[1:]):
        up, down = c.high - p.high, p.low - c.low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)

    def di(dm_s: float, tr_s: float) -> float:
        return 100 * dm_s / tr_s if tr_s > 0 else 0.0

    tr_s, p_s, m_s = sum(tr[:period]), sum(pdm[:period]), sum(mdm[:period])
    dxs: list[float] = []
    for i in range(period - 1, len(tr)):
        if i >= period:
            tr_s = tr_s - tr_s / period + tr[i]
            p_s = p_s - p_s / period + pdm[i]
            m_s = m_s - m_s / period + mdm[i]
        plus, minus = di(p_s, tr_s), di(m_s, tr_s)
        total = plus + minus
        dxs.append(100 * abs(plus - minus) / total if total > 0 else 0.0)
    value = sum(dxs[:period]) / period
    for x in dxs[period:]:
        value = (value * (period - 1) + x) / period
    return {"adx": value, "plus_di": plus, "minus_di": minus}
