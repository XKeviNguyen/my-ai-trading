"""Offline monitor benchmark (PERF-4): SQL statements and wall time per tick. SYNTHETIC data only.

    PYTHONPATH=src python3 tools/bench_monitor.py --symbols 50 --rules 20

A fake exchange serves the same rising synthetic 1h history for every symbol; ``--rules`` approved 1h
strategies are active (non-matching by default, the steady state; ``--matching`` alerts on every rule). Statements are counted with sqlite3's trace callback on the store connection.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time

from my_ai_trading.compiler import compile_prompts
from my_ai_trading.domain import DAY_MS, InstrumentIdentity, MarketType
from my_ai_trading.fixtures import candles_from_closes
from my_ai_trading.market import MarketDataService, ServerClock
from my_ai_trading.monitor import Monitor
from my_ai_trading.store import Store, UserSettings, WatchItem

START = 1_735_516_800_000  # Monday 2024-12-30 00:00Z


class Exchange:
    def __init__(self, hours: int):
        self.hist = candles_from_closes([1.0 + 0.0001 * i for i in range(hours)], "1h", start_ms=START)
        self.now = START + (hours // 24) * DAY_MS

    def server_time_ms(self, market=None):
        return self.now

    def instrument(self, symbol, market):
        return InstrumentIdentity("binance", MarketType(market), symbol, "USDT")

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        return [c for c in self.hist if c.open_time_ms <= self.now][-limit:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=50)
    ap.add_argument("--rules", type=int, default=20)
    ap.add_argument("--matching", action="store_true", help="rules match every bar (alert-heavy workload)")
    args = ap.parse_args()
    ex = Exchange(24 * 30)
    svc = MarketDataService(ex, clock=ServerClock(ex, local=lambda: ex.now / 1000))
    store = Store()
    store.save_settings(UserSettings(watchlist=[WatchItem(symbol=f"S{i:03d}USDT") for i in range(args.symbols)],
                                     timeframes=["1h"]))
    for n in range(args.rules):
        prompt = f"1h close {'above' if args.matching else 'below'} SMA{5 + n}"  # the history only rises
        rule = compile_prompts([prompt]).rule
        sid, v = store.create_strategy(rule, [prompt])
        store.approve(sid, v, rule.rule_hash())
    mon = Monitor(svc, store, rng=random.Random(0))
    count = [0]
    store._db.set_trace_callback(lambda _sql: count.__setitem__(0, count[0] + 1))
    for label in ("first tick (all due)", "second tick (none due)"):
        count[0] = 0
        t0 = time.perf_counter()
        alerts = asyncio.run(mon.tick())
        wall = time.perf_counter() - t0
        subs = len(mon.state)
        print(f"{label:24s} subscriptions={subs} rules={args.rules} sql_statements={count[0]} "
              f"per_subscription={count[0] / max(subs, 1):.1f} alerts={len(alerts)} wall_s={wall:.3f}")
    store._db.set_trace_callback(None)


if __name__ == "__main__":
    main()
