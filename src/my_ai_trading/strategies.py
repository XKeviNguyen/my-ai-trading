"""Deterministic evaluators for the experimental methodology profiles.

Outputs are descriptive states with the checks that produced them, never BUY/SELL. Criteria that
cannot be evaluated are listed, and a partial evaluation never reports a full pass (audit H5).
"""
from __future__ import annotations

from typing import Any, Callable

from .domain import CandleSeries
from .engine import utc_iso
from .indicators import prior_high, prior_low, sma, true_ranges

# Calendar model: crypto trades 24x7, so 52 weeks = 364 daily bars and one month ≈ 30 daily bars.
# Equity sources use ~252 sessions/year; that calendar is not implemented yet.
CRYPTO_52W_DAILY_BARS = 364
CRYPTO_MONTH_DAILY_BARS = 30


def _base(series: CandleSeries, method: str, version: str) -> dict[str, Any]:
    tail = series.contiguous_tail()
    last = tail[-1] if tail else None
    return {"method": method, "method_version": version, "timeframe": series.timeframe.code,
            "bar_close_time_utc": utc_iso(last.close_time_ms + 1) if last else None,
            "bars_used": len(tail)}


def minervini_trend_template(series: CandleSeries) -> dict[str, Any]:
    out = _base(series, "minervini_trend_template_price_criteria", "1")
    if series.timeframe.code != "1d":
        raise ValueError("Trend template is evaluated on daily bars only")
    tail = series.contiguous_tail()
    closes = [c.close for c in tail]
    m50, m150, m200 = (sma(closes, n) for n in (50, 150, 200))
    m200_past = sma(closes[:-CRYPTO_MONTH_DAILY_BARS], 200) if len(closes) > CRYPTO_MONTH_DAILY_BARS else None
    year = tail[-CRYPTO_52W_DAILY_BARS:] if len(tail) >= CRYPTO_52W_DAILY_BARS else None
    c = closes[-1] if closes else None

    def chk(cond: Callable[[], bool], *needs: Any) -> bool | None:
        return None if c is None or any(v is None for v in needs) else cond()

    checks = {
        "price_above_sma150_and_sma200": chk(lambda: c > m150 and c > m200, m150, m200),
        "sma150_above_sma200": chk(lambda: m150 > m200, m150, m200),
        "sma200_rising_1_month": chk(lambda: m200 > m200_past, m200, m200_past),
        "sma50_above_sma150_and_sma200": chk(lambda: m50 > m150 and m50 > m200, m50, m150, m200),
        "price_above_sma50": chk(lambda: c > m50, m50),
        "price_30pct_above_52w_low": chk(lambda: c >= 1.30 * min(x.low for x in year), year),
        "price_within_25pct_of_52w_high": chk(lambda: c >= 0.75 * max(x.high for x in year), year),
    }
    unevaluated = ["relative_strength_rating_>=70 (needs a ranked universe)",
                   "SEPA fundamentals (earnings, sales, margins)", "VCP base structure"]
    if False in checks.values():
        state = "TEMPLATE_FAILED"
    elif None in checks.values():
        state = "INSUFFICIENT_DATA"
    else:
        state = "PRICE_CRITERIA_MET_PARTIAL"  # never a full pass: RS/fundamentals unevaluated
    out.update(state=state, checks=checks, unevaluated_criteria=unevaluated,
               values={"close": c, "sma50": m50, "sma150": m150, "sma200": m200},
               calendar_note="52 weeks = 364 daily bars because crypto trades 24x7 (explicit calendar model).",
               invalidation="close back below SMA50 / SMA150 or SMA200 turning down breaks the template")
    return out


WEINSTEIN_MA = 30
WEINSTEIN_SLOPE_BARS = 4
WEINSTEIN_FLAT = 0.01      # |slope over 4 weeks| below 1% treated as flat (interpretation, not published)
WEINSTEIN_PRIOR_BARS = 20


def weinstein_stage(series: CandleSeries) -> dict[str, Any]:
    out = _base(series, "weinstein_stage_analysis", "1")
    if series.timeframe.code != "1w":
        raise ValueError("Stage analysis is evaluated on weekly bars only (30-week MA, not 30 of any bar)")
    tail = series.contiguous_tail()
    closes = [c.close for c in tail]
    ma = sma(closes, WEINSTEIN_MA)
    ma_past = sma(closes[:-WEINSTEIN_SLOPE_BARS], WEINSTEIN_MA) if len(closes) > WEINSTEIN_SLOPE_BARS else None
    ma_prior = sma(closes[:-WEINSTEIN_PRIOR_BARS], WEINSTEIN_MA) if len(closes) > WEINSTEIN_PRIOR_BARS else None
    if None in (ma, ma_past, ma_prior):
        out.update(state="INSUFFICIENT_DATA",
                   reason=f"needs {WEINSTEIN_MA + WEINSTEIN_PRIOR_BARS}+ contiguous weekly bars")
        return out
    c = closes[-1]
    slope = (ma - ma_past) / ma_past
    prior_trend = (ma - ma_prior) / ma_prior
    flat = abs(slope) < WEINSTEIN_FLAT
    if not flat and slope > 0 and c > ma:
        stage = "STAGE_2_ADVANCING"
    elif not flat and slope < 0 and c < ma:
        stage = "STAGE_4_DECLINING"
    elif prior_trend < 0:
        stage = "STAGE_1_BASING"
    elif prior_trend > 0:
        stage = "STAGE_3_TOPPING"
    else:
        stage = "UNCLEAR"
    vol = [x.volume for x in tail]
    vol_ratio = vol[-1] / (sum(vol[-11:-1]) / 10) if len(vol) >= 11 and sum(vol[-11:-1]) > 0 else None
    out.update(state=stage, checks={"close_above_30w_ma": c > ma, "ma_slope_4w": round(slope, 6),
                                    "ma_flat": flat, "ma_change_20w": round(prior_trend, 6),
                                    "weekly_volume_vs_prior10": None if vol_ratio is None else round(vol_ratio, 4)},
               values={"close": c, "sma30w": ma},
               parameters={"flat_threshold": WEINSTEIN_FLAT, "slope_bars": WEINSTEIN_SLOPE_BARS,
                           "parameter_provenance": "interpretation; the book describes stages visually"},
               invalidation=("weekly close below a rising 30-week MA weakens Stage 2" if stage.startswith("STAGE_2")
                             else "weekly close above a declining 30-week MA weakens Stage 4"
                             if stage.startswith("STAGE_4") else "a decisive weekly move away from the MA"))
    return out


def turtle_breakout(series: CandleSeries) -> dict[str, Any]:
    out = _base(series, "turtle_donchian_breakouts", "1")
    if series.timeframe.code != "1d":
        raise ValueError("Turtle rules are evaluated on daily bars")
    tail = series.contiguous_tail()
    if len(tail) < 56:
        out.update(state="INSUFFICIENT_DATA", reason="needs 56+ contiguous daily bars")
        return out
    last = tail[-1]
    h20, l20, h55, l55 = prior_high(tail, 20), prior_low(tail, 20), prior_high(tail, 55), prior_low(tail, 55)
    l10, h10 = prior_low(tail, 10), prior_high(tail, 10)
    tr = true_ranges(tail)
    n = sum(tr[:20]) / 20  # N: 20-day exponential-style average of TR per the published rules
    for x in tr[20:]:
        n = (19 * n + x) / 20
    signals = []
    if last.close > h20:
        signals.append("SYSTEM1_20D_HIGH_BREAKOUT")
    if last.close < l20:
        signals.append("SYSTEM1_20D_LOW_BREAKOUT")
    if last.close > h55:
        signals.append("SYSTEM2_55D_HIGH_BREAKOUT")
    if last.close < l55:
        signals.append("SYSTEM2_55D_LOW_BREAKOUT")
    out.update(state="BREAKOUT_CONDITIONS" if signals else "NO_BREAKOUT", signals=signals,
               values={"close": last.close, "high20": h20, "low20": l20, "high55": h55, "low55": l55,
                       "exit_low10": l10, "exit_high10": h10, "N": n},
               volatility_reference={"two_N": 2 * n,
                                     "note": "2N is the volatility unit the published rules used for stops; shown "
                                             "for study only, not a stop or position instruction"},
               note="Closed-bar evaluation; the original rules entered intraday when price crossed the level.")
    return out


DARVAS_CONFIRM_BARS = 3


def darvas_box(series: CandleSeries) -> dict[str, Any]:
    """Three-day box rule: a high not exceeded for 3 bars sets the top; a low held 3 bars sets the bottom."""
    out = _base(series, "darvas_box", "1")
    if series.timeframe.code != "1d":
        raise ValueError("Darvas boxes are evaluated on daily bars")
    tail = series.contiguous_tail()
    if len(tail) < 20:
        out.update(state="INSUFFICIENT_DATA", reason="needs 20+ contiguous daily bars")
        return out
    top = bottom = None
    top_i = None
    state = "SEEKING_TOP"
    box_history: list[dict[str, Any]] = []
    i = 0
    while i < len(tail):
        c = tail[i]
        if state == "SEEKING_TOP":
            nxt = tail[i + 1:i + 1 + DARVAS_CONFIRM_BARS]
            if len(nxt) == DARVAS_CONFIRM_BARS and all(x.high <= c.high for x in nxt) and \
                    c.high >= max(x.high for x in tail[max(0, i - 10):i + 1]):
                top, top_i, state = c.high, i, "SEEKING_BOTTOM"
                i += DARVAS_CONFIRM_BARS
        elif state == "SEEKING_BOTTOM":
            if c.high > top:
                state, top, top_i = "SEEKING_TOP", None, None
                continue
            low_i = min(range(top_i + 1, i + 1), key=lambda k: tail[k].low)
            held = tail[low_i + 1:low_i + 1 + DARVAS_CONFIRM_BARS]
            if len(held) == DARVAS_CONFIRM_BARS and all(x.low >= tail[low_i].low for x in held) \
                    and all(x.high <= top for x in held):
                bottom, state = tail[low_i].low, "BOX_ACTIVE"
                i = low_i + DARVAS_CONFIRM_BARS
        elif state == "BOX_ACTIVE":
            if c.close > top:
                box_history.append({"top": top, "bottom": bottom, "exit": "BREAKOUT_ABOVE", "bar": i})
                state, top, bottom = "SEEKING_TOP", None, None
                if i == len(tail) - 1:
                    out.update(state="BREAKOUT_ABOVE_BOX", box=box_history[-1])
                    return _finish_darvas(out, tail)
            elif c.close < bottom:
                box_history.append({"top": top, "bottom": bottom, "exit": "BREAK_BELOW", "bar": i})
                state, top, bottom = "SEEKING_TOP", None, None
                if i == len(tail) - 1:
                    out.update(state="BREAK_BELOW_BOX", box=box_history[-1])
                    return _finish_darvas(out, tail)
        i += 1
    out.update(state={"SEEKING_TOP": "NO_ACTIVE_BOX", "SEEKING_BOTTOM": "BOX_TOP_ONLY",
                      "BOX_ACTIVE": "INSIDE_BOX"}[state], box={"top": top, "bottom": bottom})
    return _finish_darvas(out, tail)


def _finish_darvas(out: dict[str, Any], tail) -> dict[str, Any]:
    out["values"] = {"close": tail[-1].close}
    out["parameters"] = {"confirm_bars": DARVAS_CONFIRM_BARS,
                         "parameter_provenance": "commonly cited three-day rule; Darvas's own book is narrative"}
    out["unevaluated_criteria"] = ["new all-time/52-week high requirement", "volume confirmation"]
    return out


EVALUATORS: dict[str, Callable[[CandleSeries], dict[str, Any]]] = {
    "minervini_trend_template": minervini_trend_template,
    "weinstein_stage": weinstein_stage,
    "turtle_breakout": turtle_breakout,
    "darvas_box": darvas_box,
}
