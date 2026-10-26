"""PRD F10 backtester: signal timing, costs, stops, fail-closed data handling, walk-forward."""
import math

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from my_ai_trading.backtest import (LABEL, BacktestConfig, compute_signals, run_backtest, simulate,
                                    walk_forward)
from my_ai_trading.domain import DAY_MS, Candle, CandleSeries, Timeframe
from my_ai_trading.fixtures import DEMO_INSTRUMENT, EPOCH_2025_MS, candles_from_closes, series_from_closes
from my_ai_trading.rules import StrategyRule

M, N, I = "MATCH", "NO_MATCH", "INSUFFICIENT_DATA"
NO_COST = dict(fee_bps=0.0, slippage_bps=0.0)


def bars(ohlc, *, skip_after=None, skip=0):
    """Daily candles from (open, high, low, close) tuples; optionally leave a gap after an index."""
    out, t = [], EPOCH_2025_MS
    for i, (o, h, lo, c) in enumerate(ohlc):
        out.append(Candle(t, t + DAY_MS - 1, o, h, lo, c, 1000.0))
        t += DAY_MS * (1 + (skip if i == skip_after else 0))
    return CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), tuple(out))


def rule(cond, tf="1d"):
    return StrategyRule.model_validate({"name": "t", "trigger_timeframe": tf, "condition": cond})


def close_vs(op, right, tf="1d"):
    return {"kind": "compare", "left": {"name": "close", "timeframe": tf}, "op": op, "right": right}


FLAT = (100.0, 101.0, 99.0, 100.0)  # true range 2 -> Wilder ATR(14) == 2


def test_entry_fills_at_next_bar_open_not_signal_close():
    s = bars([(100, 101, 99, 100), (100, 103, 99, 102), (110, 112, 108, 111), (111, 112, 109, 110)])
    # Rule matches only on bar 1 (close 102 > 101.5); bar 2 opens at 110, far from the 102 close.
    r = run_backtest(rule(close_vs(">", 101.5)), s, BacktestConfig(hold_bars=1, **NO_COST))
    first = r["trades"][0]
    assert first["signal_idx"] == 1 and first["entry_idx"] == 2
    assert first["entry_open"] == 110 and first["entry_fill"] == 110
    assert first["exit_kind"] == "hold" and first["exit_raw"] == 111
    assert first["gross_return"] == pytest.approx(111 / 110 - 1)
    assert r["label"] == LABEL


def test_single_trade_reproduces_hand_computed_costs():
    s = bars([FLAT, (100, 111, 99, 110), FLAT])
    cfg = BacktestConfig(hold_bars=1, fee_bps=5, slippage_bps=2, funding_rate_per_8h=0.0001)
    t = simulate(s, [M, N, N], cfg)["trades"][0]
    # entry 100*(1+0.0002)=100.02; exit 110*(1-0.0002)=109.978; one daily bar = 3 funding periods.
    assert t["entry_fill"] == pytest.approx(100.02) and t["exit_fill"] == pytest.approx(109.978)
    assert t["gross_return"] == pytest.approx(0.10)
    assert t["slippage_cost"] == pytest.approx(0.10 - 9.958 / 100.02)
    assert t["fee_cost"] == pytest.approx(0.0005 * (1 + 109.978 / 100.02))
    assert t["funding_cost"] == pytest.approx(0.0003)
    assert t["net_return"] == pytest.approx(0.0982103080, abs=1e-9)
    # Short pays the adverse move, receives positive funding.
    short = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, direction="short", fee_bps=5,
                                                  slippage_bps=2, funding_rate_per_8h=0.0001))["trades"][0]
    ef, xf = 100 * 0.9998, 110 * 1.0002
    assert short["funding_cost"] == pytest.approx(-0.0003)
    assert short["net_return"] == pytest.approx(-(xf - ef) / ef - 0.0005 * (1 + xf / ef) + 0.0003)
    m = simulate(s, [M, N, N], cfg)["metrics"]
    assert m["costs_paid"]["total"] == pytest.approx(t["slippage_cost"] + t["fee_cost"] + 0.0003)
    assert m["compounded_net_return"] == pytest.approx(t["net_return"])
    assert m["exposure"] == pytest.approx(1 / 3)


def test_stop_and_time_exit_in_same_bar_resolves_to_stop():
    ohlc = [FLAT] * 20 + [(100, 106, 97, 105), FLAT]
    sig = [N] * 19 + [M, N, N]
    cfg = BacktestConfig(hold_bars=1, stop_atr_mult=1.0, **NO_COST)
    t = simulate(bars(ohlc), sig, cfg)["trades"][0]
    assert t["atr_at_signal"] == pytest.approx(2.0)
    assert t["stop_level"] == pytest.approx(98.0)
    # Bar 20 is the scheduled exit bar (close 105) AND trades through the stop: stop wins.
    assert t["exit_idx"] == 20 and t["exit_kind"] == "stop" and t["exit_raw"] == pytest.approx(98.0)
    assert t["net_return"] == pytest.approx(-0.02)


def test_stop_gap_through_fills_at_worse_open_and_short_stop_uses_high():
    ohlc = [FLAT] * 20 + [(100, 101, 99, 100), (90, 91, 89, 90), FLAT]
    sig = [N] * 19 + [M, N, N, N]
    t = simulate(bars(ohlc), sig, BacktestConfig(hold_bars=3, stop_atr_mult=1.0, **NO_COST))["trades"][0]
    assert t["exit_kind"] == "stop" and t["exit_idx"] == 21 and t["exit_raw"] == 90
    ohlc = [FLAT] * 20 + [(100, 103, 99, 100), FLAT]
    t = simulate(bars(ohlc), [N] * 19 + [M, N, N],
                 BacktestConfig(hold_bars=2, direction="short", stop_atr_mult=1.0, **NO_COST))["trades"][0]
    assert t["stop_level"] == pytest.approx(102.0) and t["exit_kind"] == "stop"
    assert t["exit_raw"] == pytest.approx(102.0) and t["net_return"] == pytest.approx(-0.02)


def test_stop_without_enough_history_for_atr_skips_the_signal():
    r = simulate(bars([FLAT] * 5), [M, N, N, N, N], BacktestConfig(hold_bars=1, stop_atr_mult=2.0))
    assert r["trades"] == [] and r["signal_counts"]["skipped_no_atr"] == 1


def test_exit_on_rule_false_exits_at_next_open():
    s = bars([FLAT, (100, 101, 99, 100), (100, 103, 99, 102), (104, 105, 103, 104), FLAT])
    cfg = BacktestConfig(hold_bars=4, exit_on_rule_false=True, **NO_COST)
    t = simulate(s, [M, M, N, M, M], cfg)["trades"][0]
    assert t["exit_kind"] == "rule_false" and t["exit_idx"] == 3 and t["exit_raw"] == 104


def test_insufficient_data_never_trades():
    s = series_from_closes([1.0 + 0.01 * i for i in range(60)])
    assert simulate(s, [I] * 60, BacktestConfig(hold_bars=1))["trades"] == []
    r = run_backtest(rule(close_vs(">", {"name": "sma", "timeframe": "1d", "period": 200})), s,
                     BacktestConfig(hold_bars=1))
    assert r["trades"] == [] and r["signal_counts"]["insufficient_data"] == 60
    short = series_from_closes([1.0] * 15 + [2.0], volumes=[1.0] * 15 + [9.0])
    assert set(compute_signals("BREAKOUT", short)) == {I}
    assert set(compute_signals("SMA200_RECLAIM", series_from_closes([1.0] * 30))) == {I}


def test_gaps_are_reported_and_never_spanned():
    # Gap of 3 missing days after bar 4.
    s = bars([FLAT] * 10, skip_after=4, skip=3)
    cfg = BacktestConfig(hold_bars=5, **NO_COST)
    r = run_backtest("BREAKOUT", s, cfg, signals=[N, N, M, N, N, N, N, N, N, N])
    assert r["gaps"] and r["gaps"][0]["missing_bars"] == 3
    t = r["trades"][0]
    assert t["entry_idx"] == 3 and t["exit_idx"] == 4 and t["exit_kind"] == "gap"
    # A signal on the last bar before the gap is skipped (its entry bar would be stale).
    r = simulate(s, [N, N, N, N, M, N, N, N, N, N], cfg)
    assert r["trades"] == [] and r["signal_counts"]["skipped_gap_before_entry"] == 1


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=st.lists(st.floats(-0.06, 0.06), min_size=40, max_size=70),
       cut=st.integers(10, 60), hold=st.integers(1, 4), short=st.booleans(), rule_exit=st.booleans(),
       stop=st.sampled_from([None, 1.0, 2.5]))
def test_no_look_ahead_truncation_does_not_change_earlier_trades(steps, cut, hold, short, rule_exit, stop):
    """Trades exiting at bar <= k, the equity marks and signals up to k are identical whether or not
    bars after k exist (all exit kinds: hold, stop and rule_false are exercised)."""
    closes = [100.0]
    for x in steps:
        closes.append(closes[-1] * (1 + x))
    vols = [1000.0 * (1 + 3 * (i % 5 == 0)) for i in range(len(closes))]
    full = series_from_closes(closes, volumes=vols, spread=0.02)
    k = min(cut, len(closes) - 1)
    part = CandleSeries(full.instrument, full.timeframe, full.candles[:k + 1])
    r = rule(close_vs(">", {"name": "sma", "timeframe": "1d", "period": 5}))
    cfg = BacktestConfig(hold_bars=hold, direction="short" if short else "long", stop_atr_mult=stop,
                         exit_on_rule_false=rule_exit, funding_rate_per_8h=0.0001)
    sig_full, sig_part = compute_signals(r, full), compute_signals(r, part)
    assert sig_part == sig_full[:k + 1]
    a, b = run_backtest(r, full, cfg), run_backtest(r, part, cfg)
    assert [t for t in a["trades"] if t["exit_idx"] <= k] == b["trades"]
    assert a["equity_curve"][:k + 1] == pytest.approx(b["equity_curve"])
    ea, eb = compute_signals("UNUSUAL_VOLUME", full), compute_signals("UNUSUAL_VOLUME", part)
    assert eb == ea[:k + 1]


def test_walk_forward_folds_are_chronological_non_overlapping_and_embargoed():
    closes = [100 * (1 + 0.05 * math.sin(i / 6)) for i in range(250)]
    s = series_from_closes(closes)
    r_ = rule(close_vs("crosses_above", {"name": "sma", "timeframe": "1d", "period": 10}))
    cfg = BacktestConfig(hold_bars=5)
    wf = walk_forward(r_, s, cfg, n_folds=4)
    assert wf["label"] == LABEL and wf["cost_assumptions"]["fee_bps"] == 5.0
    folds = wf["folds"]
    assert len(folds) == 4
    prev_end = None
    for f in folds:
        (is0, is1), (e0, e1), (t0, t1) = f["in_sample"], f["embargo"], f["test"]
        assert is0 == 0 and is1 == e0 and e1 == t0 and t0 < t1
        assert t0 - is1 >= cfg.hold_bars
        if prev_end is not None:
            assert t0 >= prev_end + cfg.hold_bars
        prev_end = t1
        for t in f["trades"]:
            assert t0 <= t["signal_idx"] and t["exit_idx"] < t1
    assert folds[-1]["test"][1] == len(closes)
    assert wf["stability"]["std"] >= 0 and len(wf["stability"]["fold_net_returns"]) == 4
    with pytest.raises(ValueError):
        walk_forward(r_, s, cfg, embargo_bars=2)


def test_config_and_source_validation():
    for bad in (dict(hold_bars=0), dict(hold_bars=True), dict(hold_bars=1, direction="up"),
                dict(hold_bars=1, fee_bps=-1), dict(hold_bars=1, stop_atr_mult=0),
                dict(hold_bars=1, funding_rate_per_8h=float("nan"))):
        with pytest.raises(ValueError):
            BacktestConfig(**bad)
    s = series_from_closes([1.0] * 30)
    with pytest.raises(ValueError):
        compute_signals("BREAKOUTT", s)
    with pytest.raises(ValueError):
        compute_signals(rule(close_vs(">", 1.0), tf="4h"), s)
    multi = rule({"kind": "all", "items": [close_vs(">", 1.0), {"kind": "trend", "timeframe": "1w", "state": "UP"}]})
    with pytest.raises(ValueError, match="1w"):
        compute_signals(multi, s)


def test_metrics_drawdown_and_counts():
    s = bars([FLAT, (100, 101, 89, 90), (90, 96, 89, 95), FLAT, (100, 106, 99, 105), (105, 111, 104, 110)])
    r = simulate(s, [M, N, N, M, N, N], BacktestConfig(hold_bars=2, **NO_COST))
    m = r["metrics"]
    assert m["n_trades"] == 2
    assert [t["net_return"] for t in r["trades"]] == pytest.approx([-0.05, 0.10])
    assert m["win_rate"] == 0.5 and m["avg_win"] == pytest.approx(0.10) and m["avg_loss"] == pytest.approx(-0.05)
    assert m["expectancy_net_per_trade"] == pytest.approx(0.025)
    assert m["compounded_net_return"] == pytest.approx(0.95 * 1.10 - 1)
    assert m["max_drawdown"] == pytest.approx(0.10)  # marked at bar 1 close (90)
    assert m["exposure"] == pytest.approx(4 / 6)
    assert r["open_at_end"] is None
    assert "not a probability" in m["note"]
