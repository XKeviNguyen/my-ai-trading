"""R6 ML research lab: causal features, purged chronological splits, walk-forward model comparison,
leakage checks. scikit-learn and LightGBM are optional; their tests skip cleanly when not importable."""
import dataclasses
import json
import math
import random
import sys

import pytest

from my_ai_trading.domain import CandleSeries
from my_ai_trading.fixtures import series_from_closes
from my_ai_trading.ml import (LABEL_VERSION, WALK_FORWARD_DISCLAIMER, build_dataset, chronological_split,
                              features_at, leakage_check, run_baselines, walk_forward_folds,
                              walk_forward_models)

CLOSES = [100 * (1 + 0.001 * i) * (1 + 0.04 * math.sin(i / 5)) * (1 + 0.01 * ((i % 7) - 3)) for i in range(140)]
VOLS = [1000.0 * (1 + (i % 4)) for i in range(140)]


@pytest.fixture(scope="module")
def series():
    return series_from_closes(CLOSES, volumes=VOLS)


@pytest.fixture(scope="module")
def ds(series):
    return build_dataset(series, horizon_bars=3, cost_bps=7.0)


def test_rows_use_only_information_available_at_t(series, ds):
    assert ds.label_version == LABEL_VERSION and ds.rows
    for row in ds.rows[::9] + ds.rows[-2:]:
        t = row["t"]
        # Same features from a series that physically ends at bar t, and from one with a different future.
        truncated = CandleSeries(series.instrument, series.timeframe, series.candles[:t + 1])
        assert features_at(truncated, t) == row["features"]
        alt = series_from_closes(CLOSES[:t + 1] + [c * 3 for c in CLOSES[t + 1:]], volumes=VOLS)
        assert features_at(alt, t) == row["features"]


def test_label_definition_and_rows_past_end_dropped(series, ds):
    c = series.candles
    n, h = len(c), ds.horizon_bars
    assert max(r["t"] for r in ds.rows) == n - 1 - h
    assert ds.dropped["label_past_end"] == h
    for r in ds.rows:
        fwd = c[r["t"] + h].close / c[r["t"] + 1].open - 1
        assert r["forward_return"] == pytest.approx(fwd)
        assert r["label"] == int(fwd > 2 * 7.0 / 1e4)
    assert 0 < sum(ds.y) < len(ds.y)  # both classes present in this fixture


def test_label_windows_crossing_a_gap_are_dropped(series):
    candles = series.candles[:80] + series.candles[85:]
    gapped = CandleSeries(series.instrument, series.timeframe, candles)
    d = build_dataset(gapped, horizon_bars=3, cost_bps=0.0)
    assert d.dropped["label_crosses_gap"] == 3
    assert all(not (77 <= r["t"] <= 79) for r in d.rows)


def test_chronological_split_purges_label_overlap():
    train, test = chronological_split(100, 0.3, purge_bars=5)
    assert test == list(range(70, 100)) and train == list(range(65))
    assert max(train) + 5 < min(test)
    for bad in ((100, 0.0, 1), (100, 1.0, 1), (100, 0.3, -1), (10, 0.5, 6)):
        with pytest.raises(ValueError):
            chronological_split(*bad)


def test_baselines_without_sklearn(ds, monkeypatch):
    monkeypatch.setitem(sys.modules, "sklearn", None)  # makes `import sklearn...` raise ImportError
    out = run_baselines(ds, test_fraction=0.3)
    assert out["logistic_regression"] == {"status": "SKIPPED", "reason": "scikit-learn not installed"}
    split = out["split"]
    assert split["purge_rows"] >= ds.horizon_bars
    assert split["train_last_t"] + ds.horizon_bars < split["test_first_t"]
    m = out["majority_class"]
    assert m["n_test"] == split["n_test"] and 0 <= m["base_rate"] <= 1
    assert set(out["rule_close_above_sma50"]) >= {"precision", "recall", "base_rate", "n_positives"}
    assert "not a trade win probability" in out["disclaimer"]
    with pytest.raises(ValueError):
        run_baselines(ds, purge_bars=ds.horizon_bars - 1)


def test_logistic_regression_when_sklearn_installed(ds):
    pytest.importorskip("sklearn")
    out = run_baselines(ds)
    assert out["logistic_regression"]["status"] == "OK"
    assert out["logistic_regression"]["n_test"] == out["split"]["n_test"]


# ---------- calendar-aware labels ----------

def test_monthly_series_labels_use_calendar_steps():
    monthly = series_from_closes(CLOSES, "1M", volumes=VOLS)  # bar lengths vary (28-31 days)
    d = build_dataset(monthly, horizon_bars=3, cost_bps=7.0)
    assert d.dropped["label_crosses_gap"] == 0 and d.dropped["label_past_end"] == 3 and d.rows
    gapped = CandleSeries(monthly.instrument, monthly.timeframe, monthly.candles[:80] + monthly.candles[85:])
    g = build_dataset(gapped, horizon_bars=3, cost_bps=7.0)
    assert g.dropped["label_crosses_gap"] == 3
    assert all(not (77 <= r["t"] <= 79) for r in g.rows)


# ---------- walk-forward evaluation ----------

def synthetic(n: int, *, seed: int, planted: bool):
    """SYNTHETIC series. ``planted``: a volume spike on bar j (rare, 15%) is followed by one strongly
    positive bar j+1, so ``volume_ratio_20`` at t carries a learnable signal for the label at t.
    Otherwise a driftless random walk with independent random volume (no signal)."""
    rng = random.Random(seed)
    spikes = [rng.random() < 0.15 for _ in range(n)]
    if planted:
        vols = [1000.0 * rng.lognormvariate(0, 0.2) * (3.0 if s else 1.0) for s in spikes]
    else:
        vols = [1000.0 * rng.lognormvariate(0, 0.5) for _ in range(n)]
    closes, c = [], 100.0
    for j in range(n):
        drift = (-0.004 + (0.02 if j >= 1 and spikes[j - 1] else 0.0)) if planted else 0.0
        c *= 1 + drift + rng.gauss(0, 0.008)
        closes.append(c)
    return series_from_closes(closes, volumes=vols)


def _has(mod: str) -> bool:
    try:
        __import__(mod)
    except ImportError:
        return False
    return True


@pytest.fixture(scope="module")
def planted_series():
    return synthetic(600, seed=1, planted=True)


@pytest.fixture(scope="module")
def planted_ds(planted_series):
    return build_dataset(planted_series, horizon_bars=3, cost_bps=5.0)


@pytest.fixture(scope="module")
def noise_ds():
    return build_dataset(synthetic(600, seed=1, planted=False), horizon_bars=3, cost_bps=5.0)


def _agg(out, model, key="precision"):
    return out["models"][model]["aggregate"][key]["mean"]


def test_walk_forward_folds_are_expanding_and_purged_by_bar_index():
    ts = [t for t in range(200) if not 95 <= t <= 104]  # rows dropped mid-series (e.g. a gap)
    folds = walk_forward_folds(ts, 4, purge_bars=5)
    assert len(folds) == 4
    prev_train = 0
    for train, test in folds:
        assert train == list(range(len(train))) and len(train) > prev_train  # expanding, starts at row 0
        prev_train = len(train)
        assert test == list(range(test[0], test[-1] + 1))  # contiguous test block
        assert ts[train[-1]] + 5 < ts[test[0]]
        assert all(ts[i] + 5 >= ts[test[0]] for i in range(train[-1] + 1, test[0]))  # only purged rows skipped
    assert folds[-1][1][-1] == len(ts) - 1
    for bad in ((ts, 0, 3), (ts, 2, -1), (ts[::-1], 2, 3), (ts[:30], 4, 3)):
        with pytest.raises(ValueError):
            walk_forward_folds(*bad)


@pytest.mark.parametrize("gapped", [False, True])
def test_purge_no_train_label_window_overlaps_any_test_row(planted_series, gapped):
    s = planted_series
    if gapped:
        s = CandleSeries(s.instrument, s.timeframe, s.candles[:300] + s.candles[310:])
    d = build_dataset(s, horizon_bars=3, cost_bps=5.0)
    h = d.horizon_bars
    ts = [r["t"] for r in d.rows]
    for train, test in walk_forward_folds(ts, 4, purge_bars=h):
        test_bars = set()
        for i in test:  # every bar a test row touches: its feature bar and its label window
            test_bars.update(range(ts[i], ts[i] + h + 1))
        first_test_t = ts[test[0]]
        for i in train:
            assert not set(range(ts[i] + 1, ts[i] + h + 1)) & test_bars
            assert ts[i] + h < first_test_t
    out = walk_forward_models(d, models=("majority",))
    assert all(f["train_last_t"] + h < f["test_first_t"] for f in out["folds"])
    with pytest.raises(ValueError):
        walk_forward_models(d, purge_bars=h - 1)


def test_output_contract_without_optional_libraries(planted_ds, monkeypatch):
    for name in [m for m in sys.modules if m.split(".")[0] in ("sklearn", "lightgbm")] + ["sklearn", "lightgbm"]:
        monkeypatch.setitem(sys.modules, name, None)  # also blocks submodules cached by earlier tests
    out = walk_forward_models(planted_ds, n_folds=4)
    assert out["disclaimer"] == WALK_FORWARD_DISCLAIMER == (
        "Classification metrics on chronological out-of-sample folds; not trade win probabilities, "
        "not evidence of a trading edge.")
    assert out["label_version"] == LABEL_VERSION and out["feature_names"] == list(planted_ds.feature_names)
    lr, lgb_res = out["models"]["logistic_regression"], out["models"]["lightgbm"]
    assert (lr["status"], lr["reason"]) == ("SKIPPED", "scikit-learn not installed")
    assert (lgb_res["status"], lgb_res["reason"]) == ("SKIPPED", "lightgbm not installed")
    lgb = out["model_params"]["lightgbm"]
    assert lgb["num_leaves"] <= 15 and lgb["n_estimators"] <= 200 and lgb["min_child_samples"] >= 20
    assert lgb["deterministic"] is True and lgb["n_jobs"] == 1 and lgb["random_state"] == 0
    assert "training rows only" in out["model_params"]["logistic_regression"]["preprocessing"]
    assert len(out["folds"]) == 4
    for f in out["folds"]:
        for k in ("train_first_bar_open_utc", "train_last_bar_open_utc", "test_first_bar_open_utc",
                  "test_last_bar_open_utc"):
            assert f[k].endswith("Z") and f[k].startswith("20")
        assert f["train_last_bar_open_utc"] < f["test_first_bar_open_utc"] <= f["test_last_bar_open_utc"]
    maj, rule = out["models"]["majority"], out["models"]["rule_close_above_sma50"]
    assert maj["status"] == rule["status"] == "OK"
    for f, info in zip(maj["folds"], out["folds"]):
        assert f["n_test"] == info["n_test"] and {"precision", "recall", "base_rate", "n_positives"} <= set(f)
        cal = f["calibration"]
        assert sum(b["count"] for b in cal["reliability_bins"]) == f["n_test"]
        assert 0 <= cal["brier_score"] <= 1
    assert all("calibration" not in f for f in rule["folds"]) and rule["pooled_calibration"] is None
    agg = maj["aggregate"]["base_rate"]
    assert agg["n_folds"] == 4 and agg["std"] is not None and agg["min"] <= agg["mean"] <= agg["max"]
    assert maj["stability"]["folds_total"] == 4
    json.dumps(out)  # serializable


def test_planted_signal_logistic_beats_majority_out_of_sample(planted_ds):
    pytest.importorskip("sklearn")
    out = walk_forward_models(planted_ds, models=("majority", "logistic_regression"))
    lr, maj = out["models"]["logistic_regression"], out["models"]["majority"]
    assert lr["status"] == "OK"
    # Majority predicts the (negative) majority class here, so it has no precision; predicting all
    # positives would score exactly the base rate.
    assert maj["aggregate"]["precision"]["mean"] is None
    assert _agg(out, "logistic_regression") > _agg(out, "logistic_regression", "base_rate") + 0.15
    assert lr["stability"]["above_base_rate_in_every_fold"] is True
    assert lr["aggregate"]["brier_score"]["mean"] < maj["aggregate"]["brier_score"]["mean"]
    n_test = sum(f["n_test"] for f in out["folds"])
    assert sum(b["count"] for b in lr["pooled_calibration"]["reliability_bins"]) == n_test


def test_planted_signal_lightgbm_beats_base_rate_out_of_sample(planted_ds):
    pytest.importorskip("lightgbm")
    out = walk_forward_models(planted_ds, models=("majority", "lightgbm"))
    lgb = out["models"]["lightgbm"]
    assert lgb["status"] == "OK"
    assert _agg(out, "lightgbm") > _agg(out, "lightgbm", "base_rate") + 0.05
    assert lgb["stability"]["folds_precision_above_base_rate"] >= 3


def test_shuffled_train_labels_drop_test_precision_to_base_rate(planted_ds):
    pytest.importorskip("sklearn")
    models = ("logistic_regression", "lightgbm") if _has("lightgbm") else ("logistic_regression",)
    real = walk_forward_models(planted_ds, models=models)
    control = walk_forward_models(planted_ds, models=models, shuffle_train_labels=True)
    assert control["negative_control_shuffled_train_labels"] is True
    for m in models:
        base = _agg(control, m, "base_rate")
        assert base == _agg(real, m, "base_rate")  # test labels are untouched
        prec = _agg(control, m)
        assert prec is None or prec <= base + 0.05
        assert control["models"][m]["stability"]["above_base_rate_in_every_fold"] is False
    assert _agg(real, "logistic_regression") > _agg(control, "logistic_regression") + 0.15


def test_leakage_check_passes_clean_dataset_and_flags_future_feature(planted_series, planted_ds):
    clean = leakage_check(planted_ds, planted_series, n_samples=20, seed=3)
    assert clean["status"] == "PASS" and clean["checked_rows"] == 20 and not clean["mismatches"]
    # Test-only dataset: inject a feature computed from FUTURE bars (the label's own forward return).
    leaked_rows = tuple({**r, "features": {**r["features"], "volume_ratio_20": r["forward_return"]}}
                        for r in planted_ds.rows)
    leaked = dataclasses.replace(planted_ds, rows=leaked_rows)
    res = leakage_check(leaked, planted_series, n_samples=20, seed=3)
    assert res["status"] == "FAIL"
    assert {m["feature"] for m in res["mismatches"]} == {"volume_ratio_20"}
    assert {m["t"] for m in res["mismatches"]} == set(res["checked_t"])
    # A dataset paired with a different series is also flagged.
    assert leakage_check(planted_ds, synthetic(600, seed=2, planted=True), n_samples=5)["status"] == "FAIL"
    with pytest.raises(ValueError):
        leakage_check(planted_ds, planted_series, n_samples=0)


def test_pure_noise_no_model_flagged_and_nothing_called_an_edge(noise_ds):
    out = walk_forward_models(noise_ds)
    assert out["disclaimer"] == WALK_FORWARD_DISCLAIMER
    for name, res in out["models"].items():
        if res["status"] == "SKIPPED":
            continue
        assert res["stability"]["above_base_rate_in_every_fold"] is False, name
        lift = res["aggregate"]["precision_minus_base_rate"]["mean"]
        assert lift is None or abs(lift) < 0.1, name
    body = json.dumps({k: v for k, v in out.items() if k != "disclaimer"}).lower()
    assert "edge" not in body and "profit" not in body and "win probabilit" not in body


def test_walk_forward_is_deterministic_with_a_seed(planted_ds):
    a = walk_forward_models(planted_ds, seed=7, shuffle_train_labels=True)
    b = walk_forward_models(planted_ds, seed=7, shuffle_train_labels=True)
    assert a == b
    for bad in (("majority", "neural_net"), ("majority", "majority"), ()):
        with pytest.raises(ValueError):
            walk_forward_models(planted_ds, models=bad)
