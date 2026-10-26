"""Challenge My Thesis (PRD F12, Scenario J) — deterministic evidence audit, zero tokens.

Separates observations, supporting and contradicting evidence, assumptions, unknowns and the
observations that would falsify the thesis. It never assigns probabilities and never accepts the
user's premise by default; an LLM may elaborate later but cannot change these facts.
"""
from __future__ import annotations

import re
from typing import Any

from .domain import Timeframe

BULL_EVENTS = {"BREAKOUT", "FAILED_BREAKDOWN", "SMA25_RECLAIM", "SMA50_RECLAIM", "SMA200_RECLAIM"}
BEAR_EVENTS = {"BREAKDOWN", "FAILED_BREAKOUT", "SMA25_LOSS", "SMA50_LOSS", "SMA200_LOSS"}
NOT_AVAILABLE = ["funding rate and open interest", "order book / liquidity depth", "news and catalysts",
                 "on-chain or fundamental data"]


def detect_direction(text: str) -> str | None:
    t = text.lower()
    bull = len(re.findall(r"\b(bull\w*|long|breakout|uptrend|go(?:es|ing)? up|rally|pump|moon|buy)\b", t))
    bear = len(re.findall(r"\b(bear\w*|short|breakdown|downtrend|go(?:es|ing)? down|dump|crash|sell)\b", t))
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return None


def challenge(reports: dict[str, dict[str, Any]], thesis: str, *, direction: str | None = None,
              focus_timeframe: str | None = None) -> dict[str, Any]:
    direction = direction or detect_direction(thesis)
    out: dict[str, Any] = {"thesis": thesis[:500], "direction": direction, "focus_timeframe": focus_timeframe,
                           "observations": [], "supporting_evidence": [], "contradicting_evidence": [],
                           "assumptions": [], "unknowns": [], "falsification_conditions": [],
                           "proposed_tests": [], "method": "deterministic evidence audit v1 (zero tokens)"}
    if direction not in ("bullish", "bearish"):
        out["assessment"] = "INCONCLUSIVE"
        out["unknowns"].append("Could not tell whether the thesis is bullish or bearish; state the direction.")
        return out
    bull = direction == "bullish"
    usable = {tf: r for tf, r in reports.items() if r.get("status") in ("OK", "INSUFFICIENT_DATA")}
    for tf, r in reports.items():
        if tf not in usable:
            out["unknowns"].append(f"{tf}: {r.get('status')} — not used as evidence"
                                   + (f" ({r['error']})" if r.get("error") else ""))
    if not usable:
        out["assessment"] = "INSUFFICIENT_DATA"
        return out

    ordered = sorted(usable, key=lambda t: Timeframe(t).duration_ms)
    focus = focus_timeframe if focus_timeframe in usable else ordered[0]
    out["focus_timeframe"] = focus
    for tf in ordered:
        r = usable[tf]
        iv = r.get("indicator_values", {})
        when = r.get("bar_close_time_utc")
        tag = f"[{tf}, bar closed {when}]"
        trend = r.get("trend", {}).get("state", "UNKNOWN")
        out["observations"].append(f"{tag} close {r.get('last_close')}, trend {trend}, "
                                   f"SMA50 {iv.get('sma50')}, SMA200 {iv.get('sma200')}, "
                                   f"RSI14 {iv.get('rsi14_wilder')}, volume ratio {iv.get('volume_ratio_20')}")
        higher = Timeframe(tf).duration_ms > Timeframe(focus).duration_ms
        scope = "higher-timeframe context" if higher else "focus timeframe" if tf == focus else "lower timeframe"
        if trend in ("UP", "DOWN"):
            agrees = (trend == "UP") == bull
            (out["supporting_evidence"] if agrees else out["contradicting_evidence"]).append(
                f"{tag} trend is {trend} ({scope})")
        elif trend == "UNKNOWN":
            out["unknowns"].append(f"{tf}: trend undetermined (needs 200+ contiguous bars)")
        close, s200 = r.get("last_close"), iv.get("sma200")
        if close is not None and s200 is not None:
            above = close > s200
            (out["supporting_evidence"] if above == bull else out["contradicting_evidence"]).append(
                f"{tag} close is {'above' if above else 'below'} SMA200 {s200:g}")
        for ev in r.get("events", []):
            kind = ev["type"]
            if kind in BULL_EVENTS | BEAR_EVENTS:
                agrees = (kind in BULL_EVENTS) == bull
                (out["supporting_evidence"] if agrees else out["contradicting_evidence"]).append(
                    f"{tag} {kind} — invalidated by {ev['invalidation']}")
            if kind == "BREAKOUT" and bull:
                vr = ev["conditions_met"].get("volume_ratio_20")
                out["assumptions"].append(f"{tf} breakout volume ratio {vr} confirms participation only for this "
                                          "bar; follow-through is not yet observed.")
        rsi = iv.get("rsi14_wilder")
        if rsi is not None and ((bull and rsi >= 75) or (not bull and rsi <= 25)):
            out["contradicting_evidence"].append(f"{tag} RSI14 {rsi:g} is stretched in the thesis direction; "
                                                 "extension raises pullback risk (not a reversal signal by itself)")
        lvl = r.get("levels", {})
        support, resist = lvl.get("support_prior_low_20"), lvl.get("resistance_prior_high_20")
        if tf == focus or higher:
            if bull and support is not None:
                out["falsification_conditions"].append(f"a closed {tf} bar below {support:g} (prior 20-bar low)")
            if not bull and resist is not None:
                out["falsification_conditions"].append(f"a closed {tf} bar above {resist:g} (prior 20-bar high)")
            if iv.get("sma50") is not None:
                out["falsification_conditions"].append(
                    f"a closed {tf} bar {'below' if bull else 'above'} SMA50 {iv['sma50']:g}")
        if r.get("data_quality", {}).get("gaps"):
            out["unknowns"].append(f"{tf}: {r['data_quality']['missing_bars']} missing bars in history")

    higher_against = [e for e in out["contradicting_evidence"] if "higher-timeframe" in e]
    if higher_against:
        out["assumptions"].append(f"The thesis is countertrend to higher-timeframe context; a {focus} signal does not "
                                  "change the higher-timeframe conclusion until that timeframe's own bar closes.")
    out["assumptions"].append("Only closed bars are used; a candle still forming on your chart is not evidence yet.")
    out["unknowns"].extend(f"not available on this platform: {x}" for x in NOT_AVAILABLE)
    out["proposed_tests"] = [
        f"Watch the next closed {focus} bar relative to the falsification levels above.",
        "Backtest the exact setup with fees and slippage before trusting its history (Research Lab).",
        "Check whether higher-timeframe context changes at its own next close.",
    ]
    s, c = len(out["supporting_evidence"]), len(out["contradicting_evidence"])
    out["assessment"] = ("MIXED_EVIDENCE" if s and c else "EVIDENCE_LEANS_SUPPORTING" if s else
                         "EVIDENCE_LEANS_CONTRADICTING" if c else "INCONCLUSIVE")
    out["assessment_note"] = ("A description of the evidence listed, not a probability or a vote. Items differ in "
                              "weight; higher-timeframe context and invalidation levels matter more than counts.")
    return out
