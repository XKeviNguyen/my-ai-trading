"""R6 ML research baseline: causal features, purged chronological split, optional scikit-learn."""
import math
import sys

import pytest

from my_ai_trading.domain import CandleSeries
from my_ai_trading.fixtures import series_from_closes
from my_ai_trading.ml import (LABEL_VERSION, build_dataset, chronological_split, features_at,
                              run_baselines)

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
