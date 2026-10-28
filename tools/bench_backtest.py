"""Offline backtest benchmark (PERF-1): per-bar cost of signal computation at several series lengths.

SYNTHETIC data only; no network, no accounts. Run from the repository root:

    PYTHONPATH=src python3 tools/bench_backtest.py --bars 100 400 1499 --repeat 3

Prints one line per (workload, bars) with the best-of-``repeat`` wall time and the per-bar cost in
microseconds. Point PYTHONPATH at another source tree to compare implementations on the same workload.
"""
from __future__ import annotations

import argparse
import random
import time

from my_ai_trading.backtest import BacktestConfig, compute_signals, run_backtest
from my_ai_trading.domain import CandleSeries
from my_ai_trading.fixtures import series_from_closes
from my_ai_trading.rules import StrategyRule


def random_series(n: int, *, seed: int = 7, timeframe: str = "1d") -> CandleSeries:
    rng = random.Random(seed)
    closes, vols = [100.0], [1000.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.02)))
        vols.append(1000.0 * rng.lognormvariate(0, 0.6))
    return series_from_closes(closes, timeframe, volumes=vols, spread=0.015)


def _m(name: str, tf: str = "1d", period: int | None = None, bar: str = "last_closed") -> dict:
    out = {"name": name, "timeframe": tf, "bar": bar}
    if period is not None:
        out["period"] = period
    return out


RULE = StrategyRule.model_validate({
    "name": "bench rule", "trigger_timeframe": "1d",
    "condition": {"kind": "all", "items": [
        {"kind": "compare", "left": _m("close"), "op": "crosses_above", "right": _m("sma", period=20)},
        {"kind": "any", "items": [
            {"kind": "compare", "left": _m("rsi_wilder", period=14), "op": "<", "right": 70.0},
            {"kind": "compare", "left": _m("macd_histogram"), "op": ">", "right": 0.0},
            {"kind": "compare", "left": _m("adx", period=14), "op": ">", "right": 20.0},
            {"kind": "compare", "left": _m("ema", period=50), "op": ">", "right": _m("sma", "1w", 10)},
        ]},
        {"kind": "not", "item": {"kind": "trend", "timeframe": "1w", "state": "DOWN"}},
    ]},
})


def workloads(n: int):
    daily = random_series(n)
    weekly = random_series(max(n // 7, 2), seed=11, timeframe="1w")
    cfg = BacktestConfig(hold_bars=5, stop_atr_mult=2.0)
    yield "event BREAKOUT signals", lambda: compute_signals("BREAKOUT", daily)
    yield "event SMA200_RECLAIM signals", lambda: compute_signals("SMA200_RECLAIM", daily)
    yield "rule (6 metrics, 1d+1w) signals", lambda: compute_signals(RULE, daily, other_series={"1w": weekly})
    yield "run_backtest rule + ATR stop", lambda: run_backtest(RULE, daily, cfg, other_series={"1w": weekly})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, nargs="+", default=[100, 400, 1499])
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    for n in args.bars:
        for name, fn in workloads(n):
            best = float("inf")
            for _ in range(args.repeat):
                t0 = time.perf_counter()
                fn()
                best = min(best, time.perf_counter() - t0)
            print(f"{name:34s} bars={n:5d} total_s={best:8.4f} per_bar_us={best / n * 1e6:9.1f}")


if __name__ == "__main__":
    main()
