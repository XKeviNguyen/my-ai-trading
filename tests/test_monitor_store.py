"""PRD F06 + Scenarios D (outage/duplicates/replay) and L (per-strategy cadence)."""
import asyncio
import random

import pytest

from my_ai_trading.compiler import compile_prompts
from my_ai_trading.domain import DAY_MS, HOUR_MS, InstrumentIdentity, MarketType
from my_ai_trading.fixtures import candles_from_closes, uptrend_closes
from my_ai_trading.market import MarketDataService, ServerClock
from my_ai_trading.monitor import Monitor, _in_quiet_hours
from my_ai_trading.providers.binance import MarketDataError, RateLimitedError
from my_ai_trading.store import Selection, Store, UserSettings, WatchItem

PERP = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")
START = 1_735_516_800_000  # Monday 2024-12-30 00:00Z, aligned for 1h/1d/1w


class Exchange:
    """Fake public exchange: serves bars that have *opened* by `now`, per timeframe."""

    def __init__(self, histories, now):
        self.histories, self.now = histories, now
        self.fail: Exception | None = None
        self.calls = 0

    def server_time_ms(self, market=None):
        return self.now

    def instrument(self, symbol, market):
        return PERP

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        bars = [c for c in self.histories[timeframe.code] if c.open_time_ms <= self.now]
        return bars[-limit:]


def build(settings: UserSettings, *, hours=24 * 420, now_offset_ms=0):
    hist = {
        "1h": candles_from_closes([1.0 + 0.0001 * i for i in range(hours)], "1h", start_ms=START),
        "1d": candles_from_closes(uptrend_closes(hours // 24), "1d", start_ms=START),
        "1w": candles_from_closes(uptrend_closes(hours // 168, growth=0.02), "1w", start_ms=START),
    }
    now = START + 400 * DAY_MS + now_offset_ms
    ex = Exchange(hist, now)
    svc = MarketDataService(ex, clock=ServerClock(ex, local=lambda: ex.now / 1000))
    store = Store()
    store.save_settings(settings)
    return ex, svc, store, Monitor(svc, store, rng=random.Random(0))


def run(coro):
    return asyncio.run(coro)


def test_duplicate_polls_and_restart_replay_do_not_duplicate_alerts():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = build(s)
    rule = compile_prompts(["1h close above SMA20"]).rule
    sid, v = store.create_strategy(rule, ["1h close above SMA20"])
    store.approve(sid, v, rule.rule_hash())
    first = run(mon.tick())
    assert any(a["event_type"] == "STRATEGY_MATCH" for a in first)
    assert run(mon.tick()) == []  # same bar, not yet due
    restarted = Monitor(svc, store, rng=random.Random(1))  # fresh in-memory state, same DB
    assert run(restarted.tick()) == []  # replay of the same closed bar is idempotent
    assert len([a for a in store.alerts() if a["event_type"] == "STRATEGY_MATCH"]) == 1


def test_cooldown_and_new_bar_processing():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = build(s)
    rule = compile_prompts(["1h close above SMA20"]).rule.model_copy(update={"cooldown_bars": 3})
    sid, v = store.create_strategy(rule, ["x"])
    store.approve(sid, v, rule.rule_hash())
    counts = []
    for _ in range(5):
        new = run(mon.tick())
        counts.append(sum(a["event_type"] == "STRATEGY_MATCH" for a in new))
        ex.now += HOUR_MS
    assert counts == [1, 0, 0, 1, 0]


def test_unapproved_strategy_never_runs():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    _, _, store, mon = build(s)
    rule = compile_prompts(["1h close above SMA20"]).rule
    store.create_strategy(rule, ["x"])
    assert not any(a["event_type"] == "STRATEGY_MATCH" for a in run(mon.tick()))


def test_outage_backs_off_with_jitter_then_recovers():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = build(s)
    ex.fail = MarketDataError("network down")
    run(mon.tick())
    st = mon.state[(PERP.key, "1h")]
    assert st.failures == 1 and st.next_due_ms > ex.now
    calls = ex.calls
    run(mon.tick())
    assert ex.calls == calls  # not hammering while backing off
    ex.fail = RateLimitedError(120)
    ex.now = st.next_due_ms
    run(mon.tick())
    assert st.next_due_ms == ex.now + 120_000
    ex.fail, ex.now = None, st.next_due_ms
    run(mon.tick())
    assert st.failures == 0 and st.last_bar_open_ms is not None


def test_stale_feed_alerts_once_and_suppresses_analysis():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = build(s)
    ex.histories["1h"] = [c for c in ex.histories["1h"] if c.open_time_ms < ex.now - 6 * HOUR_MS]
    a1 = run(mon.tick())
    assert [a["event_type"] for a in a1] == ["STALE_DATA"]
    mon.state[(PERP.key, "1h")].next_due_ms = 0
    assert run(mon.tick()) == []  # same stale condition, not re-alerted


def test_scenario_l_weekly_methodology_not_reissued_on_hourly_closes():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"],
                     selected_methodologies=[Selection(methodology_id="weinstein")], analysis_timeframe="1h")
    ex, svc, store, mon = build(s)
    run(mon.tick())
    subs = {tf for _, tf in mon.subscriptions()}
    assert subs == {"1h", "1w"}  # weekly native timeframe subscribed, not converted
    weekly_fetches = []
    for _ in range(10):  # ten hourly closes inside the same week
        ex.now += HOUR_MS
        before = svc.fetch_count
        run(mon.tick())
        weekly_fetches.append(svc.fetch_count - before)
    assert mon.state[(PERP.key, "1w")].next_due_ms > ex.now  # weekly not due again within the week
    assert store.get_state(f"mstate|local|{PERP.key}|weinstein") is not None


def test_paused_and_quiet_hours():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"], alerts_paused=True)
    ex, svc, store, mon = build(s)
    assert run(mon.tick()) == [] and ex.calls == 0
    assert _in_quiet_hours(23 * HOUR_MS, (22, 6)) and _in_quiet_hours(2 * HOUR_MS, (22, 6))
    assert not _in_quiet_hours(12 * HOUR_MS, (22, 6))


def test_store_strategy_versioning_and_hash_bound_approval():
    store = Store()
    r1 = compile_prompts(["1h close above SMA20"]).rule
    sid, v1 = store.create_strategy(r1, ["a"])
    with pytest.raises(PermissionError):
        store.approve(sid, v1, "0" * 16)  # not the previewed hash
    store.approve(sid, v1, r1.rule_hash())
    r2 = compile_prompts(["1h close above SMA50"]).rule
    v2 = store.revise_strategy(sid, r2, ["b"])
    assert v2 == 2
    assert [x[2].rule_hash() for x in store.active_rules()] == [r1.rule_hash()]  # v1 still active
    store.approve(sid, v2, r2.rule_hash())
    versions = store.strategies()[0]["versions"]
    assert [v["status"] for v in versions] == ["ARCHIVED", "ACTIVE"]
    assert store.revise_strategy(sid, r2, ["b again"]) == 2  # identical rule: no new version
    with pytest.raises(PermissionError):
        store.approve(sid, v2, r2.rule_hash())  # already active
    with pytest.raises(KeyError):
        store.approve(sid, v2, r2.rule_hash(), user="someone-else")
    with pytest.raises(ValueError):
        store.create_strategy(r1, ["x"], kind="ADAPTED")


def test_settings_validation_and_disable_all_presets():
    store = Store()
    assert store.settings().selected_methodologies == []  # Scenario B: nothing enabled by default
    with pytest.raises(ValueError):
        UserSettings(selected_methodologies=[Selection(methodology_id="nobody")])
    with pytest.raises(ValueError):
        UserSettings(analysis_timeframe="2M")
    with pytest.raises(ValueError):
        UserSettings(watchlist=[WatchItem(symbol="bad sym")])


def test_usage_ledger_summary():
    store = Store()
    store.record_ai_usage(user="local", feature="copilot", provider="anthropic", model="m", input_tokens=100,
                          output_tokens=50, cost_usd=None, latency_ms=10, status="ok")
    store.record_ai_usage(user="local", feature="copilot", provider="anthropic", model="m", input_tokens=10,
                          output_tokens=5, cost_usd=0.01, latency_ms=20, status="ok")
    u = store.usage_summary()
    assert u["total_tokens"] == 165 and u["total_cost_usd"] == 0.01 and u["calls_with_unknown_cost"] == 1
