"""The synthetic price-axis OCR benchmark (tools/ocr_benchmark.py): determinism, ground truth, smoke run."""
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ocr_benchmark as bench  # noqa: E402


def test_generator_is_deterministic_and_labels_are_exact():
    a, b = bench.generate(100_003), bench.generate(100_003)
    assert a["image"] == b["image"] and a["labels"] == b["labels"]
    assert bench.generate(100_004)["image"] != a["image"]
    for seed in range(100_000, 100_030):
        s = bench.generate(seed)
        labs = s["labels"]
        assert len(labs) >= 2
        vals = [Decimal(lab["value"]) for lab in labs]
        step = min(abs(v2 - v1) for v1, v2 in zip(vals, vals[1:]))  # a label hidden by the tag doubles a gap
        for lab in labs:
            assert Decimal(lab["text"].replace(",", "")) == Decimal(lab["value"])  # text and value agree
            assert Decimal(lab["value"]) % step == 0  # labels are whole multiples of the step
        ys = [lab["y"] for lab in labs]  # ascending values sit higher on screen (smaller y)
        assert ys == sorted(ys, reverse=True)


def test_splits_do_not_overlap_and_mono_fonts_are_separate():
    dev, held, mono = (set(bench.SPLITS[k]) for k in ("dev", "heldout", "mono_stress"))
    assert not dev & held and not dev & mono and not held & mono
    assert all(bench.generate(s)["conditions"]["font"] not in bench.MONO_FONTS for s in range(100_000, 100_040))


def test_truetype_rasterizer_draws_digits_when_fonts_are_installed():
    fonts = bench.available_ttf()
    if not fonts:
        pytest.skip("no TrueType fonts at the known system paths; the benchmark then uses Hershey fonts only")
    import numpy as np
    pen = bench.Pen(fonts[0].name, 0.5, 1, True)
    img = np.zeros((40, 200, 3), np.uint8)
    pen.draw(img, "0.123", 5, 5, (255, 255, 255))
    ink = img[:, :, 0] > 128
    assert 5 < ink.any(axis=1).sum() < 25 and ink.any(axis=0).sum() > 20  # digit-sized, several glyphs


def test_score_image_matches_one_to_one():
    sample = {"labels": [{"value": "1.5"}, {"value": "1.5"}, {"value": "2.0"}]}
    s = bench.score_image(sample, [1.5, 2.0, 2.5])
    assert (s["matched"], s["missed"], s["extra"]) == (2, [1.5], [2.5])


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_heldout_smoke_accuracy():
    # Full held-out run (400 images): 0.9953 exact-match label accuracy, 0.9993 precision (docs/OCR_BENCHMARK.md).
    # This smoke run uses the first 20 held-out images with a margin for engine/version differences.
    res = bench.evaluate(list(bench.SPLITS["heldout"])[:20], workers=1)
    assert res["labels"] >= 120
    assert res["accuracy_exact"] >= 0.97, res["failures"]
    assert res["precision"] >= 0.98, res["failures"]
