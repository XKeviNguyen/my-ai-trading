"""Feature 5 of the trading-playbook spec: plan stops/targets, trailing exits, fixed-risk sizing, playbook source."""
import pytest

from my_ai_trading.backtest import BacktestConfig, PlaybookSource, run_backtest, simulate, walk_forward
from my_ai_trading.domain import DAY_MS, Candle, CandleSeries, Timeframe
from my_ai_trading.fixtures import DEMO_INSTRUMENT, EPOCH_2025_MS, series_from_closes

M, N = "MATCH", "NO_MATCH"
NO_COST = dict(fee_bps=0.0, slippage_bps=0.0)
FLAT = (100.0, 101.0, 99.0, 100.0)


def bars(ohlc):
    out, t = [], EPOCH_2025_MS
    for o, h, lo, c in ohlc:
        out.append(Candle(t, t + DAY_MS - 1, o, h, lo, c, 1000.0))
        t += DAY_MS
    return CandleSeries(DEMO_INSTRUMENT, Timeframe("1d"), tuple(out))


def test_plan_stop_replaces_atr_and_target_exits_at_target_price():
    s = bars([FLAT, (100, 104, 99.5, 103), (103, 112, 102, 111), FLAT])
    plans = [{"stop": 95.0, "target": 110.0}, None, None, None]
    t = simulate(s, [M, N, N, N], BacktestConfig(hold_bars=3, stop_atr_mult=2.0, **NO_COST), plans=plans)["trades"][0]
    assert t["stop_level"] == 95.0 and t["atr_at_signal"] is None
    assert (t["exit_kind"], t["exit_raw"], t["exit_idx"]) == ("target", 110.0, 2)


def test_stop_before_target_in_the_same_bar():
    s = bars([FLAT, (100, 111, 94, 100), FLAT])
    t = simulate(s, [M, N, N], BacktestConfig(hold_bars=2, **NO_COST),
                 plans=[{"stop": 95.0, "target": 110.0}, None, None])["trades"][0]
    assert (t["exit_kind"], t["exit_raw"]) == ("stop", 95.0)


def test_signal_skipped_when_next_open_is_beyond_plan_stop():
    s = bars([FLAT, (94, 96, 93, 95), FLAT])
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, **NO_COST), plans=[{"stop": 95.0}, None, None])
    assert r["trades"] == [] and r["signal_counts"]["skipped_stop_beyond_entry"] == 1


def test_trailing_three_day_exit():
    s = bars([FLAT,
              (100, 101, 99, 100.5),   # 1 entry bar (peak 101, low 99)
              (101, 105, 101, 104),    # 2 new peak (low 101)
              (104, 104.5, 100, 100.5),  # 3 setup: close < 101; trigger = low 100 from bar 4 on
              (100.5, 101, 99.5, 100),   # 4 trades below 100 -> exit at 100
              FLAT])
    t = simulate(s, [M, N, N, N, N, N], BacktestConfig(hold_bars=5, exit_policy="trailing_three_day", **NO_COST))
    trade = t["trades"][0]
    assert (trade["exit_kind"], trade["exit_raw"], trade["exit_idx"]) == ("trailing_stop", 100, 4)


def test_trailing_setup_bar_itself_cannot_trigger():
    # The setup bar's own low is below the trigger level only from the NEXT bar on (known at its close).
    s = bars([FLAT, (100, 101, 99, 100.5), (101, 105, 101, 104), (104, 104.5, 100, 100.5), FLAT])
    t = simulate(s, [M, N, N, N, N], BacktestConfig(hold_bars=3, exit_policy="trailing_three_day", **NO_COST))
    assert t["trades"][0]["exit_kind"] == "hold"


def test_fixed_risk_sizing_scales_equity_and_reports_r():
    s = bars([FLAT, (100, 101, 89, 90), FLAT])  # stopped at 95: -5% with a 5% stop distance = -1R
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, sizing="fixed_risk", risk_fraction=0.01, **NO_COST),
                 plans=[{"stop": 95.0}, None, None])
    t = r["trades"][0]
    assert t["fraction"] == pytest.approx(0.2)  # 1% risk / 5% stop distance
    assert t["r_multiple"] == pytest.approx(-1.0)
    assert r["metrics"]["compounded_net_return"] == pytest.approx(-0.01)
    assert r["metrics"]["avg_r"] == pytest.approx(-1.0)
    assert r["metrics"]["max_consecutive_losses"] == 1


def test_fixed_risk_without_a_stop_fails_closed():
    s = bars([FLAT, FLAT, FLAT])
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, sizing="fixed_risk", **NO_COST))
    assert r["trades"] == [] and r["signal_counts"]["skipped_no_stop"] == 1


def test_fixed_risk_never_levers_up():
    s = bars([FLAT, (100, 101, 99.9, 100), FLAT])
    t = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, sizing="fixed_risk", risk_fraction=0.02, **NO_COST),
                 plans=[{"stop": 99.95}, None, None])["trades"][0]
    assert t["fraction"] == 1.0


@pytest.mark.parametrize("kwargs", [{"exit_policy": "moon"}, {"sizing": "all_in"}, {"risk_fraction": 0.03},
                                    {"risk_fraction": 0.0}, {"risk_fraction": float("nan")}])
def test_new_config_fields_validate(kwargs):
    with pytest.raises(ValueError):
        BacktestConfig(hold_bars=1, **kwargs)


def _rectangle_series(extra=()):
    pts = [(0, 100), (6, 120), (12, 100), (18, 120), (24, 100), (30, 120), (36, 100), (42, 120), (48, 100),
           (52, 110), (53, 130), (60, 140)] + list(extra)
    closes = [100.0] * 30
    for (i0, p0), (i1, p1) in zip(pts, pts[1:]):
        closes.extend(p0 + (p1 - p0) * (i - i0) / (i1 - i0) for i in range(i0, i1))
    closes.append(pts[-1][1])
    vols = [1e6] * len(closes)
    vols[30 + 53] = 3e6
    return series_from_closes(closes, volumes=vols)


def test_playbook_source_trades_a_completed_rectangle_with_ldr_stop_and_target():
    s = _rectangle_series()
    r = run_backtest(PlaybookSource("brandt"), s, BacktestConfig(hold_bars=5, sizing="fixed_risk", **NO_COST))
    assert r["source"]["kind"] == "playbook" and r["source"]["profile"] == "brandt"
    t = r["trades"][0]
    assert t["plan"]["pattern"] == "RECTANGLE"
    assert t["signal_idx"] == 30 + 53
    bar = s.candles[30 + 53]
    assert t["stop_level"] in (bar.low, s.candles[30 + 52].low, s.candles[30 + 52].close)
    assert t["plan"]["target"] == pytest.approx(t["plan"]["level"] + (t["plan"]["level"] - min(
        c.low for c in s.candles[30 + 6:30 + 50])), rel=0.05)
    assert "r_multiple" in t


def test_playbook_signals_never_depend_on_later_bars():
    s = _rectangle_series()
    full = run_backtest(PlaybookSource("brandt"), s, BacktestConfig(hold_bars=3, **NO_COST))
    cut = CandleSeries(s.instrument, s.timeframe, s.candles[:30 + 58])
    part = run_backtest(PlaybookSource("brandt"), cut, BacktestConfig(hold_bars=3, **NO_COST))
    assert part["trades"] == [t for t in full["trades"] if t["exit_idx"] < 30 + 58]


def test_playbook_walk_forward_and_unknown_profile():
    s = series_from_closes([100 + (i % 40) for i in range(400)])
    wf = walk_forward(PlaybookSource("edwards_magee"), s, BacktestConfig(hold_bars=5, **NO_COST), n_folds=3)
    assert len(wf["folds"]) == 3
    with pytest.raises(ValueError):
        PlaybookSource("nobody")


def test_backtest_trailing_exit_matches_the_stop_library():
    from my_ai_trading.stops import trailing_three_day
    rows = [FLAT, (100, 101, 99, 100.5), (100.5, 105, 100, 104), (104, 104.5, 98, 99.5), (102, 110, 101.5, 109),
            (109, 109.8, 97.5, 109.5), (109.5, 112, 109, 111), (111, 111.5, 105, 106), (106, 107, 104, 104.5),
            FLAT]
    s = bars(rows)
    sig = [M] + [N] * (len(rows) - 1)
    t = simulate(s, sig, BacktestConfig(hold_bars=8, exit_policy="trailing_three_day", **NO_COST))["trades"][0]
    lib = trailing_three_day(s.candles, 1, "long")
    assert t["exit_kind"] == "trailing_stop" and t["exit_idx"] == lib["triggered_bar_index"]
    assert t["exit_raw"] == lib["trigger"]["price"]


def test_zero_atr_stop_is_skipped_not_a_crash():
    s = bars([(100, 100, 100, 100)] * 30)
    r = simulate(s, [N] * 20 + [M] + [N] * 9,
                 BacktestConfig(hold_bars=2, sizing="fixed_risk", stop_atr_mult=2.0, **NO_COST))
    assert r["trades"] == [] and r["signal_counts"]["skipped_no_atr"] == 1


def test_fixed_risk_loss_at_stop_stays_within_risk_fraction_including_costs():
    s = bars([FLAT, (100, 101, 89, 90), FLAT])
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, sizing="fixed_risk", risk_fraction=0.02,
                                              fee_bps=10, slippage_bps=5), plans=[{"stop": 95.0}, None, None])
    assert -0.02 - 1e-12 <= r["metrics"]["compounded_net_return"] < 0
    assert r["metrics"]["compounded_gross_return"] >= r["metrics"]["compounded_net_return"]


def test_precomputed_signals_are_rejected_for_a_playbook_source():
    with pytest.raises(ValueError):
        run_backtest(PlaybookSource("brandt"), bars([FLAT] * 3), BacktestConfig(hold_bars=1), signals=[N, N, N])


def test_robustness_reports_cost_stress_baseline_and_sample_warning():
    s = _rectangle_series()
    r = run_backtest(PlaybookSource("brandt"), s, BacktestConfig(hold_bars=5, fee_bps=5, slippage_bps=2))
    rb = r["robustness"]
    assert rb["zero_cost_return"] >= rb["compounded_net_return"] >= rb["double_cost_return"]
    assert rb["hold_window_return"] == pytest.approx((s.candles[-1].close - s.candles[0].open) / s.candles[0].open
                                                     - 2 * 5 / 1e4)
    assert any("too few" in w for w in rb["warnings"])
    assert rb["win_rate_ci95"] is not None and 0 <= rb["win_rate_ci95"][0] <= rb["win_rate_ci95"][1] <= 1


def test_robustness_flags_an_edge_smaller_than_costs():
    s = bars([FLAT, (100, 100.2, 99.9, 100.1), FLAT, (100, 100.2, 99.9, 100.1), FLAT])
    from my_ai_trading.backtest import robustness
    cfg = BacktestConfig(hold_bars=1, fee_bps=20, slippage_bps=5)
    sig = [M, N, M, N, N]
    res = simulate(s, sig, cfg)
    rb = robustness(s, sig, cfg, res)
    assert rb["zero_cost_return"] > 0 >= rb["compounded_net_return"]
    assert any("smaller than the costs" in w for w in rb["warnings"])


def test_target_already_passed_at_the_next_open_is_skipped_not_booked_as_a_win():
    # Breaker round 2: a gap past the target "filled" at a price the bar never traded and booked a loss as target.
    s = bars([FLAT, (106, 107, 105.5, 106.5), FLAT])
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, **NO_COST), plans=[{"stop": 95.0, "target": 104.0}, None, None])
    assert r["trades"] == [] and r["signal_counts"]["skipped_target_beyond_entry"] == 1


def test_fixed_risk_short_stop_loss_stays_within_budget():
    s = bars([FLAT, (100, 111, 99, 110), FLAT])
    r = simulate(s, [M, N, N], BacktestConfig(hold_bars=1, direction="short", sizing="fixed_risk", risk_fraction=0.01,
                                              fee_bps=10, slippage_bps=10), plans=[{"stop": 105.0}, None, None])
    assert -0.01 - 1e-12 <= r["metrics"]["compounded_net_return"] < 0
