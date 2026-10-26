"""Minimal ML research baseline (PRD §5, R6). Research only; outputs are never trade instructions.

- Features for bar t come ONLY from bars closed at t: ``engine.analyze(series.up_to(close_t + 1))``.
- Label (``LABEL_VERSION``): 1 when the long forward return from the OPEN of bar t+1 to the CLOSE of
  bar t+horizon exceeds the round-trip cost (2 x ``cost_bps``, per-side fee+slippage), else 0. Rows
  whose label window runs past the data end, or crosses a gap, are dropped.
- Splits are chronological with a purge of >= horizon rows between train and test, so no training
  label window overlaps the test period.
- Baselines: majority class, a fixed rule (close > SMA50), and scikit-learn LogisticRegression only
  when scikit-learn is importable (it is optional and not a dependency). Gradient boosting
  (LightGBM) or neural models (PyTorch) are possible future options, not used here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any, Sequence

from .domain import CandleSeries
from .engine import analyze

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
    candles, dur = series.candles, series.timeframe.duration_ms
    threshold = 2 * cost_bps / 1e4
    rows: list[dict[str, Any]] = []
    dropped = {"label_past_end": 0, "label_crosses_gap": 0, "missing_features": 0}
    for t in range(len(candles)):
        end = t + horizon_bars
        if end >= len(candles):
            dropped["label_past_end"] += 1
            continue
        if candles[end].open_time_ms - candles[t].open_time_ms != horizon_bars * dur:
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
        "future_options": "LightGBM / PyTorch models are possible later; not used or required.",
    }
    lr = _logistic(X_tr, y_tr, X_te)
    out["logistic_regression"] = lr if isinstance(lr, dict) else {"status": "OK", **classification_metrics(y_te, lr)}
    return out
