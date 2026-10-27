"""Deterministic Fast Engine: facts and rule-based events, never recommendations.

Every event carries the conditions that produced it, the bar it refers to, and an invalidation
condition. Events are computed only from the contiguous run of closed bars ending at the latest
bar, and are suppressed when data is stale. No methodology preset is applied here; presets are
evaluated separately and only when selected (audit H4).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .domain import CandleSeries
from .indicators import (adx, atr_sma_tr, atr_wilder, bollinger, ema, macd, prior_high, prior_low, rsi_wilder,
                         sma, sma_series, true_ranges, volume_ratio)

ENGINE_VERSION = "fast-engine/0.2.0"
SMA_PERIODS = (7, 25, 50, 100, 150, 200)
MIN_BARS = 21  # breakout/volume rules need 20 prior bars + the tested bar
PIVOT_LOOKBACK = 20
BREAKOUT_VOLUME_RATIO = 1.5
UNUSUAL_VOLUME_RATIO = 2.0
RECLAIM_PERIODS = (25, 50, 200)
CONTRACTION_RECENT, CONTRACTION_BASE, CONTRACTION_RATIO = 10, 50, 0.6
TREND_SLOPE_BARS = 10


def utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _r(v: float | None, digits: int = 10) -> float | None:
    return None if v is None else round(v, digits)


def analyze(series: CandleSeries, *, as_of_ms: int) -> dict[str, Any]:
    tail = series.contiguous_tail()
    stale = series.is_stale(as_of_ms)
    missing: list[str] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "instrument": series.instrument.as_dict(),
        "timeframe": series.timeframe.code,
        "as_of_utc": utc_iso(as_of_ms),
        "data_quality": {
            "closed_bars": len(series.candles), "contiguous_bars": len(tail),
            "gaps": len(series.gaps), "missing_bars": series.missing_bars(), "stale": stale,
        },
        "read_only": True,
    }
    if not tail:
        report.update(status="INSUFFICIENT_DATA", missing_inputs=["no closed candles"],
                      indicator_values={}, events=[], trend={"state": "UNKNOWN"})
        return report

    last = tail[-1]
    closes = [c.close for c in tail]
    sma_now = {n: sma(closes, n) for n in SMA_PERIODS}
    vr = volume_ratio(tail, PIVOT_LOOKBACK)
    ph, pl = prior_high(tail, PIVOT_LOOKBACK), prior_low(tail, PIVOT_LOOKBACK)
    m, bb, dx = macd(closes), bollinger(closes), adx(tail)
    indicators = {
        **{f"sma{n}": _r(v) for n, v in sma_now.items()},
        "ema21": _r(ema(closes, 21)),
        "atr14_wilder": _r(atr_wilder(tail, 14)),
        "atr14_sma_tr": _r(atr_sma_tr(tail, 14)),
        "rsi14_wilder": _r(rsi_wilder(closes, 14), 4),
        "volume_ratio_20": _r(vr, 6),
        "prior_high_20": _r(ph),
        "prior_low_20": _r(pl),
        "macd_12_26": _r(m and m["macd"]), "macd_signal_9": _r(m and m["signal"]),
        "macd_histogram": _r(m and m["histogram"]),
        "bollinger_20_2_upper": _r(bb and bb["upper"]), "bollinger_20_2_lower": _r(bb and bb["lower"]),
        "bollinger_20_2_percent_b": _r(bb and bb["percent_b"], 4),
        "bollinger_20_2_bandwidth": _r(bb and bb["bandwidth"], 6),
        "adx14": _r(dx and dx["adx"], 4), "plus_di14": _r(dx and dx["plus_di"], 4),
        "minus_di14": _r(dx and dx["minus_di"], 4),
    }
    for name, v in indicators.items():
        if v is None:
            missing.append(f"{name}: needs more contiguous closed bars (have {len(tail)})")
    if series.gaps:
        missing.append(f"{series.missing_bars()} missing bar(s) in history; indicators use the "
                       f"{len(tail)} contiguous bars after the last gap")

    trend = trend_state(closes, sma_now)
    events = [] if stale or len(tail) < MIN_BARS else _events(tail, closes, vr, ph, pl)
    if stale:
        missing.append("STALE_DATA: newest closed bar is older than expected; events suppressed")

    status = "STALE_DATA" if stale else "OK" if len(tail) >= MIN_BARS else "INSUFFICIENT_DATA"
    report.update(
        status=status,
        bar_open_time_ms=last.open_time_ms,
        bar_close_time_utc=utc_iso(last.close_time_ms + 1),
        last_close=last.close,
        indicator_values=indicators,
        trend=trend,
        levels={"resistance_prior_high_20": _r(ph), "support_prior_low_20": _r(pl)},
        events=events,
        missing_inputs=missing,
        disclaimer="Research observations from closed bars only; not a trade instruction or a validated edge.",
    )
    return report


def trend_state(closes: list[float], sma_now: dict[int, float | None]) -> dict[str, Any]:
    s50, s200 = sma_now[50], sma_now[200]
    s50_past = sma(closes[:-TREND_SLOPE_BARS], 50) if len(closes) > TREND_SLOPE_BARS else None
    if None in (s50, s200, s50_past):
        return {"state": "UNKNOWN", "reason": "needs 200+ contiguous bars for SMA200 and SMA50 slope"}
    c = closes[-1]
    cond = {"close_above_sma50": c > s50, "sma50_above_sma200": s50 > s200,
            f"sma50_rising_{TREND_SLOPE_BARS}_bars": s50 > s50_past}
    if all(cond.values()):
        state = "UP"
    elif c < s50 and s50 < s200 and s50 < s50_past:
        state = "DOWN"
    else:
        state = "MIXED"
    return {"state": state, "conditions": cond, "definition": "close vs SMA50, SMA50 vs SMA200, SMA50 slope"}


def _events(tail, closes, vr, ph, pl) -> list[dict[str, Any]]:
    last, prev = tail[-1], tail[-2]
    bar = {"bar_open_time_ms": last.open_time_ms, "bar_close_time_utc": utc_iso(last.close_time_ms + 1)}
    out: list[dict[str, Any]] = []

    def add(kind: str, conditions: dict[str, Any], invalidation: str) -> None:
        conditions = {k: _r(v) if isinstance(v, float) else v for k, v in conditions.items()}
        out.append({"type": kind, **bar, "conditions_met": conditions, "invalidation": invalidation,
                    "rule_version": f"{kind.lower()}/1"})

    if ph is not None and vr is not None and last.close > ph and vr >= BREAKOUT_VOLUME_RATIO:
        add("BREAKOUT", {"close": last.close, "prior_high_20": ph, "volume_ratio_20": round(vr, 4),
                         "min_volume_ratio": BREAKOUT_VOLUME_RATIO},
            f"a later closed bar back below {ph:g} (the broken prior-20-bar high)")
    if pl is not None and vr is not None and last.close < pl and vr >= BREAKOUT_VOLUME_RATIO:
        add("BREAKDOWN", {"close": last.close, "prior_low_20": pl, "volume_ratio_20": round(vr, 4),
                          "min_volume_ratio": BREAKOUT_VOLUME_RATIO},
            f"a later closed bar back above {pl:g} (the broken prior-20-bar low)")

    if len(tail) >= PIVOT_LOOKBACK + 2:
        prev_ph, prev_pl = prior_high(tail[:-1], PIVOT_LOOKBACK), prior_low(tail[:-1], PIVOT_LOOKBACK)
        if prev_ph is not None and prev.close > prev_ph and last.close < prev_ph:
            add("FAILED_BREAKOUT", {"previous_close": prev.close, "pivot": prev_ph, "close": last.close},
                f"a closed bar back above {prev_ph:g}")
        if prev_pl is not None and prev.close < prev_pl and last.close > prev_pl:
            add("FAILED_BREAKDOWN", {"previous_close": prev.close, "pivot": prev_pl, "close": last.close},
                f"a closed bar back below {prev_pl:g}")

    for n in RECLAIM_PERIODS:
        s = sma_series(closes, n)
        if len(s) >= 2 and s[-1] is not None and s[-2] is not None:
            if prev.close < s[-2] and last.close > s[-1]:
                add(f"SMA{n}_RECLAIM", {"previous_close": prev.close, f"previous_sma{n}": s[-2],
                                        "close": last.close, f"sma{n}": s[-1]},
                    f"a closed bar back below SMA{n}")
            elif prev.close > s[-2] and last.close < s[-1]:
                add(f"SMA{n}_LOSS", {"previous_close": prev.close, f"previous_sma{n}": s[-2],
                                     "close": last.close, f"sma{n}": s[-1]},
                    f"a closed bar back above SMA{n}")

    if vr is not None and vr >= UNUSUAL_VOLUME_RATIO:
        add("UNUSUAL_VOLUME", {"volume_ratio_20": round(vr, 4), "threshold": UNUSUAL_VOLUME_RATIO},
            "informational; no price invalidation")

    tr = true_ranges(tail)
    if len(tr) >= CONTRACTION_RECENT + CONTRACTION_BASE:
        recent = sum(tr[-CONTRACTION_RECENT:]) / CONTRACTION_RECENT
        base = sum(tr[-CONTRACTION_RECENT - CONTRACTION_BASE:-CONTRACTION_RECENT]) / CONTRACTION_BASE
        if base > 0 and recent / base < CONTRACTION_RATIO:
            add("VOLATILITY_CONTRACTION",
                {"mean_tr_recent_10": recent, "mean_tr_prior_50": base, "ratio": round(recent / base, 4),
                 "threshold": CONTRACTION_RATIO},
                "range expansion (ratio back above threshold); contraction alone is not a VCP")
    return out
