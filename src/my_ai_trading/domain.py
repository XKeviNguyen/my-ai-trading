"""Core market-data types: instrument identity, timeframes, candles and validated series.

All times are UTC epoch milliseconds. A candle covers [open_time_ms, open_time_ms + duration);
``close_time_ms`` follows the Binance convention of the last millisecond *inside* the bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from typing import Any, Iterable

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS

# Fixed-duration intervals only; calendar months are excluded on purpose (variable length).
_DURATIONS_MS: dict[str, int] = {
    "1m": MINUTE_MS, "3m": 3 * MINUTE_MS, "5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS,
    "30m": 30 * MINUTE_MS, "1h": HOUR_MS, "2h": 2 * HOUR_MS, "4h": 4 * HOUR_MS,
    "6h": 6 * HOUR_MS, "8h": 8 * HOUR_MS, "12h": 12 * HOUR_MS, "1d": DAY_MS,
    "3d": 3 * DAY_MS, "1w": 7 * DAY_MS,
    "1M": 31 * DAY_MS,  # calendar month: nominal upper bound only; exact bounds use next_open_ms
}
CALENDAR_CODES = frozenset({"1M"})


class MarketType(StrEnum):
    USDM_PERPETUAL = "usdm_perpetual"
    SPOT = "spot"


class Calendar(StrEnum):
    CRYPTO_24X7 = "crypto_24x7"


class Applicability(StrEnum):
    """Whether a methodology can be evaluated here — never a buy/sell signal (PRD F03)."""
    SUPPORTED_NATIVE = "SUPPORTED_NATIVE"
    SUPPORTED_HYBRID = "SUPPORTED_HYBRID"
    ADAPTED_UNVALIDATED = "ADAPTED_UNVALIDATED"
    UNSUPPORTED_TIMEFRAME = "UNSUPPORTED_TIMEFRAME"
    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNVERIFIED_METHOD = "UNVERIFIED_METHOD"
    STALE_DATA = "STALE_DATA"


class AnalysisMode(StrEnum):
    NATIVE = "NATIVE"
    HYBRID = "HYBRID"
    ADAPTED = "ADAPTED"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True, slots=True)
class Timeframe:
    code: str

    def __post_init__(self) -> None:
        if self.code not in _DURATIONS_MS:
            raise ValueError(f"Unsupported timeframe {self.code!r}")

    @property
    def duration_ms(self) -> int:
        """Exact bar length for fixed intervals; a nominal upper bound for calendar months.
        Use next_open_ms / next_bar_close_ms / bars_between for anything that must be exact."""
        return _DURATIONS_MS[self.code]

    @property
    def is_calendar(self) -> bool:
        return self.code in CALENDAR_CODES

    def next_open_ms(self, open_ms: int) -> int:
        """Open time of the bar after the bar that opens at ``open_ms`` (UTC calendar for 1M)."""
        if not self.is_calendar:
            return open_ms + self.duration_ms
        d = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)

    def is_aligned_open(self, open_ms: int) -> bool:
        if not self.is_calendar:
            return True  # fixed intervals are checked by spacing
        d = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        return (d.day, d.hour, d.minute, d.second, d.microsecond) == (1, 0, 0, 0, 0)

    def next_bar_close_ms(self, close_ms: int) -> int:
        """Last millisecond of the bar after the bar that closes at ``close_ms``."""
        return self.next_open_ms(close_ms + 1) - 1

    def bars_between(self, earlier_open_ms: int, later_open_ms: int) -> int:
        """Whole bars from one bar open to a later bar open."""
        if not self.is_calendar:
            return (later_open_ms - earlier_open_ms) // self.duration_ms
        a = datetime.fromtimestamp(earlier_open_ms / 1000, tz=timezone.utc)
        b = datetime.fromtimestamp(later_open_ms / 1000, tz=timezone.utc)
        return (b.year - a.year) * 12 + (b.month - a.month)

    @classmethod
    def all_codes(cls) -> tuple[str, ...]:
        return tuple(_DURATIONS_MS)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    """Same ticker text on a different venue or market type is a different instrument."""
    venue: str
    market_type: MarketType
    symbol: str
    quote_asset: str | None = None
    calendar: Calendar = Calendar.CRYPTO_24X7

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.market_type}:{self.symbol}"

    def as_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "market_type": str(self.market_type), "symbol": self.symbol,
                "quote_asset": self.quote_asset, "calendar": str(self.calendar), "key": self.key}


def _strict_ms(value: Any, name: str) -> int:
    # bool is an int subclass; floats would be silently truncated (audit L1).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be integer milliseconds")
    return value


@dataclass(frozen=True, slots=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0

    def __post_init__(self) -> None:
        _strict_ms(self.open_time_ms, "open_time_ms")
        _strict_ms(self.close_time_ms, "close_time_ms")
        if self.open_time_ms < 0 or self.close_time_ms < self.open_time_ms:
            raise ValueError("Invalid candle time range")
        prices = (self.open, self.high, self.low, self.close)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) and v > 0
                   for v in prices):
            raise ValueError("Prices must be positive and finite")
        if self.low > min(prices) or self.high < max(prices):
            raise ValueError("Inconsistent OHLC prices")
        for v in (self.volume, self.quote_volume):
            if isinstance(v, bool) or not isfinite(v) or v < 0:
                raise ValueError("Volumes must be nonnegative and finite")

    def is_closed(self, now_ms: int, *, safety_ms: int = 0) -> bool:
        """Closed once ``now`` is past the bar's last millisecond plus a clock-skew margin."""
        return now_ms > self.close_time_ms + safety_ms

    @classmethod
    def from_binance(cls, row: Any) -> "Candle":
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            raise ValueError("Binance kline must contain at least 8 fields")
        try:
            return cls(
                open_time_ms=_strict_ms(row[0], "open_time"), close_time_ms=_strict_ms(row[6], "close_time"),
                open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                volume=float(row[5]), quote_volume=float(row[7]),
            )
        except (TypeError, OverflowError) as exc:
            raise ValueError("Malformed Binance kline") from exc


@dataclass(frozen=True)
class CandleSeries:
    """Closed candles for one instrument and timeframe, validated for order, spacing and gaps.

    Construction fails on duplicates, disorder, misaligned spacing or bars whose duration does not
    match the timeframe (audit H3). Gaps are *recorded*, never filled (audit H2).
    """
    instrument: InstrumentIdentity
    timeframe: Timeframe
    candles: tuple[Candle, ...]
    gaps: tuple[tuple[int, int], ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        tf = self.timeframe
        gaps: list[tuple[int, int]] = []
        for c in self.candles:
            if c.close_time_ms != tf.next_open_ms(c.open_time_ms) - 1 or not tf.is_aligned_open(c.open_time_ms):
                raise ValueError(f"Candle duration does not match timeframe {tf}")
        for prev, cur in zip(self.candles, self.candles[1:]):
            step = cur.open_time_ms - prev.open_time_ms
            if step <= 0:
                raise ValueError("Candles must be strictly ordered and unique")
            if not tf.is_calendar and step % tf.duration_ms:
                raise ValueError("Candle open times are misaligned for the timeframe")
            expected = tf.next_open_ms(prev.open_time_ms)
            if cur.open_time_ms != expected:
                gaps.append((expected, cur.open_time_ms))
        object.__setattr__(self, "gaps", tuple(gaps))

    @classmethod
    def from_candles(cls, instrument: InstrumentIdentity, timeframe: Timeframe | str,
                     candles: Iterable[Candle], *, now_ms: int, safety_ms: int = 0) -> "CandleSeries":
        """Keep only candles closed at ``now_ms`` (forming bars are dropped, never used)."""
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(timeframe)
        closed = tuple(c for c in candles if c.is_closed(now_ms, safety_ms=safety_ms))
        return cls(instrument, tf, closed)

    def missing_bars(self) -> int:
        return sum(self.timeframe.bars_between(start, end) for start, end in self.gaps)

    def contiguous_tail(self) -> tuple[Candle, ...]:
        """Longest run of consecutive bars ending at the latest bar (indicators never span a gap)."""
        if not self.gaps:
            return self.candles
        last_gap_end = self.gaps[-1][1]
        return tuple(c for c in self.candles if c.open_time_ms >= last_gap_end)

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    def is_stale(self, now_ms: int, *, grace_ms: int | None = None) -> bool:
        """Stale when the newest closed bar is older than one full bar plus grace."""
        if not self.candles:
            return True
        grace = min(self.timeframe.duration_ms // 2, 5 * MINUTE_MS) if grace_ms is None else grace_ms
        # The bar after the newest one should have closed by now.
        return now_ms > self.timeframe.next_bar_close_ms(self.candles[-1].close_time_ms) + 1 + grace

    def up_to(self, as_of_ms: int) -> "CandleSeries":
        """View containing only bars closed at ``as_of_ms`` — used for replay without look-ahead."""
        return CandleSeries(self.instrument, self.timeframe,
                            tuple(c for c in self.candles if c.close_time_ms < as_of_ms))
