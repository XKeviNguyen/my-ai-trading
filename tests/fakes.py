"""Shared offline fakes. SYNTHETIC data only."""
from my_ai_trading.domain import DAY_MS, InstrumentIdentity, MarketType
from my_ai_trading.fixtures import candles_from_closes, uptrend_closes
from my_ai_trading.market import MarketDataService, ServerClock

PERP = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")
START = 1_735_516_800_000  # Monday 2024-12-30 00:00Z, aligned for 1h/4h/1d/1w


class Exchange:
    """Fake public exchange serving bars that have opened by `now`, per timeframe."""

    def __init__(self, histories, now):
        self.histories, self.now = histories, now
        self.fail: Exception | None = None
        self.calls = 0

    def server_time_ms(self, market=None):
        return self.now

    def instrument(self, symbol, market):
        if symbol != "SANDUSDT":
            raise ValueError(f"{symbol} not found on Binance {market}")
        return InstrumentIdentity("binance", MarketType(market), symbol, "USDT")

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        bars = [c for c in self.histories[timeframe.code] if c.open_time_ms <= self.now]
        return bars[-limit:]


def make_exchange(days=420):
    hours = days * 24
    hist = {
        "1h": candles_from_closes([1.0 + 0.0001 * i for i in range(hours)], "1h", start_ms=START),
        "4h": candles_from_closes([1.0 + 0.0004 * i for i in range(hours // 4)], "4h", start_ms=START),
        "1d": candles_from_closes(uptrend_closes(days), "1d", start_ms=START),
        "1w": candles_from_closes(uptrend_closes(days // 7, growth=0.02), "1w", start_ms=START),
    }
    ex = Exchange(hist, START + 400 * DAY_MS)
    svc = MarketDataService(ex, clock=ServerClock(ex, local=lambda: ex.now / 1000))
    return ex, svc
