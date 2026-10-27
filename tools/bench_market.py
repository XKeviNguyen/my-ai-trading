"""Offline market-cache benchmark (DATA-5, PERF-5). SYNTHETIC data, fake provider, no network.

    PYTHONPATH=src python3 tools/bench_market.py

1. Partial first response (50 of 401 rows), then healthy calls: bars served after each new daily close.
2. One incremental response missing older rows: gaps left in the served series.
3. 400 subscriptions (50 symbols x 8 keys), two passes: provider calls on the second pass.
"""
from __future__ import annotations

from my_ai_trading.domain import DAY_MS, InstrumentIdentity, MarketType
from my_ai_trading.fixtures import candles_from_closes, uptrend_closes
from my_ai_trading.market import MarketDataService, ServerClock


class Provider:
    def __init__(self, candles, now_ms, plan=()):
        self.candles, self.now_ms, self.plan, self.calls = candles, now_ms, list(plan), 0

    def server_time_ms(self, market=None):
        return self.now_ms

    def instrument(self, symbol, market):
        return InstrumentIdentity("binance", MarketType(market), symbol, "USDT")

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        self.calls += 1
        visible = [c for c in self.candles if c.open_time_ms <= self.now_ms][-limit:]
        cap = self.plan.pop(0) if self.plan else None
        return visible if cap is None else visible[-cap:]


def make(plan=()):
    cs = candles_from_closes(uptrend_closes(700))
    prov = Provider(cs, cs[-60].open_time_ms + DAY_MS // 2, plan)
    return prov, MarketDataService(prov, clock=ServerClock(prov, local=lambda: prov.now_ms / 1000))


def main() -> None:
    inst = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")
    prov, svc = make(plan=[50])
    served = []
    for _ in range(5):
        served.append(len(svc.series(inst, "1d", bars=400).candles))
        prov.now_ms += DAY_MS
    print(f"partial first response: bars served at 5 successive closes={served} provider_calls={prov.calls}")

    prov, svc = make()
    svc.series(inst, "1d", bars=400)
    prov.now_ms += 6 * DAY_MS
    prov.plan = [2]
    s = svc.series(inst, "1d", bars=400)
    prov.now_ms += DAY_MS
    s2 = svc.series(inst, "1d", bars=400)
    print(f"incremental response after a hole: gaps={len(s.gaps)} missing_bars={s.missing_bars()} "
          f"next_close_gaps={len(s2.gaps)} provider_calls={prov.calls}")

    cs = candles_from_closes([1.0 + 0.001 * i for i in range(60)])
    prov = Provider(cs, cs[-1].open_time_ms + DAY_MS // 2)
    svc = MarketDataService(prov, clock=ServerClock(prov, local=lambda: prov.now_ms / 1000))
    counts = []
    for _ in range(2):
        before = prov.calls
        for i in range(50):
            for j in range(8):
                svc.series(InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, f"S{i:02d}USDT{j}", "USDT"),
                           "1d", bars=50)
        counts.append(prov.calls - before)
    print(f"400 subscriptions: provider calls first_pass={counts[0]} second_pass={counts[1]}")


if __name__ == "__main__":
    main()
