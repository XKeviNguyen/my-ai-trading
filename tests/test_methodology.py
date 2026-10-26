"""PRD F04/F13 and acceptance Scenarios G, H, I, K (deterministic parts)."""
import pytest

from my_ai_trading.domain import AnalysisMode, Applicability, InstrumentIdentity, MarketType
from my_ai_trading.fixtures import series_from_closes, uptrend_closes
from my_ai_trading.methodology import BY_ID, PROFILES, assess
from my_ai_trading.perspectives import evaluate_perspectives

SAND = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")
F13_FIELDS = {"methodology_id", "strategy_version", "published_assets", "documented_timeframes",
              "context_timeframes", "execution_timeframes", "market_calendar", "required_data",
              "provenance", "evidence_quality", "implementation_maturity", "limitations"}


def series_for(tf_bars):
    cache = {tf: series_from_closes(uptrend_closes(n), tf) for tf, n in tf_bars.items()}

    def get(tf):
        return cache[tf]
    return get, cache


def test_catalog_has_20_profiles_with_f13_metadata_and_no_invented_timeframes():
    assert len(PROFILES) == 20
    for p in PROFILES:
        d = p.as_dict()
        assert F13_FIELDS <= set(d)
        assert p.implementation_maturity in ("catalog_only", "experimental", "validated")
        if p.evidence_quality == "unverified":
            assert d["documented_timeframes"] == "UNVERIFIED"
        if p.implementation_maturity == "catalog_only":
            assert p.evaluator is None
    assert not any(p.implementation_maturity == "validated" for p in PROFILES)


def test_scenario_g_weinstein_on_1h_evaluates_weekly_not_hourly_sma30():
    a = assess(BY_ID["weinstein"], SAND, "1h")
    assert a.status is Applicability.SUPPORTED_NATIVE
    assert a.evaluation_timeframe == "1w"
    assert {r["mode"] for r in a.offered_routes} == {"NATIVE", "HYBRID", "ADAPTED"}
    get, cache = series_for({"1w": 120, "1h": 300})
    out = evaluate_perspectives(SAND, "1h", [{"methodology_id": "weinstein"}], get,
                                as_of_ms=cache["1w"].candles[-1].close_time_ms + 1)
    ev = out["perspectives"][0]["evaluation"]
    assert ev["timeframe"] == "1w" and ev["state"].startswith("STAGE_")


def test_scenario_g_hybrid_keeps_native_context_and_separate_tactical_layer():
    get, cache = series_for({"1w": 120, "1h": 300})
    as_of = min(cache["1w"].candles[-1].close_time_ms, cache["1h"].candles[-1].close_time_ms) + 1
    out = evaluate_perspectives(SAND, "1h", [{"methodology_id": "weinstein", "mode": "HYBRID"}], get,
                                as_of_ms=as_of)
    p = out["perspectives"][0]
    assert p["assessment"]["label"] == "Hybrid: native context + user timing"
    assert p["evaluation"]["timeframe"] == "1w"
    assert p["tactical_layer"]["timeframe"] == "1h"
    assert "not part of the methodology" in p["tactical_layer"]["label"]


def test_scenario_h_lynch_and_oneil_on_crypto_are_unsupported_asset():
    for mid in ("lynch", "oneil"):
        a = assess(BY_ID[mid], SAND, "1h")
        assert a.status is Applicability.UNSUPPORTED_ASSET
        assert not a.evaluate
        assert any(r["mode"] == "CUSTOM" for r in a.offered_routes)


def test_scenario_i_adapted_requires_approval_and_labels_unvalidated():
    a = assess(BY_ID["weinstein"], SAND, "1h", AnalysisMode.ADAPTED)
    assert a.status is Applicability.ADAPTED_UNVALIDATED
    assert a.requires_user_approval and not a.evaluate
    assert a.label == "User-adapted — unvalidated; not the trader's original system"
    assert BY_ID["weinstein"].documented_timeframes == ("1w",)  # original unchanged


def test_unverified_methods_and_missing_data():
    assert assess(BY_ID["seykota"], SAND, "1d").status is Applicability.UNVERIFIED_METHOD
    assert assess(BY_ID["ptj"], SAND, "1d").status is Applicability.UNVERIFIED_METHOD
    assert assess(BY_ID["kullamagi"], SAND, "1d").status is Applicability.INSUFFICIENT_DATA  # catalysts
    a = assess(BY_ID["brandt"], SAND, "1d")
    assert a.status is Applicability.SUPPORTED_NATIVE and not a.evaluate  # catalog only
    with pytest.raises(ValueError):
        assess(BY_ID["brandt"], SAND, "1d", AnalysisMode.CUSTOM)


def test_minervini_on_crypto_is_partial_never_full_pass():
    a = assess(BY_ID["minervini"], SAND, "1d")
    assert a.status is Applicability.SUPPORTED_NATIVE and a.evaluate
    assert any("relative_strength_universe" in r for r in a.reasons)


def test_scenario_k_three_profiles_reported_independently_without_vote():
    get, cache = series_for({"1d": 400, "1w": 120})
    as_of = min(s.candles[-1].close_time_ms for s in cache.values()) + 1
    out = evaluate_perspectives(SAND, "1d", [{"methodology_id": m} for m in ("minervini", "weinstein", "lynch")],
                                get, as_of_ms=as_of)
    statuses = [p["assessment"]["status"] for p in out["perspectives"]]
    assert statuses[2] == "UNSUPPORTED_ASSET"
    assert "no majority vote" in out["aggregation"]
    assert not any(k in out for k in ("consensus", "votes", "overall_signal"))
    text = str(out)
    assert "BUY" not in text and "SELL" not in text


def test_stale_native_data_is_reported_not_evaluated():
    get, cache = series_for({"1w": 120})
    out = evaluate_perspectives(SAND, "1w", [{"methodology_id": "weinstein"}], get,
                                as_of_ms=cache["1w"].candles[-1].close_time_ms + 30 * 86_400_000)
    p = out["perspectives"][0]
    assert p["assessment"]["status"] == "STALE_DATA" and p["evaluation"]["state"] == "SKIPPED"
