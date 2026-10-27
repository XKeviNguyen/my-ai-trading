"""PERF-4 (rules read once per tick), FLOW-11 (stop honored mid-tick), PERF-6 (backoff when ticks fail)."""
import asyncio
import logging
import random

from test_monitor_store import build

from my_ai_trading.compiler import compile_prompts
from my_ai_trading.monitor import MAX_TICK_BACKOFF_S, Monitor
from my_ai_trading.providers.binance import MarketDataError
from my_ai_trading.store import UserSettings, WatchItem


def add_rules(store, prompts):
    for p in prompts:
        rule = compile_prompts([p]).rule
        sid, v = store.create_strategy(rule, [p])
        store.approve(sid, v, rule.rule_hash())


def test_sql_statements_per_tick_are_bounded_by_subscriptions_not_rules():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h", "1d", "1w"])
    ex, svc, store, mon = build(s)
    # 20 active 1h rules that do not match a rising series: the steady state of a quiet market.
    add_rules(store, [f"1h close below SMA{5 + n}" for n in range(20)])
    statements = []
    store._db.set_trace_callback(statements.append)
    try:
        alerts = asyncio.run(mon.tick())
    finally:
        store._db.set_trace_callback(None)
    subs = len(mon.state)
    assert subs == 3 and not [a for a in alerts if a["event_type"] == "STRATEGY_MATCH"]
    assert sum("strategy_versions" in q for q in statements) == 1  # active rules loaded once per tick
    # Engine/methodology alerts may add a few writes; 20 rules x 3 subscriptions used to cost 60+ reads.
    assert len(statements) <= 2 * subs + 2 * len(alerts), statements


def test_stop_is_honored_between_subscriptions():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h", "1d", "1w"])
    ex, svc, store, mon = build(s)

    class Stop:  # tick() only calls is_set(); set from the worker thread after the first fetch
        flag = False

        def is_set(self):
            return self.flag
    stop = Stop()
    original = ex.klines

    def klines(*a, **kw):
        stop.flag = True
        return original(*a, **kw)
    ex.klines = klines
    asyncio.run(mon.tick(stop=stop))
    assert ex.calls == 1  # the two remaining subscriptions were not fetched
    assert len(mon.state) == 1
    stop.flag = False
    asyncio.run(mon.tick())  # the next start picks up the rest
    assert len(mon.state) == 3


def test_run_forever_stops_mid_tick():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h", "1d", "1w"])
    ex, svc, store, mon = build(s)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = ex.klines

        def klines(*a, **kw):
            loop.call_soon_threadsafe(stop.set)
            return original(*a, **kw)
        ex.klines = klines
        await asyncio.wait_for(mon.run_forever(stop=stop, idle_s=60), 5)
    asyncio.run(main())
    assert ex.calls == 1


def test_tick_backoff_grows_with_jitter_and_never_exceeds_the_cap():
    mon = Monitor(service=None, store=None, rng=random.Random(3))
    for failures in range(0, 40):
        mon.tick_failures = failures
        for _ in range(20):
            d = mon.tick_backoff_s(1.0)
            assert 0 < d <= MAX_TICK_BACKOFF_S
            assert d >= min(MAX_TICK_BACKOFF_S, 0.5 * 2 ** min(failures, 16))  # exponential, jittered x0.5-1.5


def test_failing_first_clock_sync_backs_off_instead_of_a_tight_loop(caplog):
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = build(s)
    calls = []

    def down(market=None):
        calls.append(1)
        raise MarketDataError("network unreachable")
    ex.server_time_ms = down

    async def main():
        stop = asyncio.Event()
        runner = asyncio.ensure_future(mon.run_forever(stop=stop, idle_s=0.01))
        await asyncio.sleep(0.6)
        stop.set()
        await asyncio.wait_for(runner, 5)
    with caplog.at_level(logging.WARNING, logger="my_ai_trading.monitor"):
        asyncio.run(main())
    # Without backoff this was ~60 attempts (and log lines) in 0.6 s at idle_s=0.01; with exponential backoff
    # (0.02, 0.04, 0.08, 0.16, 0.32 s, each x0.5-1.5) at most about 8 fit.
    assert 2 <= len(calls) <= 8, len(calls)
    assert len(caplog.records) == len(calls) and mon.tick_failures == len(calls)
    ex.server_time_ms = lambda market=None: ex.now
    asyncio.run(mon.tick())  # recovers once the clock syncs
