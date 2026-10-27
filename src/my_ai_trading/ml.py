"""Minimal ML research baseline (PRD §5, R6). Research only; outputs are never trade instructions.

- Features for bar t come ONLY from bars closed at t: ``engine.analyze(series.up_to(close_t + 1))``.
- Label (``LABEL_VERSION``): 1 when the long forward return from the OPEN of bar t+1 to the CLOSE of
  bar t+horizon exceeds the round-trip cost (2 x ``cost_bps``, per-side fee+slippage), else 0. Rows
  whose label window runs past the data end, or crosses a gap, are dropped.
- Splits are chronological with a purge of >= horizon rows between train and test, so no training
  label window overlaps the test period.
- Baselines: majority class, a fixed rule (close > SMA50), and scikit-learn LogisticRegression only
  when scikit-learn is importable (it is optional and not a dependency).
- ``walk_forward_models``: expanding-window chronological folds, purged by bar index, comparing the
  baselines with LogisticRegression and LightGBM (both optional imports, SKIPPED when missing).
  Reports precision/recall/base rate per fold, mean/std across folds, a calibration table and
  Brier score for probability outputs, and fold-to-fold stability. Preprocessing is fit on each
  fold's training rows only. Neural models (PyTorch) are a possible future option, not used here.
- ``leakage_check``: recomputes features for a random sample of rows from a series truncated at the
  row's close and reports any mismatch (a sign that a feature used future bars).
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from math import ceil, isclose
from typing import Any, Sequence

from .domain import CandleSeries
from .engine import analyze, utc_iso

LABEL_VERSION = "fwd_open_t+1_to_close_t+h_gt_round_trip_cost/1"
FEATURE_NAMES = ("close_over_sma50_m1", "close_over_sma25_m1", "atr14_wilder_over_close",
                 "volume_ratio_20", "rsi14_wilder_scaled")
METRICS_DISCLAIMER = ("Classification metrics on the chronological TEST split only. Precision is not a trade win "
                      "probability and ignores position sizing, path and costs beyond the label threshold.")


@dataclass(frozen=True)
class Dataset:
    rows: tuple[dict[str, Any], ...]
    horizon_bars: int
    cost_bps: float
    label_version: str = LABEL_VERSION
    feature_names: tuple[str, ...] = FEATURE_NAMES
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def X(self) -> list[list[float]]:
        return [[r["features"][k] for k in self.feature_names] for r in self.rows]

    @property
    def y(self) -> list[int]:
        return [r["label"] for r in self.rows]


def features_at(series: CandleSeries, t: int) -> dict[str, float] | None:
    """Features for bar ``t`` from bars closed at its close; ``None`` if any input is missing."""
    as_of = series.candles[t].close_time_ms + 1
    rep = analyze(series.up_to(as_of), as_of_ms=as_of)
    iv = rep["indicator_values"]
    if rep["status"] != "OK":
        return None
    close = rep["last_close"]
    raw = (iv.get("sma50"), iv.get("sma25"), iv.get("atr14_wilder"), iv.get("volume_ratio_20"),
           iv.get("rsi14_wilder"))
    if any(v is None for v in raw):
        return None
    s50, s25, atr, vr, rsi = raw
    return dict(zip(FEATURE_NAMES, (close / s50 - 1, close / s25 - 1, atr / close, vr, rsi / 100)))


def build_dataset(series: CandleSeries, horizon_bars: int, cost_bps: float) -> Dataset:
    if isinstance(horizon_bars, bool) or not isinstance(horizon_bars, int) or horizon_bars < 1:
        raise ValueError("horizon_bars must be an integer >= 1")
    if isinstance(cost_bps, bool) or not isinstance(cost_bps, (int, float)) or not cost_bps >= 0:
        raise ValueError("cost_bps must be a nonnegative number")
    candles, tf = series.candles, series.timeframe
    threshold = 2 * cost_bps / 1e4
    rows: list[dict[str, Any]] = []
    dropped = {"label_past_end": 0, "label_crosses_gap": 0, "missing_features": 0}
    for t in range(len(candles)):
        end = t + horizon_bars
        if end >= len(candles):
            dropped["label_past_end"] += 1
            continue
        # Calendar-aware (1M bars vary in length): the window is contiguous only when exactly
        # horizon_bars bar steps separate bar t from bar t+horizon (CandleSeries enforces order/alignment).
        if tf.bars_between(candles[t].open_time_ms, candles[end].open_time_ms) != horizon_bars:
            dropped["label_crosses_gap"] += 1
            continue
        feats = features_at(series, t)
        if feats is None:
            dropped["missing_features"] += 1
            continue
        fwd = candles[end].close / candles[t + 1].open - 1
        rows.append({"t": t, "bar_open_time_ms": candles[t].open_time_ms, "features": feats,
                     "forward_return": fwd, "label": int(fwd > threshold)})
    return Dataset(tuple(rows), horizon_bars, float(cost_bps), dropped=dropped)


def chronological_split(n: int, test_fraction: float, purge_bars: int) -> tuple[list[int], list[int]]:
    """Train = rows before ``test_start - purge_bars``; test = the last ``ceil(n*test_fraction)`` rows."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise ValueError("n must be an integer >= 2")
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be in (0, 1)")
    if isinstance(purge_bars, bool) or not isinstance(purge_bars, int) or purge_bars < 0:
        raise ValueError("purge_bars must be an integer >= 0")
    test_start = n - ceil(n * test_fraction)
    train_end = test_start - purge_bars
    if train_end < 1 or test_start >= n:
        raise ValueError("not enough rows for this split and purge")
    return list(range(train_end)), list(range(test_start, n))


def classification_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict[str, Any]:
    tp = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 1)
    pred_pos, pos = sum(y_pred), sum(y_true)
    return {"n_test": len(y_true), "n_positives": pos, "n_predicted_positive": pred_pos,
            "base_rate": pos / len(y_true) if y_true else None,
            "precision": tp / pred_pos if pred_pos else None,
            "recall": tp / pos if pos else None}


def _logistic(X_tr, y_tr, X_te) -> dict[str, Any] | list[int]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return {"status": "SKIPPED", "reason": "scikit-learn not installed"}
    if len(set(y_tr)) < 2:
        return {"status": "SKIPPED", "reason": "training labels contain a single class"}
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(X_tr, y_tr)
    return [int(v) for v in model.predict(X_te)]


def run_baselines(ds: Dataset, *, test_fraction: float = 0.3, purge_bars: int | None = None) -> dict[str, Any]:
    purge = ds.horizon_bars if purge_bars is None else purge_bars
    if purge < ds.horizon_bars:
        raise ValueError("purge_bars must be >= horizon_bars to prevent label-overlap leakage")
    train, test = chronological_split(len(ds.rows), test_fraction, purge)
    X, y = ds.X, ds.y
    X_tr, y_tr = [X[i] for i in train], [y[i] for i in train]
    X_te, y_te = [X[i] for i in test], [y[i] for i in test]
    majority = int(sum(y_tr) * 2 > len(y_tr))
    i50 = ds.feature_names.index("close_over_sma50_m1")
    out: dict[str, Any] = {
        "label_version": ds.label_version, "horizon_bars": ds.horizon_bars, "cost_bps_per_side": ds.cost_bps,
        "split": {"n_train": len(train), "n_test": len(test), "purge_rows": purge,
                  "train_last_t": ds.rows[train[-1]]["t"], "test_first_t": ds.rows[test[0]]["t"]},
        "majority_class": {"predicts": majority, **classification_metrics(y_te, [majority] * len(y_te))},
        "rule_close_above_sma50": classification_metrics(y_te, [int(x[i50] > 0) for x in X_te]),
        "disclaimer": METRICS_DISCLAIMER,
        "future_options": ("LightGBM is optional and evaluated only in walk_forward_models; PyTorch models "
                           "are possible later; neither is required."),
    }
    lr = _logistic(X_tr, y_tr, X_te)
    out["logistic_regression"] = lr if isinstance(lr, dict) else {"status": "OK", **classification_metrics(y_te, lr)}
    return out


# ---------- walk-forward model evaluation ----------

WALK_FORWARD_VERSION = "walk_forward_models/1"
WALK_FORWARD_DISCLAIMER = ("Classification metrics on chronological out-of-sample folds; not trade win "
                           "probabilities, not evidence of a trading edge.")
MODEL_NAMES = ("majority", "rule_close_above_sma50", "logistic_regression", "lightgbm")
CALIBRATION_BINS = 10
DECISION_THRESHOLD = 0.5
# A fold counts as "precision above base rate" only when precision beats that fold's test base rate
# by at least MIN_LIFT with at least MIN_PREDICTED_POSITIVE predicted positives. This is a
# descriptive stability count, not a significance test.
MIN_LIFT, MIN_PREDICTED_POSITIVE = 0.05, 10
MIN_TRAIN_ROWS, MIN_TEST_ROWS = 20, 5


def _logistic_params(seed: int) -> dict[str, Any]:
    return {"max_iter": 1000, "C": 1.0, "random_state": seed}


def _lightgbm_params(seed: int) -> dict[str, Any]:
    # Small, fixed and single-threaded so results are reproducible; never tuned on test folds.
    return {"n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15, "max_depth": 4,
            "min_child_samples": 20, "subsample": 1.0, "colsample_bytree": 1.0, "reg_lambda": 1.0,
            "random_state": seed, "deterministic": True, "force_row_wise": True, "n_jobs": 1,
            "verbose": -1}


def walk_forward_folds(ts: Sequence[int], n_folds: int, purge_bars: int) -> list[tuple[list[int], list[int]]]:
    """Expanding-window folds over rows whose bar indices are ``ts`` (strictly increasing).

    Rows are cut into ``n_folds + 1`` contiguous blocks; fold k tests on block k (k = 1..n_folds)
    and trains on every earlier row whose bar index ``t`` satisfies ``t + purge_bars < first test t``.
    The purge uses bar indices, not row positions, so rows dropped for gaps or warm-up cannot
    shrink it. Returns (train_row_positions, test_row_positions) per fold.
    """
    if isinstance(n_folds, bool) or not isinstance(n_folds, int) or n_folds < 1:
        raise ValueError("n_folds must be an integer >= 1")
    if isinstance(purge_bars, bool) or not isinstance(purge_bars, int) or purge_bars < 0:
        raise ValueError("purge_bars must be an integer >= 0")
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise ValueError("row bar indices must be strictly increasing")
    n = len(ts)
    bounds = [round(i * n / (n_folds + 1)) for i in range(n_folds + 2)]
    folds = []
    for k in range(1, n_folds + 1):
        test = list(range(bounds[k], bounds[k + 1]))
        if len(test) < MIN_TEST_ROWS:
            raise ValueError("not enough rows for this many folds")
        cutoff = ts[test[0]] - purge_bars
        train = [i for i in range(test[0]) if ts[i] < cutoff]
        if len(train) < MIN_TRAIN_ROWS:
            raise ValueError("not enough training rows before a test fold after the purge")
        folds.append((train, test))
    return folds


def _r6(v: float | None) -> float | None:
    return None if v is None else round(v, 6)


def _calibration(y: Sequence[int], p: Sequence[float]) -> dict[str, Any]:
    """Reliability table (equal-width probability bins) and Brier score."""
    bins = []
    for b in range(CALIBRATION_BINS):
        lo, hi = b / CALIBRATION_BINS, (b + 1) / CALIBRATION_BINS
        idx = [i for i, v in enumerate(p) if lo <= v < hi or (b == CALIBRATION_BINS - 1 and v == 1.0)]
        bins.append({"bin": [lo, hi], "count": len(idx),
                     "mean_predicted": _r6(sum(p[i] for i in idx) / len(idx)) if idx else None,
                     "observed_frequency": _r6(sum(y[i] for i in idx) / len(idx)) if idx else None})
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(y) if y else None
    return {"brier_score": _r6(brier), "reliability_bins": bins}


def _fit_predict_proba(name: str, X_tr, y_tr, X_te, seed: int) -> list[float] | dict[str, str]:
    """Positive-class probabilities for ``X_te``, or a SKIPPED record. Fitting sees training rows only."""
    if len(set(y_tr)) < 2:
        return {"status": "SKIPPED", "reason": "training labels contain a single class"}
    try:
        import numpy as np
    except ImportError:
        return {"status": "SKIPPED", "reason": "numpy not installed"}
    Xa, ya, Xb = np.asarray(X_tr, dtype=float), np.asarray(y_tr, dtype=int), np.asarray(X_te, dtype=float)
    if name == "logistic_regression":
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            return {"status": "SKIPPED", "reason": "scikit-learn not installed"}
        model = make_pipeline(StandardScaler(), LogisticRegression(**_logistic_params(seed)))
    else:
        try:
            from lightgbm import LGBMClassifier
        except ImportError:
            return {"status": "SKIPPED", "reason": "lightgbm not installed"}
        model = LGBMClassifier(**_lightgbm_params(seed))
    model.fit(Xa, ya)
    return [float(v) for v in model.predict_proba(Xb)[:, 1]]


def _predict(name: str, ds: Dataset, X: list[list[float]], y: Sequence[int], train: list[int],
             test: list[int], seed: int) -> tuple[list[int], list[float] | None] | dict[str, str]:
    X_te = [X[i] for i in test]
    y_tr = [y[i] for i in train]
    if name == "majority":
        cls = int(sum(y_tr) * 2 > len(y_tr))
        return [cls] * len(test), [sum(y_tr) / len(y_tr)] * len(test)
    if name == "rule_close_above_sma50":
        i50 = ds.feature_names.index("close_over_sma50_m1")
        return [int(x[i50] > 0) for x in X_te], None
    proba = _fit_predict_proba(name, [X[i] for i in train], y_tr, X_te, seed)
    if isinstance(proba, dict):
        return proba
    return [int(v >= DECISION_THRESHOLD) for v in proba], proba


def _mean_std(values: Sequence[float | None]) -> dict[str, Any]:
    v = [x for x in values if x is not None]
    return {"mean": _r6(statistics.fmean(v)) if v else None,
            "std": _r6(statistics.stdev(v)) if len(v) >= 2 else None,
            "min": _r6(min(v)) if v else None, "max": _r6(max(v)) if v else None, "n_folds": len(v)}


def walk_forward_models(ds: Dataset, *, n_folds: int = 4, purge_bars: int | None = None,
                        models: Sequence[str] = MODEL_NAMES, seed: int = 0,
                        shuffle_train_labels: bool = False) -> dict[str, Any]:
    """Compare models on expanding-window chronological folds (PRD §5, F10). Research only.

    ``shuffle_train_labels`` is a negative control: training labels are permuted (seeded) inside
    each fold while test labels stay intact, so every fitted model should fall back to roughly
    base-rate precision. The output does not rank models or call anything tradable.
    """
    purge = ds.horizon_bars if purge_bars is None else purge_bars
    if isinstance(purge, bool) or not isinstance(purge, int) or purge < ds.horizon_bars:
        raise ValueError("purge_bars must be an integer >= horizon_bars to prevent label-overlap leakage")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    models = tuple(models)
    if not models or len(set(models)) != len(models) or any(m not in MODEL_NAMES for m in models):
        raise ValueError(f"models must be distinct names from {MODEL_NAMES}")
    ts = [r["t"] for r in ds.rows]
    folds = walk_forward_folds(ts, n_folds, purge)
    X, y = ds.X, ds.y
    rng = random.Random(seed)

    fold_info = []
    per_model: dict[str, list[dict[str, Any]]] = {m: [] for m in models}
    pooled: dict[str, tuple[list[int], list[float]]] = {m: ([], []) for m in models}
    for k, (train, test) in enumerate(folds):
        fold_info.append({
            "fold": k, "n_train": len(train), "n_test": len(test), "purge_bars": purge,
            "train_first_bar_open_utc": utc_iso(ds.rows[train[0]]["bar_open_time_ms"]),
            "train_last_bar_open_utc": utc_iso(ds.rows[train[-1]]["bar_open_time_ms"]),
            "test_first_bar_open_utc": utc_iso(ds.rows[test[0]]["bar_open_time_ms"]),
            "test_last_bar_open_utc": utc_iso(ds.rows[test[-1]]["bar_open_time_ms"]),
            "train_last_t": ts[train[-1]], "test_first_t": ts[test[0]], "test_last_t": ts[test[-1]]})
        y_fit: Sequence[int] = y
        if shuffle_train_labels:
            y_tr = [y[i] for i in train]
            rng.shuffle(y_tr)
            y_fit = list(y)
            for i, v in zip(train, y_tr):
                y_fit[i] = v
        y_te = [y[i] for i in test]
        for m in models:
            res = _predict(m, ds, X, y_fit, train, test, seed)
            if isinstance(res, dict):
                per_model[m].append({"fold": k, **res})
                continue
            pred, proba = res
            met = classification_metrics(y_te, pred)
            rec: dict[str, Any] = {"fold": k, "status": "OK",
                                   **{key: _r6(v) if isinstance(v, float) else v for key, v in met.items()}}
            rec["precision_minus_base_rate"] = (None if met["precision"] is None
                                                else _r6(met["precision"] - met["base_rate"]))
            if proba is not None:
                rec["calibration"] = _calibration(y_te, proba)
                pooled[m][0].extend(y_te)
                pooled[m][1].extend(proba)
            per_model[m].append(rec)

    results: dict[str, Any] = {}
    for m in models:
        ok = [f for f in per_model[m] if f["status"] == "OK"]
        if not ok:
            results[m] = {"status": "SKIPPED", "reason": per_model[m][0]["reason"], "folds": per_model[m]}
            continue
        above = [f for f in ok if f["precision_minus_base_rate"] is not None
                 and f["precision_minus_base_rate"] >= MIN_LIFT
                 and f["n_predicted_positive"] >= MIN_PREDICTED_POSITIVE]
        agg = {key: _mean_std([f[key] for f in ok])
               for key in ("precision", "recall", "base_rate", "n_positives", "n_predicted_positive",
                           "precision_minus_base_rate")}
        if any("calibration" in f for f in ok):
            agg["brier_score"] = _mean_std([f["calibration"]["brier_score"] for f in ok])
        results[m] = {
            "status": "OK" if len(ok) == len(folds) else "PARTIAL",
            "folds": per_model[m],
            "aggregate": agg,
            "stability": {
                "folds_evaluated": len(ok), "folds_total": len(folds),
                "folds_without_predicted_positives": sum(1 for f in ok if f["precision"] is None),
                "folds_precision_above_base_rate": len(above),
                "above_base_rate_in_every_fold": len(above) == len(folds),
                "criterion": (f"precision - test base rate >= {MIN_LIFT} with >= {MIN_PREDICTED_POSITIVE} "
                              "predicted positives; descriptive count, not a significance test"),
            },
            "pooled_calibration": _calibration(*pooled[m]) if pooled[m][0] else None,
        }

    return {
        "version": WALK_FORWARD_VERSION,
        "label_version": ds.label_version, "feature_names": list(ds.feature_names),
        "horizon_bars": ds.horizon_bars, "cost_bps_per_side": ds.cost_bps, "n_rows": len(ds.rows),
        "scheme": "expanding window; contiguous chronological test blocks; purge by bar index",
        "n_folds": len(folds), "purge_bars": purge, "seed": seed,
        "negative_control_shuffled_train_labels": shuffle_train_labels,
        "decision_threshold": DECISION_THRESHOLD, "calibration_bins": CALIBRATION_BINS,
        "folds": fold_info,
        "model_params": {
            "majority": "predicts the training majority class; probability = training positive rate",
            "rule_close_above_sma50": "predicts 1 when close > SMA50; no fitting, no probability",
            "logistic_regression": {"preprocessing": "StandardScaler fit on each fold's training rows only",
                                    **_logistic_params(seed)},
            "lightgbm": {"preprocessing": "none (tree model)", **_lightgbm_params(seed)},
        },
        "models": results,
        "disclaimer": WALK_FORWARD_DISCLAIMER,
    }


def leakage_check(ds: Dataset, series: CandleSeries, *, n_samples: int = 25, seed: int = 0) -> dict[str, Any]:
    """Recompute features for a seeded random sample of rows from ``series.up_to(close_t + 1)``.

    A mismatch means a stored feature used information not available at the close of bar t (or the
    dataset does not belong to ``series``). ``PASS`` only when every sampled row matches.
    """
    if isinstance(n_samples, bool) or not isinstance(n_samples, int) or n_samples < 1:
        raise ValueError("n_samples must be an integer >= 1")
    sample = sorted(random.Random(seed).sample(list(ds.rows), min(n_samples, len(ds.rows))),
                    key=lambda r: r["t"])
    mismatches: list[dict[str, Any]] = []
    for r in sample:
        t = r["t"]
        if not 0 <= t < len(series.candles) or series.candles[t].open_time_ms != r["bar_open_time_ms"]:
            mismatches.append({"t": t, "feature": None, "reason": "row does not match a bar of the series"})
            continue
        fresh = features_at(series.up_to(series.candles[t].close_time_ms + 1), t)
        for k in ds.feature_names:
            got, want = r["features"].get(k), None if fresh is None else fresh.get(k)
            if got is None or want is None or not isclose(got, want, rel_tol=1e-12, abs_tol=1e-15):
                mismatches.append({"t": t, "feature": k, "dataset_value": got, "recomputed_value": want})
    return {"status": "PASS" if not mismatches else "FAIL", "checked_rows": len(sample),
            "checked_t": [r["t"] for r in sample], "mismatches": mismatches}
