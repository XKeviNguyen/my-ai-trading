"""PERF-1: linear-time backtest signals must be IDENTICAL to the per-bar reference (rules.evaluate /
engine.analyze on series.up_to), and per-bar cost must not grow with series length. SYNTHETIC data only."""
import math
import random
import threading
import time

import pytest

from my_ai_trading.backtest import (ENGINE_EVENT_TYPES, INSUFFICIENT_DATA, MATCH, NO_MATCH, BacktestBusyError,
                                    BacktestConfig, MAX_CONCURRENT_BACKTESTS, backtest_slot, compute_signals,
                                    run_backtest, walk_forward)
from my_ai_trading.domain import Candle, CandleSeries, Timeframe
from my_ai_trading.engine import PIVOT_LOOKBACK, CONTRACTION_BASE, CONTRACTION_RECENT, analyze
from my_ai_trading.fixtures import DEMO_INSTRUMENT, EPOCH_2025_MS, MONDAY_2024_12_30_MS, series_from_closes
from my_ai_trading.indicators import atr_wilder
from my_ai_trading.rules import StrategyRule, evaluate


# ---------- the pre-PERF-1 implementation, kept verbatim as the oracle ----------

def reference_event_state(series, event_type, as_of):
    rep = analyze(series.up_to(as_of), as_of_ms=as_of)
    if rep["status"] != "OK":
        return INSUFFICIENT_DATA
    n = rep["data_quality"]["contiguous_bars"]
    need = {"FAILED_BREAKOUT": PIVOT_LOOKBACK + 2, "FAILED_BREAKDOWN": PIVOT_LOOKBACK + 2,
            "VOLATILITY_CONTRACTION": CONTRACTION_RECENT + CONTRACTION_BASE + 1}.get(event_type, 0)
    if event_type.startswith("SMA"):
        need = int(event_type[3:].split("_")[0]) + 1
    if n < need:
        return INSUFFICIENT_DATA
    return MATCH if any(e["type"] == event_type for e in rep["events"]) else NO_MATCH


def reference_signals(source, series, other_series=None):
    if isinstance(source, StrategyRule):
        by_tf = {**(other_series or {}), series.timeframe.code: series}
        return [evaluate(source, by_tf.__getitem__, as_of_ms=c.close_time_ms + 1)["result"] for c in series.candles]
    return [reference_event_state(series, source, c.close_time_ms + 1) for c in series.candles]


# ---------- synthetic data with volatility regimes, volume spikes, gaps and early-ending series ----------

def regime_series(n, *, seed, timeframe="1d", gaps=(), start=EPOCH_2025_MS):
    """Random walk whose volatility switches regime (so contraction events occur) with volume spikes.
    ``gaps``: (after_index, missing_bars) pairs."""
    rng = random.Random(seed)
    tf, skip = Timeframe(timeframe), dict(gaps)
    out, t, price, vol = [], start, 100.0, 0.02
    for i in range(n):
        if rng.random() < 0.03:
            vol = rng.choice([0.004, 0.01, 0.02, 0.05])
        o = price
        c = max(o * (1 + rng.gauss(0.0005, vol)), 0.01)
        hi, lo = max(o, c) * (1 + abs(rng.gauss(0, vol / 2))), min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = 1000.0 * (rng.choice([3.0, 0.0]) if rng.random() < 0.08 else rng.lognormvariate(0, 0.4))
        out.append(Candle(t, tf.next_open_ms(t) - 1, o, hi, lo, c, v, v * c))
        price = c
        t = tf.next_open_ms(t)
        for _ in range(skip.get(i, 0)):
            t = tf.next_open_ms(t)
    return CandleSeries(DEMO_INSTRUMENT, tf, tuple(out))


def m(name, tf="1d", period=None, bar="last_closed"):
    d = {"name": name, "timeframe": tf, "bar": bar}
    if period is not None:
        d["period"] = period
    return d


def cmp(left, op, right):
    return {"kind": "compare", "left": left, "op": op, "right": right}


def rule(cond, tf="1d"):
    return StrategyRule.model_validate({"name": "perf", "trigger_timeframe": tf, "condition": cond})


EVERY_METRIC = [m("close"), m("open"), m("high"), m("low"), m("volume"), m("sma", period=20), m("ema", period=12),
                m("volume_ratio", period=20), m("prior_high", period=20), m("prior_low", period=20),
                m("atr_wilder", period=14), m("rsi_wilder", period=14), m("adx", period=14),
                m("bollinger_percent_b", period=20), m("macd_line"), m("macd_histogram")]

RULES = [
    # every metric, on last and previous closed bars, against a constant chosen to flip often
    *[rule(cmp(x, ">", {"close": 100.0, "open": 100.0, "high": 100.0, "low": 100.0, "volume": 1000.0,
                        "sma": 100.0, "ema": 100.0, "volume_ratio": 1.0, "prior_high": 100.0, "prior_low": 100.0,
                        "atr_wilder": 2.0, "rsi_wilder": 50.0, "adx": 25.0, "bollinger_percent_b": 0.5,
                        "macd_line": 0.0, "macd_histogram": 0.0}[x["name"]]))
      for x in EVERY_METRIC],
    *[rule(cmp({**x, "bar": "previous_closed"}, "<=", y)) for x, y in
      [(m("close"), m("sma", period=5)), (m("rsi_wilder", period=7), 45.0), (m("adx", period=5), 30.0),
       (m("macd_histogram"), 0.0), (m("atr_wilder", period=3), m("atr_wilder", period=20))]],
    # crossings, including multi-timeframe and a previous-closed right-hand side (DATA-4 semantics)
    rule(cmp(m("close"), "crosses_above", m("sma", period=10))),
    rule(cmp(m("ema", period=5), "crosses_below", m("ema", period=21))),
    rule(cmp(m("close"), "crosses_above", m("sma", "1w", 4))),
    rule(cmp(m("close"), "crosses_below", m("close", "1w", bar="previous_closed"))),
    rule(cmp(m("rsi_wilder", period=14), "crosses_above", 50.0)),
    rule(cmp(m("macd_line"), "crosses_below", m("macd_line", "1w"))),
    # trend and boolean structure
    rule({"kind": "trend", "timeframe": "1d", "state": "UP"}),
    rule({"kind": "not", "item": {"kind": "trend", "timeframe": "1w", "state": "DOWN"}}),
    rule({"kind": "all", "items": [cmp(m("close"), ">", m("sma", period=50)),
                                   {"kind": "any", "items": [{"kind": "trend", "timeframe": "1d", "state": "MIXED"},
                                                             cmp(m("adx", "1w", 3), ">", 20.0)]}]}),
]


@pytest.fixture(scope="module")
def data():
    daily = regime_series(330, seed=3, gaps=((150, 4), (260, 1)))
    weekly = regime_series(60, seed=4, timeframe="1w", gaps=((20, 2),), start=MONDAY_2024_12_30_MS)
    weekly_short = CandleSeries(weekly.instrument, weekly.timeframe, weekly.candles[:30])  # stale for later bars
    return daily, weekly, weekly_short


@pytest.mark.parametrize("event_type", sorted(ENGINE_EVENT_TYPES))
def test_event_signals_identical_to_per_bar_analyze(event_type, data):
    rng = random.Random(1)
    cyclical = [100 * (1 + 0.15 * math.sin(i / 37)) * (1 + rng.gauss(0, 0.01)) for i in range(640)]
    seen = []
    for series in (data[0], regime_series(330, seed=13, gaps=((150, 4),)),
                   series_from_closes(cyclical, volumes=[1000.0 * (1 + 2 * (i % 23 == 0)) for i in range(640)]),
                   series_from_closes([1.0] * 30 + [2.0] * 5)):
        fast, ref = compute_signals(event_type, series), reference_signals(event_type, series)
        assert fast == ref
        seen += fast
    # The fixtures exercise every outcome for every event type, so the identity is not vacuous.
    assert {MATCH, NO_MATCH, INSUFFICIENT_DATA} <= set(seen)


@pytest.mark.parametrize("idx", range(len(RULES)))
def test_rule_signals_identical_to_rules_evaluate(idx, data):
    daily, weekly, weekly_short = data
    r = RULES[idx]
    for others in ({"1w": weekly}, {"1w": weekly_short}):
        assert compute_signals(r, daily, other_series=others) == reference_signals(r, daily, others)


def test_rule_signals_identical_on_random_series():
    for seed in range(4):
        daily = regime_series(260, seed=100 + seed, gaps=((90 + seed * 7, 1 + seed),))
        weekly = regime_series(40, seed=200 + seed, timeframe="1w", start=MONDAY_2024_12_30_MS)
        for r in RULES:
            assert compute_signals(r, daily, other_series={"1w": weekly}) == \
                reference_signals(r, daily, {"1w": weekly}), (seed, r.condition)


def test_every_per_bar_metric_value_is_bit_identical_to_metric_value(data):
    """Stronger than signal equality: each cached value equals rules.metric_value on the truncated series."""
    from my_ai_trading.backtest import _Prefix
    from my_ai_trading.rules import MetricRef, metric_value
    rng = random.Random(2)
    long = series_from_closes([100 * (1 + 0.3 * math.sin(i / 60)) * (1 + rng.gauss(0, 0.01)) for i in range(300)])
    for series in (data[0], long):
        pre = _Prefix(series)
        tails = [series.up_to(c.close_time_ms + 1).contiguous_tail() for c in series.candles]
        for spec in EVERY_METRIC + [m("sma", period=200), m("adx", period=3), m("rsi_wilder", period=2)]:
            ref = MetricRef.model_validate(spec)
            assert pre.array(ref.name, ref.period) == [metric_value(ref, t) for t in tails], spec
        from my_ai_trading.engine import trend_state
        from my_ai_trading.indicators import sma
        assert [pre.trend(i) for i in range(len(tails))] == [
            trend_state([c.close for c in t], {50: sma([c.close for c in t], 50),
                                               200: sma([c.close for c in t], 200)})["state"] for t in tails]


def test_trend_rules_identical_when_trend_is_known():
    rng = random.Random(2)
    daily = series_from_closes([100 * (1 + 0.3 * math.sin(i / 60)) * (1 + rng.gauss(0, 0.01)) for i in range(480)])
    weekly = series_from_closes([100 * (1 + 0.3 * math.sin(i / 25)) for i in range(260)], "1w")
    states = set()
    for r in RULES[-3:]:
        fast = compute_signals(r, daily, other_series={"1w": weekly})
        assert fast == reference_signals(r, daily, {"1w": weekly})
        states.update(fast)
    assert {MATCH, NO_MATCH, INSUFFICIENT_DATA} <= states


def test_intraday_trigger_with_higher_timeframe_crossing_matches_evaluate():
    h1 = regime_series(24 * 12, seed=5, timeframe="1h", gaps=((100, 3),))
    h4 = regime_series(6 * 12, seed=6, timeframe="4h")
    r = rule(cmp(m("close", "1h"), "crosses_above", m("sma", "4h", 3)), tf="1h")
    assert compute_signals(r, h1, other_series={"4h": h4}) == reference_signals(r, h1, {"4h": h4})


def test_backtest_outputs_and_stop_atr_identical_to_reference(data):
    daily, weekly, _ = data
    r = rule(cmp(m("close"), "crosses_above", m("sma", period=10)))
    for cfg in (BacktestConfig(hold_bars=3, stop_atr_mult=1.5), BacktestConfig(hold_bars=2, direction="short",
                                                                                stop_atr_mult=0.7,
                                                                                exit_on_rule_false=True)):
        fast = run_backtest(r, daily, cfg)
        ref = run_backtest(r, daily, cfg, signals=reference_signals(r, daily))
        assert fast == ref and fast["trades"]
        for t in fast["trades"]:
            s = daily.candles[t["signal_idx"]]
            assert t["atr_at_signal"] == atr_wilder(daily.up_to(s.close_time_ms + 1).contiguous_tail(), 14)
        wf = walk_forward("BREAKOUT", daily, cfg, n_folds=3)
        assert wf == walk_forward("BREAKOUT", daily, cfg, n_folds=3, signals=reference_signals("BREAKOUT", daily))


def test_concurrent_backtests_fail_fast_with_a_typed_error():
    s = series_from_closes([1.0 + 0.01 * i for i in range(40)])
    cfg = BacktestConfig(hold_bars=1)
    held = [backtest_slot() for _ in range(MAX_CONCURRENT_BACKTESTS)]
    for h in held:
        h.__enter__()
    try:
        t0 = time.perf_counter()
        with pytest.raises(BacktestBusyError):
            run_backtest("BREAKOUT", s, cfg)
        with pytest.raises(BacktestBusyError):
            walk_forward("BREAKOUT", s, cfg, n_folds=2)
        assert time.perf_counter() - t0 < 0.5  # fails fast, never queues
        assert not issubclass(BacktestBusyError, ValueError)  # must not be mapped to HTTP 422
    finally:
        for h in held:
            h.__exit__(None, None, None)
    assert run_backtest("BREAKOUT", s, cfg)["bars"] == 40  # slots are released
    errors = []

    def worker():
        try:
            run_backtest("BREAKOUT", s, cfg)
        except BacktestBusyError as exc:  # acceptable under contention; anything else is a bug
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(e, BacktestBusyError) for e in errors)


def _per_bar_seconds(n, repeat=3):
    daily = regime_series(n, seed=9)
    weekly = regime_series(max(n // 7, 2), seed=10, timeframe="1w", start=MONDAY_2024_12_30_MS)
    r = rule({"kind": "all", "items": [cmp(m("close"), "crosses_above", m("sma", period=20)),
                                       cmp(m("rsi_wilder", period=14), "<", 70.0),
                                       cmp(m("adx", period=14), ">", 10.0),
                                       {"kind": "trend", "timeframe": "1w", "state": "UP"}]})
    best = {}
    for name, fn in (("rule", lambda: compute_signals(r, daily, other_series={"1w": weekly})),
                     ("event", lambda: compute_signals("SMA200_RECLAIM", daily))):
        best[name] = min(_timed(fn) for _ in range(repeat)) / n
    return best


def _timed(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_per_bar_cost_does_not_grow_with_series_length():
    small, large = _per_bar_seconds(400), _per_bar_seconds(1499)
    for name in small:
        if small[name] > 0.005:  # > 5 ms per bar even when linear: machine far too slow to judge timing
            pytest.skip(f"machine too slow for a timing guard ({small[name] * 1e6:.0f} us/bar at 400 bars)")
        # Linear: per-bar cost roughly constant. The old quadratic code measured 3.7x here.
        assert large[name] <= 1.5 * small[name], (name, small[name], large[name])
