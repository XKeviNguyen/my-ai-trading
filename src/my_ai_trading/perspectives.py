"""Evaluate user-selected methodology perspectives: applicability gate first, then native evaluation.

Each perspective is reported independently. There is no vote or consensus across methodologies
(PRD F07, Scenario K): frameworks differ in horizon, inputs and applicability.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .domain import AnalysisMode, Applicability, CandleSeries, InstrumentIdentity
from .engine import analyze
from .methodology import BY_ID, assess
from .strategies import EVALUATORS

SeriesGetter = Callable[[str], CandleSeries]


def evaluate_perspectives(instrument: InstrumentIdentity, analysis_timeframe: str,
                          selections: Iterable[dict[str, str]], get_series: SeriesGetter, *,
                          as_of_ms: int, approved_adaptations: frozenset[str] = frozenset()) -> dict[str, Any]:
    results = []
    for sel in selections:
        mid = sel.get("methodology_id", "")
        profile = BY_ID.get(mid)
        if profile is None:
            raise ValueError(f"Unknown methodology_id {mid!r}")
        mode = AnalysisMode(sel.get("mode", "NATIVE"))
        a = assess(profile, instrument, analysis_timeframe, mode,
                   approved_adaptation=mid in approved_adaptations)
        item: dict[str, Any] = {"assessment": a.as_dict(), "provenance": profile.provenance,
                                "evidence_quality": profile.evidence_quality,
                                "implementation_maturity": profile.implementation_maturity,
                                "limitations": list(profile.limitations)}
        if a.evaluate and a.evaluation_timeframe:
            series = get_series(a.evaluation_timeframe)
            if series.is_stale(as_of_ms):
                item["assessment"]["status"] = str(Applicability.STALE_DATA)
                item["evaluation"] = {"state": "SKIPPED", "reason": "native-timeframe data is stale"}
            else:
                ev = EVALUATORS[profile.evaluator](series)
                if ev.get("state") == "INSUFFICIENT_DATA":
                    item["assessment"]["status"] = str(Applicability.INSUFFICIENT_DATA)
                item["evaluation"] = ev
        else:
            item["evaluation"] = {"state": "NOT_EVALUATED",
                                  "reason": a.reasons[-1] if a.reasons else str(a.status)}
        if mode is AnalysisMode.HYBRID and a.status is Applicability.SUPPORTED_HYBRID:
            tactical = analyze(get_series(analysis_timeframe), as_of_ms=as_of_ms)
            item["tactical_layer"] = {
                "label": "Custom tactical timing — not part of the methodology",
                "timeframe": analysis_timeframe, "status": tactical["status"],
                "events": tactical.get("events", []), "trend": tactical.get("trend"),
            }
        results.append(item)
    return {"instrument": instrument.as_dict(), "analysis_timeframe": analysis_timeframe,
            "perspectives": results,
            "aggregation": "none — each framework is reported on its own applicability and evidence; "
                           "no majority vote across methodologies"}
