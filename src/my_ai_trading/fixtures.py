"""SYNTHETIC offline data for tests and the demo. Never represented as market history."""
from __future__ import annotations

from typing import Sequence

from .domain import Candle, CandleSeries, InstrumentIdentity, MarketType, Timeframe

DEMO_INSTRUMENT = InstrumentIdentity("synthetic", MarketType.USDM_PERPETUAL, "DEMOUSDT", "USDT")
EPOCH_2025_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z (a Wednesday)
MONDAY_2024_12_30_MS = 1_735_516_800_000  # weekly bars on Binance open Monday 00:00 UTC


def candles_from_closes(closes: Sequence[float], timeframe: str = "1d", *, start_ms: int | None = None,
                        volumes: Sequence[float] | None = None, spread: float = 0.01) -> list[Candle]:
    tf = Timeframe(timeframe)
    start = start_ms if start_ms is not None else (MONDAY_2024_12_30_MS if timeframe == "1w" else EPOCH_2025_MS)
    # EPOCH_2025_MS is also the 1st of a month, so it is a valid monthly open.
    out: list[Candle] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        hi = max(open_, close) * (1 + spread)
        lo = min(open_, close) * (1 - spread)
        vol = volumes[i] if volumes is not None else 1_000_000.0
        t = start if i == 0 else tf.next_open_ms(out[-1].open_time_ms)
        out.append(Candle(t, tf.next_open_ms(t) - 1, open_, hi, lo, close, vol, vol * close))
        prev = close
    return out


def series_from_closes(closes: Sequence[float], timeframe: str = "1d", *,
                       instrument: InstrumentIdentity = DEMO_INSTRUMENT, **kw) -> CandleSeries:
    return CandleSeries(instrument, Timeframe(timeframe), tuple(candles_from_closes(closes, timeframe, **kw)))


def uptrend_closes(n: int = 400, start: float = 0.02, growth: float = 0.004) -> list[float]:
    return [start * (1 + growth) ** i * (1 + 0.003 * ((i % 7) - 3)) for i in range(n)]


def downtrend_closes(n: int = 400, start: float = 1.0, decay: float = 0.004) -> list[float]:
    return [start * (1 - decay) ** i * (1 + 0.003 * ((i % 5) - 2)) for i in range(n)]


def demo_series(timeframe: str = "1d", n: int = 400) -> CandleSeries:
    return series_from_closes(uptrend_closes(n), timeframe)
