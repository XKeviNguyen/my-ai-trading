"""No-look-ahead research backtester (PRD F10). Output is HYPOTHETICAL, never live performance.

Timing model (the property every test guards):
- A signal for bar ``t`` is computed only from bars CLOSED at ``as_of = candles[t].close_time_ms + 1``
  (``rules.evaluate`` / ``engine.analyze`` on ``series.up_to(as_of)``). It never sees bar t+1.
- Entry fills at the OPEN of bar t+1, worsened by slippage. Bar t+1 counts as held bar 1; the
  time exit fills at the CLOSE of bar ``t + hold_bars`` (worsened by slippage).
- Optional stop: ``stop_atr_mult`` x Wilder ATR(14) computed from bars closed at t (known at entry),
  measured from the entry fill. It is checked on every held bar from t+1 using the intrabar
  high/low. If the stop and the time exit fall in the same bar, the STOP is assumed to fill first
  (the worse outcome). A bar that opens beyond the stop fills at its open (gap-through).
- Optional ``exit_on_rule_false``: an explicit NO_MATCH on a held bar's close exits at the OPEN of
  the next bar (decision at close, fill on the next tradable price, consistent with entries).
  INSUFFICIENT_DATA while held does not force an exit.

Sizing and costs (fixed choices, no options):
- Equity starts at 1.0; each trade uses 100% of current equity as notional, no leverage, one
  position at a time. Equity therefore COMPOUNDS trade to trade; the non-compounded sum of per-trade
  net returns is reported alongside.
- ``fee_bps`` (taker) is charged per side on traded notional; ``slippage_bps`` worsens each fill.
- Funding (perpetuals): ``funding_rate_per_8h`` x holding time / 8h on entry notional, pro rata.
  Longs pay positive funding, shorts receive it. Holding time runs from the entry bar's open to the
  exit bar's close (stops are charged to the end of the stop bar; rule exits to the exit bar open).

Fail-closed data rules:
- INSUFFICIENT_DATA signals never open trades; a stop requested without an ATR skips the signal.
- Gaps (``CandleSeries.gaps``) are reported. A signal whose next bar lies after a gap is skipped
  (the entry would be stale). An open trade is CLOSED at the close of the last bar before a gap
  (exit kind ``gap``) so no trade spans missing data. This is causal: the decision uses only the
  gap and bars already present; nothing skips a signal because of a gap further in the future.
- A trade still open at the end of the data (or fold) is reported as ``open_at_end`` and excluded
  from metrics; it keeps blocking new entries, so truncating the data never changes earlier trades.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from math import isfinite, prod
from typing import Any, Literal, Mapping, Sequence

from .domain import CandleSeries
from .engine import CONTRACTION_BASE, CONTRACTION_RECENT, PIVOT_LOOKBACK, RECLAIM_PERIODS, analyze, utc_iso
from .indicators import atr_wilder
from .rules import StrategyRule, evaluate

BACKTEST_VERSION = "backtest/0.1.0"
LABEL = "HYPOTHETICAL — research backtest, not live or audited performance"
MATCH, NO_MATCH, INSUFFICIENT_DATA = "MATCH", "NO_MATCH", "INSUFFICIENT_DATA"
EIGHT_HOURS_MS = 8 * 3_600_000
ATR_PERIOD = 14
ENGINE_EVENT_TYPES = frozenset(
    {"BREAKOUT", "BREAKDOWN", "FAILED_BREAKOUT", "FAILED_BREAKDOWN", "UNUSUAL_VOLUME",
     "VOLATILITY_CONTRACTION"}
    | {f"SMA{n}_{k}" for n in RECLAIM_PERIODS for k in ("RECLAIM", "LOSS")})
METRICS_NOTE = ("Per-trade statistics of a hypothetical replay; win rate is a historical frequency in this "
                "sample, not a probability of future wins. No annualised ratios are reported.")

Source = StrategyRule | str  # a typed rule, or an engine event type such as "BREAKOUT"


def _finite(name: str, v: Any, *, minimum: float | None = None) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and v < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


@dataclass(frozen=True)
class BacktestConfig:
    hold_bars: int
    direction: Literal["long", "short"] = "long"
    fee_bps: float = 5.0  # taker, per side
    slippage_bps: float = 2.0  # per fill
    funding_rate_per_8h: float = 0.0  # e.g. 0.0001 = 0.01% per 8h; longs pay when positive
    stop_atr_mult: float | None = None
    exit_on_rule_false: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.hold_bars, bool) or not isinstance(self.hold_bars, int) or self.hold_bars < 1:
            raise ValueError("hold_bars must be an integer >= 1")
        if self.direction not in ("long", "short"):
            raise ValueError("direction must be 'long' or 'short'")
        _finite("fee_bps", self.fee_bps, minimum=0)
        _finite("slippage_bps", self.slippage_bps, minimum=0)
        _finite("funding_rate_per_8h", self.funding_rate_per_8h)
        if self.stop_atr_mult is not None:
            _finite("stop_atr_mult", self.stop_atr_mult)
            if self.stop_atr_mult <= 0:
                raise ValueError("stop_atr_mult must be > 0")
        if not isinstance(self.exit_on_rule_false, bool):
            raise ValueError("exit_on_rule_false must be a bool")

    def assumptions(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "initial_equity": 1.0,
            "position_size": "100% of current equity notional, no leverage, one position at a time",
            "compounding": "equity compounds trade to trade; sum of per-trade net returns also reported",
            "entry": "open of the bar after the signal bar, worsened by slippage",
            "exit": "close of bar signal+hold_bars; stop at stop price (or worse open); rule-false exit at next open",
            "same_bar_stop_and_exit": "stop assumed first (worse outcome)",
            "stop_reference": f"entry fill -/+ stop_atr_mult x Wilder ATR({ATR_PERIOD}) of bars closed at the signal",
            "funding": "rate x hours held / 8 on entry notional; longs pay positive funding, shorts receive it",
            "gaps": "signals followed by a gap are skipped; open trades close at the last bar before a gap",
        }


# ---------- signals ----------

def _event_state(series: CandleSeries, event_type: str, as_of: int) -> str:
    rep = analyze(series.up_to(as_of), as_of_ms=as_of)
    if rep["status"] != "OK":
        return INSUFFICIENT_DATA
    n = rep["data_quality"]["contiguous_bars"]
    need = {"FAILED_BREAKOUT": PIVOT_LOOKBACK + 2, "FAILED_BREAKDOWN": PIVOT_LOOKBACK + 2,
            "VOLATILITY_CONTRACTION": CONTRACTION_RECENT + CONTRACTION_BASE + 1}.get(event_type, 0)
    if event_type.startswith("SMA"):
        need = int(event_type[3:].split("_")[0]) + 1  # needs the SMA on the previous bar too
    if n < need:
        return INSUFFICIENT_DATA
    return MATCH if any(e["type"] == event_type for e in rep["events"]) else NO_MATCH


def compute_signals(source: Source, series: CandleSeries, *,
                    other_series: Mapping[str, CandleSeries] | None = None) -> list[str]:
    """One state (MATCH / NO_MATCH / INSUFFICIENT_DATA) per bar of ``series``, each computed from
    bars closed at that bar's close only. ``other_series`` supplies extra rule timeframes."""
    if isinstance(source, StrategyRule):
        if source.trigger_timeframe != series.timeframe.code:
            raise ValueError("series timeframe must equal the rule's trigger_timeframe")
        by_tf = {**(other_series or {}), series.timeframe.code: series}
        missing = source.timeframes() - set(by_tf)
        if missing:
            raise ValueError(f"missing series for timeframe(s): {sorted(missing)}")
        return [evaluate(source, by_tf.__getitem__, as_of_ms=c.close_time_ms + 1)["result"]
                for c in series.candles]
    if isinstance(source, str):
        if source not in ENGINE_EVENT_TYPES:
            raise ValueError(f"unknown engine event type {source!r}")
        return [_event_state(series, source, c.close_time_ms + 1) for c in series.candles]
    raise TypeError("source must be a StrategyRule or an engine event type string")


# ---------- simulation ----------

def _trade_returns(sign: int, entry_open: float, exit_raw: float, entry_ms: int, exit_ms: int,
                   cfg: BacktestConfig) -> dict[str, float]:
    s, f = cfg.slippage_bps / 1e4, cfg.fee_bps / 1e4
    entry_fill = entry_open * (1 + sign * s)
    exit_fill = exit_raw * (1 - sign * s)
    gross = sign * (exit_raw - entry_open) / entry_open
    after_slip = sign * (exit_fill - entry_fill) / entry_fill
    fee = f * (1 + exit_fill / entry_fill)
    funding = sign * cfg.funding_rate_per_8h * (exit_ms - entry_ms) / EIGHT_HOURS_MS
    return {"entry_fill": entry_fill, "exit_fill": exit_fill, "gross_return": gross,
            "slippage_cost": gross - after_slip, "fee_cost": fee, "funding_cost": funding,
            "net_return": after_slip - fee - funding}


def simulate(series: CandleSeries, signals: Sequence[str], config: BacktestConfig, *,
             start: int = 0, end: int | None = None) -> dict[str, Any]:
    """Replay ``signals`` over bars ``[start, end)``; no trade bar may lie outside the window."""
    candles = series.candles
    n = len(candles)
    end = n if end is None else end
    if len(signals) != n:
        raise ValueError("signals must have one entry per candle")
    if not 0 <= start <= end <= n:
        raise ValueError("invalid window")
    cfg = config
    sign = 1 if cfg.direction == "long" else -1
    f = cfg.fee_bps / 1e4

    def adjacent(j: int) -> bool:  # bar j+1 exists in the window and directly follows bar j
        return j + 1 < end and candles[j + 1].open_time_ms == series.timeframe.next_open_ms(candles[j].open_time_ms)

    counts = {"match": 0, "no_match": 0, "insufficient_data": 0, "ignored_in_position": 0,
              "skipped_gap_before_entry": 0, "skipped_no_atr": 0, "no_next_bar": 0}
    trades: list[dict[str, Any]] = []
    curve: list[float] = []
    equity, bars_in_pos = 1.0, 0
    pos: dict[str, Any] | None = None

    def close(j: int, kind: str, raw: float, exit_ms: int) -> None:
        nonlocal equity, pos
        assert pos is not None
        r = _trade_returns(sign, pos["entry_open"], raw, pos["entry_ms"], exit_ms, cfg)
        equity *= 1 + r["net_return"]
        trades.append({**{k: v for k, v in pos.items() if k not in ("entry_ms", "pending_rule_exit")},
                       "exit_idx": j, "exit_kind": kind, "exit_raw": raw,
                       "exit_time_utc": utc_iso(exit_ms), "bars_held": j - pos["entry_idx"] + 1, **r})
        pos = None

    for j in range(start, end):
        c = candles[j]
        if pos is not None and pos["entry_idx"] <= j:
            bars_in_pos += 1
            stop = pos["stop_level"]
            if pos["pending_rule_exit"]:
                close(j, "rule_false", c.open, c.open_time_ms)
            elif stop is not None and sign == 1 and c.low <= stop:
                close(j, "stop", min(stop, c.open), c.close_time_ms + 1)
            elif stop is not None and sign == -1 and c.high >= stop:
                close(j, "stop", max(stop, c.open), c.close_time_ms + 1)
            elif j == pos["scheduled_exit_idx"]:
                close(j, "hold", c.close, c.close_time_ms + 1)
            elif j + 1 < end and not adjacent(j):
                close(j, "gap", c.close, c.close_time_ms + 1)
            elif cfg.exit_on_rule_false and signals[j] == NO_MATCH:
                pos["pending_rule_exit"] = True
        if pos is not None and pos["entry_idx"] <= j:
            unreal = (sign * (c.close - pos["entry_fill"]) / pos["entry_fill"] - f
                      - sign * cfg.funding_rate_per_8h * (c.close_time_ms + 1 - pos["entry_ms"]) / EIGHT_HOURS_MS)
            curve.append(pos["equity_before"] * (1 + unreal))
        else:
            curve.append(equity)

        state = signals[j]
        if state == INSUFFICIENT_DATA:
            counts["insufficient_data"] += 1
            continue
        if state != MATCH:
            counts["no_match"] += 1
            continue
        counts["match"] += 1
        if pos is not None:
            counts["ignored_in_position"] += 1
        elif j + 1 >= end:
            counts["no_next_bar"] += 1
        elif not adjacent(j):
            counts["skipped_gap_before_entry"] += 1
        else:
            atr = None
            if cfg.stop_atr_mult is not None:
                atr = atr_wilder(series.up_to(c.close_time_ms + 1).contiguous_tail(), ATR_PERIOD)
                if atr is None:
                    counts["skipped_no_atr"] += 1
                    continue
            nxt = candles[j + 1]
            entry_fill = nxt.open * (1 + sign * cfg.slippage_bps / 1e4)
            pos = {"signal_idx": j, "signal_bar_close_utc": utc_iso(c.close_time_ms + 1),
                   "entry_idx": j + 1, "entry_time_utc": utc_iso(nxt.open_time_ms),
                   "entry_ms": nxt.open_time_ms, "entry_open": nxt.open, "entry_fill": entry_fill,
                   "scheduled_exit_idx": j + cfg.hold_bars, "atr_at_signal": atr,
                   "stop_level": None if atr is None else entry_fill - sign * cfg.stop_atr_mult * atr,
                   "equity_before": equity, "pending_rule_exit": False}

    open_at_end = None
    if pos is not None:
        open_at_end = {k: v for k, v in pos.items() if k not in ("entry_ms", "pending_rule_exit")}
    return {"trades": trades, "open_at_end": open_at_end, "signal_counts": counts,
            "equity_curve": curve, "metrics": metrics(trades, curve, bars_in_pos, end - start)}


def _max_drawdown(curve: Sequence[float]) -> float:
    peak, mdd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    return mdd


def metrics(trades: Sequence[dict[str, Any]], curve: Sequence[float], bars_in_pos: int,
            n_bars: int) -> dict[str, Any]:
    nets = [t["net_return"] for t in trades]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    fees = sum(t["fee_cost"] for t in trades)
    slip = sum(t["slippage_cost"] for t in trades)
    fund = sum(t["funding_cost"] for t in trades)
    return {
        "n_trades": len(trades),
        "sum_gross_return": sum(t["gross_return"] for t in trades),
        "sum_net_return": sum(nets),
        "compounded_gross_return": prod(1 + t["gross_return"] for t in trades) - 1,
        "compounded_net_return": prod(1 + x for x in nets) - 1,
        "win_rate": len(wins) / len(nets) if nets else None,
        "avg_win": statistics.fmean(wins) if wins else None,
        "avg_loss": statistics.fmean(losses) if losses else None,
        "expectancy_net_per_trade": statistics.fmean(nets) if nets else None,
        "max_drawdown": _max_drawdown(curve),
        "exposure": bars_in_pos / n_bars if n_bars else 0.0,
        "costs_paid": {"fees": fees, "slippage": slip, "funding": fund, "total": fees + slip + fund},
        "note": METRICS_NOTE,
    }


def _gap_report(series: CandleSeries) -> list[dict[str, Any]]:
    tf = series.timeframe
    return [{"from_utc": utc_iso(a), "to_utc": utc_iso(b), "missing_bars": tf.bars_between(a, b)} for a, b in series.gaps]


def _header(source: Source, series: CandleSeries, config: BacktestConfig) -> dict[str, Any]:
    src = ({"kind": "rule", "name": source.name, "rule_hash": source.rule_hash()}
           if isinstance(source, StrategyRule) else {"kind": "engine_event", "event_type": source})
    return {"label": LABEL, "backtest_version": BACKTEST_VERSION, "source": src,
            "instrument": series.instrument.as_dict(), "timeframe": series.timeframe.code,
            "bars": len(series.candles), "cost_assumptions": config.assumptions(),
            "gaps": _gap_report(series), "read_only": True}


def run_backtest(source: Source, series: CandleSeries, config: BacktestConfig, *,
                 other_series: Mapping[str, CandleSeries] | None = None,
                 signals: Sequence[str] | None = None) -> dict[str, Any]:
    """Backtest a rule or engine event over ``series``. Precomputed ``signals`` may be passed."""
    sig = list(signals) if signals is not None else compute_signals(source, series, other_series=other_series)
    return {**_header(source, series, config), **simulate(series, sig, config)}


def walk_forward(source: Source, series: CandleSeries, config: BacktestConfig, *, n_folds: int = 4,
                 embargo_bars: int | None = None, other_series: Mapping[str, CandleSeries] | None = None,
                 signals: Sequence[str] | None = None) -> dict[str, Any]:
    """Expanding-window walk-forward. Bars split into ``n_folds + 1`` chronological segments; fold i
    tests segment i after skipping an embargo (>= hold_bars) right after its in-sample end. Rules are
    not fitted here, so in-sample data is unused and folds report per-period metrics and stability.
    Trades must lie entirely inside their fold's test window."""
    embargo = config.hold_bars if embargo_bars is None else embargo_bars
    if isinstance(n_folds, bool) or not isinstance(n_folds, int) or n_folds < 1:
        raise ValueError("n_folds must be an integer >= 1")
    if isinstance(embargo, bool) or not isinstance(embargo, int) or embargo < config.hold_bars:
        raise ValueError("embargo_bars must be an integer >= hold_bars")
    n = len(series.candles)
    seg = n // (n_folds + 1)
    if seg <= embargo:
        raise ValueError("series too short for this many folds and embargo")
    sig = list(signals) if signals is not None else compute_signals(source, series, other_series=other_series)
    candles, folds = series.candles, []
    for i in range(1, n_folds + 1):
        is_end, test_start = i * seg, i * seg + embargo
        test_end = (i + 1) * seg if i < n_folds else n
        res = simulate(series, sig, config, start=test_start, end=test_end)
        folds.append({
            "fold": i, "in_sample": [0, is_end], "embargo": [is_end, test_start], "test": [test_start, test_end],
            "test_from_utc": utc_iso(candles[test_start].open_time_ms),
            "test_to_utc": utc_iso(candles[test_end - 1].close_time_ms + 1),
            "metrics": res["metrics"], "trades": res["trades"], "open_at_end": res["open_at_end"]})
    rets = [f["metrics"]["compounded_net_return"] for f in folds]
    return {**_header(source, series, config), "embargo_bars": embargo, "folds": folds,
            "stability": {"fold_net_returns": rets, "mean": statistics.fmean(rets),
                          "std": statistics.pstdev(rets),
                          "folds_with_trades": sum(1 for f in folds if f["metrics"]["n_trades"])},
            "note": "Index ranges are half-open [start, end) bar indices. Rules are not fitted, so folds "
                    "measure period-to-period stability only."}
