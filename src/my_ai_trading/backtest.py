"""No-look-ahead research backtester (PRD F10). Output is HYPOTHETICAL, never live performance.

Timing model (the property every test guards):
- A signal for bar ``t`` is computed only from bars CLOSED at ``as_of = candles[t].close_time_ms + 1``
  (``rules.evaluate`` / ``engine.analyze`` on ``series.up_to(as_of)``). It never sees bar t+1.
  For speed (PERF-1) each per-bar value is computed once over the whole series with the SAME arithmetic,
  in the same order, as those functions on the truncated series, so results are identical (guarded by
  identity tests against ``rules.evaluate`` and ``engine.analyze``) while the cost is linear in bars.
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
import threading
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from math import isfinite, prod
from typing import Any, Iterator, Literal, Mapping, Sequence

from .domain import Candle, CandleSeries
from .engine import (BREAKOUT_VOLUME_RATIO, CONTRACTION_BASE, CONTRACTION_RATIO, CONTRACTION_RECENT, MIN_BARS,
                     PIVOT_LOOKBACK, RECLAIM_PERIODS, TREND_SLOPE_BARS, UNUSUAL_VOLUME_RATIO, trend_state, utc_iso)
from .indicators import (bollinger, ema_series, prior_high, prior_low, sma, sma_series, true_ranges,
                         volume_ratio)
from .rules import AllOf, AnyOf, MetricRef, Not, StrategyRule, Trend

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

MAX_CONCURRENT_BACKTESTS = 2
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_BACKTESTS)


class BacktestBusyError(RuntimeError):
    """Too many backtests are already running in this process. Not a validation error: retry later
    (the API maps it to HTTP 429). Fails fast instead of queueing, so bursts cannot starve the server."""


@contextmanager
def backtest_slot() -> Iterator[None]:
    """Hold one of ``MAX_CONCURRENT_BACKTESTS`` process-wide slots, or raise ``BacktestBusyError`` now."""
    if not _SLOTS.acquire(blocking=False):
        raise BacktestBusyError(f"{MAX_CONCURRENT_BACKTESTS} backtests already running; retry shortly")
    try:
        yield
    finally:
        _SLOTS.release()


class _Prefix:
    """Per-bar values of one series. Entry ``i`` is computed from the contiguous run of bars ending at bar i
    (``series.up_to(close_i + 1).contiguous_tail()``), with the same indicator functions or the same
    recurrences in the same order, so every value equals the one ``rules`` / ``engine`` compute on the
    truncated series. Recursive indicators (EMA, Wilder ATR/RSI, ADX, MACD) run once per contiguous
    segment; windowed ones call the indicator on the bounded window it reads."""

    def __init__(self, series: CandleSeries):
        self.series = series
        self.candles = series.candles
        self.closes = [c.close for c in self.candles]
        self.close_times = [c.close_time_ms for c in self.candles]
        tf, seg, start = series.timeframe, [], 0
        for i, c in enumerate(self.candles):  # same adjacency test as CandleSeries gap detection
            if i and c.open_time_ms != tf.next_open_ms(self.candles[i - 1].open_time_ms):
                start = i
            seg.append(start)
        self.seg = seg
        starts = sorted(set(seg))
        self.segments = list(zip(starts, starts[1:] + [len(seg)]))  # [start, end) of each contiguous run
        self.seg_end = dict(self.segments)
        self._arrays: dict[tuple[str, int | None], list[Any]] = {}
        self._trend: dict[int, str] = {}

    def view(self, as_of: int) -> int | None:
        """Index of the newest bar closed at ``as_of``, or None when that view is empty or stale."""
        j = bisect_left(self.close_times, as_of) - 1
        if j < 0:
            return None
        last = CandleSeries(self.series.instrument, self.series.timeframe, (self.candles[j],))
        return None if last.is_stale(as_of) else j  # staleness depends only on the newest bar

    def _windowed(self, fn: Any, width: int, *, closes: bool) -> list[Any]:
        src = self.closes if closes else self.candles
        return [fn(src[max(self.seg[i], i - width + 1):i + 1]) for i in range(len(src))]

    def _per_segment(self, fn: Any) -> list[Any]:
        out: list[Any] = [None] * len(self.candles)
        for s, e in self.segments:
            out[s:e] = fn(self.candles[s:e], self.closes[s:e])
        return out

    def array(self, name: str, n: int | None) -> list[Any]:
        key = (name, n)
        if key not in self._arrays:
            self._arrays[key] = self._build(name, n)
        return self._arrays[key]

    def _build(self, name: str, n: int | None) -> list[Any]:
        if name in ("close", "open", "high", "low", "volume"):
            return [getattr(c, name) for c in self.candles]
        assert n is not None or name in ("macd_line", "macd_histogram")
        match name:
            case "sma":
                return self._windowed(lambda w: sma(w, n), n, closes=True)
            case "volume_ratio":
                return self._windowed(lambda w: volume_ratio(w, n), n + 1, closes=False)
            case "prior_high":
                return self._windowed(lambda w: prior_high(w, n), n + 1, closes=False)
            case "prior_low":
                return self._windowed(lambda w: prior_low(w, n), n + 1, closes=False)
            case "bollinger_percent_b":
                def pb(w: list[float]) -> float | None:
                    b = bollinger(w, n)
                    return b and b["percent_b"]
                return self._windowed(pb, n, closes=True)
            case "ema":
                return self._per_segment(lambda _c, cl: ema_series(cl, n))
            case "atr_wilder":
                return self._per_segment(lambda cs, _cl: _atr_wilder_series(cs, n))
            case "rsi_wilder":
                return self._per_segment(lambda _c, cl: _rsi_wilder_series(cl, n))
            case "adx":
                return self._per_segment(lambda cs, _cl: _adx_series(cs, n))
            case "macd_line" | "macd_histogram":
                return self._per_segment(lambda _c, cl: _macd_series(cl, name == "macd_histogram"))
        raise ValueError(f"unsupported metric {name!r}")

    def trend(self, j: int) -> str:
        if j not in self._trend:  # trend_state reads SMA50, SMA200 and SMA50 TREND_SLOPE_BARS bars ago
            w = self.closes[max(self.seg[j], j - (200 + TREND_SLOPE_BARS) + 1):j + 1]
            self._trend[j] = trend_state(w, {50: sma(w, 50), 200: sma(w, 200)})["state"]
        return self._trend[j]


def _atr_wilder_series(cs: Sequence[Candle], n: int) -> list[float | None]:
    """``indicators.atr_wilder`` of every prefix of ``cs`` (same seed and recurrence)."""
    out: list[float | None] = [None] * len(cs)
    tr = true_ranges(cs)
    if len(tr) >= n:
        v = sum(tr[:n]) / n
        out[n] = v
        for k in range(n + 1, len(cs)):
            v = (v * (n - 1) + tr[k - 1]) / n
            out[k] = v
    return out


def _rsi_wilder_series(cl: Sequence[float], n: int) -> list[float | None]:
    """``indicators.rsi_wilder`` of every prefix of ``cl`` (same seed, recurrence and final formula)."""
    out: list[float | None] = [None] * len(cl)
    deltas = [b - a for a, b in zip(cl, cl[1:])]
    if len(deltas) < n:
        return out

    def rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)
    gain = sum(max(d, 0.0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0.0) for d in deltas[:n]) / n
    out[n] = rsi(gain, loss)
    for k in range(n + 1, len(cl)):
        d = deltas[k - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
        out[k] = rsi(gain, loss)
    return out


def _adx_series(cs: Sequence[Candle], n: int) -> list[float | None]:
    """``indicators.adx(...)["adx"]`` of every prefix of ``cs`` (same Wilder sums and smoothing)."""
    out: list[float | None] = [None] * len(cs)
    if len(cs) < 2 * n:
        return out
    tr = true_ranges(cs)
    pdm: list[float] = []
    mdm: list[float] = []
    for p, c in zip(cs, cs[1:]):
        up, down = c.high - p.high, p.low - c.low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)

    def di(dm_s: float, tr_s: float) -> float:
        return 100 * dm_s / tr_s if tr_s > 0 else 0.0

    tr_s, p_s, m_s = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs: list[float] = []  # dxs[q] belongs to true-range index n - 1 + q
    for i in range(n - 1, len(tr)):
        if i >= n:
            tr_s = tr_s - tr_s / n + tr[i]
            p_s = p_s - p_s / n + pdm[i]
            m_s = m_s - m_s / n + mdm[i]
        plus, minus = di(p_s, tr_s), di(m_s, tr_s)
        total = plus + minus
        dxs.append(100 * abs(plus - minus) / total if total > 0 else 0.0)
    v = sum(dxs[:n]) / n
    out[2 * n - 1] = v
    for k in range(2 * n, len(cs)):
        v = (v * (n - 1) + dxs[k - n]) / n
        out[k] = v
    return out


def _macd_series(cl: Sequence[float], histogram: bool, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> list[float | None]:
    """``indicators.macd`` (default periods) line or histogram of every prefix of ``cl``."""
    out: list[float | None] = [None] * len(cl)
    if len(cl) < slow + signal - 1:
        return out
    fs, ss = ema_series(cl, fast), ema_series(cl, slow)
    line = [f - s for f, s in zip(fs[slow - 1:], ss[slow - 1:])]
    sig = ema_series(line, signal)
    for k in range(slow + signal - 2, len(cl)):
        q = k - (slow - 1)
        out[k] = line[q] - sig[q] if histogram else line[q]
    return out


class _FastCtx:
    """Mirror of ``rules._Ctx``: one evaluation instant, with the previous-trigger-close context for crossings."""

    def __init__(self, pre: Mapping[str, _Prefix], as_of: int, trigger: str):
        self.pre, self.as_of, self.trigger = pre, as_of, trigger
        self._views: dict[str, int | None] = {}
        self._previous: _FastCtx | None = None

    def view(self, tf: str) -> int | None:
        if tf not in self._views:
            self._views[tf] = self.pre[tf].view(self.as_of)
        return self._views[tf]

    def previous(self) -> "_FastCtx | None":
        if self._previous is None:
            j, p = self.view(self.trigger), self.pre[self.trigger]
            if j is None or j - p.seg[j] + 1 < 2:
                return None
            self._previous = _FastCtx(self.pre, p.candles[j].open_time_ms, self.trigger)
        return self._previous


def _fast_value(ref: MetricRef | float, ctx: _FastCtx, *, previous: bool = False) -> float | None:
    if not isinstance(ref, MetricRef):
        return float(ref)
    if previous:
        prev = ctx.previous()
        if prev is None:
            return None
        ctx = prev
    j = ctx.view(ref.timeframe)
    if j is None:
        return None
    p = ctx.pre[ref.timeframe]
    if ref.bar == "previous_closed":
        if j - 1 < p.seg[j]:
            return None
        j -= 1
    v = p.array(ref.name, ref.period)[j]
    return None if v is None else float(v)


def _fast_eval(node: Any, ctx: _FastCtx) -> bool | None:
    """Mirror of ``rules._eval`` (three-valued logic) without the human-readable leaves."""
    if isinstance(node, AllOf):
        outcomes = [_fast_eval(i, ctx) for i in node.items]
        return False if False in outcomes else None if None in outcomes else True
    if isinstance(node, AnyOf):
        outcomes = [_fast_eval(i, ctx) for i in node.items]
        return True if True in outcomes else None if None in outcomes else False
    if isinstance(node, Not):
        r = _fast_eval(node.item, ctx)
        return None if r is None else not r
    if isinstance(node, Trend):
        j = ctx.view(node.timeframe)
        state = "UNKNOWN" if j is None else ctx.pre[node.timeframe].trend(j)
        return None if state == "UNKNOWN" else state == node.state
    left, right = _fast_value(node.left, ctx), _fast_value(node.right, ctx)
    if left is None or right is None:
        return None
    if node.op in (">", ">=", "<", "<="):
        return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[node.op]
    pl, pr = _fast_value(node.left, ctx, previous=True), _fast_value(node.right, ctx, previous=True)
    if pl is None or pr is None:
        return None
    if node.op == "crosses_above":
        return pl <= pr and left > right
    return pl >= pr and left < right


def _rule_signals(rule: StrategyRule, by_tf: Mapping[str, CandleSeries], series: CandleSeries) -> list[str]:
    pre = {tf: _Prefix(by_tf[tf]) for tf in rule.timeframes()}
    out = []
    for c in series.candles:
        r = _fast_eval(rule.condition, _FastCtx(pre, c.close_time_ms + 1, rule.trigger_timeframe))
        out.append(MATCH if r is True else NO_MATCH if r is False else INSUFFICIENT_DATA)
    return out


def _event_need(event_type: str) -> int:
    if event_type.startswith("SMA"):
        return int(event_type[3:].split("_")[0]) + 1  # needs the SMA on the previous bar too
    return {"FAILED_BREAKOUT": PIVOT_LOOKBACK + 2, "FAILED_BREAKDOWN": PIVOT_LOOKBACK + 2,
            "VOLATILITY_CONTRACTION": CONTRACTION_RECENT + CONTRACTION_BASE + 1}.get(event_type, 0)


def _event_signals(series: CandleSeries, event_type: str) -> list[str]:
    """Per bar: would ``engine.analyze(series.up_to(close + 1))`` report status OK with at least the bars this
    event needs, and list ``event_type``? Mirrors ``engine._events`` on per-bar values computed once."""
    p = _Prefix(series)
    candles, need = p.candles, max(_event_need(event_type), MIN_BARS)
    vr = p.array("volume_ratio", PIVOT_LOOKBACK)
    ph, pl = p.array("prior_high", PIVOT_LOOKBACK), p.array("prior_low", PIVOT_LOOKBACK)
    sma_n = int(event_type[3:].split("_")[0]) if event_type.startswith("SMA") else None
    seg_sma: dict[int, list[float | None]] = {}
    seg_tr: dict[int, list[float]] = {}
    out = []
    for i, c in enumerate(candles):
        s = p.seg[i]
        if p.view(c.close_time_ms + 1) is None or i - s + 1 < need:
            out.append(INSUFFICIENT_DATA)  # stale, too few bars for status OK, or for this event
            continue
        prev, k = candles[i - 1], i - s
        hit = False
        if event_type == "BREAKOUT":
            hit = ph[i] is not None and vr[i] is not None and c.close > ph[i] and vr[i] >= BREAKOUT_VOLUME_RATIO
        elif event_type == "BREAKDOWN":
            hit = pl[i] is not None and vr[i] is not None and c.close < pl[i] and vr[i] >= BREAKOUT_VOLUME_RATIO
        elif event_type == "FAILED_BREAKOUT":  # need >= PIVOT_LOOKBACK + 2, so bar i - 1 is in this segment
            hit = ph[i - 1] is not None and prev.close > ph[i - 1] and c.close < ph[i - 1]
        elif event_type == "FAILED_BREAKDOWN":
            hit = pl[i - 1] is not None and prev.close < pl[i - 1] and c.close > pl[i - 1]
        elif event_type == "UNUSUAL_VOLUME":
            hit = vr[i] is not None and vr[i] >= UNUSUAL_VOLUME_RATIO
        elif event_type == "VOLATILITY_CONTRACTION":
            if s not in seg_tr:
                seg_tr[s] = true_ranges(candles[s:p.seg_end[s]])
            tr = seg_tr[s]  # true ranges of the segment; the tail ending at i has k of them
            recent = sum(tr[k - CONTRACTION_RECENT:k]) / CONTRACTION_RECENT
            base = sum(tr[k - CONTRACTION_RECENT - CONTRACTION_BASE:k - CONTRACTION_RECENT]) / CONTRACTION_BASE
            hit = base > 0 and recent / base < CONTRACTION_RATIO
        else:  # SMA{n}_RECLAIM / SMA{n}_LOSS: rolling SMA over the segment, as engine.sma_series does
            assert sma_n is not None
            if s not in seg_sma:
                seg_sma[s] = sma_series(p.closes[s:p.seg_end[s]], sma_n)
            sm = seg_sma[s]
            if sm[k] is not None and sm[k - 1] is not None:
                if prev.close < sm[k - 1] and c.close > sm[k]:
                    hit = event_type.endswith("_RECLAIM")
                elif prev.close > sm[k - 1] and c.close < sm[k]:
                    hit = event_type.endswith("_LOSS")
        out.append(MATCH if hit else NO_MATCH)
    return out


def compute_signals(source: Source, series: CandleSeries, *,
                    other_series: Mapping[str, CandleSeries] | None = None) -> list[str]:
    """One state (MATCH / NO_MATCH / INSUFFICIENT_DATA) per bar of ``series``, each computed from
    bars closed at that bar's close only. ``other_series`` supplies extra rule timeframes.
    Identical to ``rules.evaluate(...)["result"]`` / ``engine.analyze`` per bar, in linear time."""
    if isinstance(source, StrategyRule):
        if source.trigger_timeframe != series.timeframe.code:
            raise ValueError("series timeframe must equal the rule's trigger_timeframe")
        by_tf = {**(other_series or {}), series.timeframe.code: series}
        missing = source.timeframes() - set(by_tf)
        if missing:
            raise ValueError(f"missing series for timeframe(s): {sorted(missing)}")
        return _rule_signals(source, by_tf, series)
    if isinstance(source, str):
        if source not in ENGINE_EVENT_TYPES:
            raise ValueError(f"unknown engine event type {source!r}")
        return _event_signals(series, source)
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

    atr_at: list[float | None] | None = None  # Wilder ATR of bars closed at each bar, built on first use

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
                if atr_at is None:
                    atr_at = _Prefix(series).array("atr_wilder", ATR_PERIOD)
                atr = atr_at[j]
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
    """Backtest a rule or engine event over ``series``. Precomputed ``signals`` may be passed.
    Raises ``BacktestBusyError`` when ``MAX_CONCURRENT_BACKTESTS`` are already running."""
    with backtest_slot():
        sig = list(signals) if signals is not None else compute_signals(source, series, other_series=other_series)
        return {**_header(source, series, config), **simulate(series, sig, config)}


def walk_forward(source: Source, series: CandleSeries, config: BacktestConfig, *, n_folds: int = 4,
                 embargo_bars: int | None = None, other_series: Mapping[str, CandleSeries] | None = None,
                 signals: Sequence[str] | None = None) -> dict[str, Any]:
    """Expanding-window walk-forward. Bars split into ``n_folds + 1`` chronological segments; fold i
    tests segment i after skipping an embargo (>= hold_bars) right after its in-sample end. Rules are
    not fitted here, so in-sample data is unused and folds report per-period metrics and stability.
    Trades must lie entirely inside their fold's test window. Raises ``BacktestBusyError`` like run_backtest."""
    with backtest_slot():
        return _walk_forward(source, series, config, n_folds=n_folds, embargo_bars=embargo_bars,
                             other_series=other_series, signals=signals)


def _walk_forward(source: Source, series: CandleSeries, config: BacktestConfig, *, n_folds: int,
                  embargo_bars: int | None, other_series: Mapping[str, CandleSeries] | None,
                  signals: Sequence[str] | None) -> dict[str, Any]:
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
