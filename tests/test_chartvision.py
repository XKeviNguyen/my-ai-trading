"""PRD F08: local OpenCV/OCR screenshot analysis. Charts are drawn synthetically; no real screenshots."""
import shutil
import struct
import zlib

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from fakes import make_exchange, PERP  # noqa: E402
from my_ai_trading.chartvision import (analyze_screenshot, candle_geometry, cross_check, decode,  # noqa: E402
                                       image_dimensions, parse_chart_text, parse_tsv)
from my_ai_trading.copilot import Copilot  # noqa: E402
from my_ai_trading.store import Store, UserSettings  # noqa: E402


def draw_chart(n=30, rising=True, w=900, h=500, title="SANDUSDT Perpetual 1h MA(25)", labels=("0.0500", "0.0480")):
    img = np.full((h, w, 3), 20, dtype=np.uint8)  # dark theme
    for i in range(n):
        x = 60 + i * 24
        base = 380 - i * 8 if rising else 120 + i * 8
        up = (i % 4) != 3 if rising else (i % 4) == 3
        color = (80, 200, 60) if up else (60, 60, 220)  # BGR green / red
        cv2.rectangle(img, (x, base - 30), (x + 12, base), color, -1)
    cv2.putText(img, title, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (230, 230, 230), 2)
    for k, lab in enumerate(labels):
        cv2.putText(img, lab, (w - 100, 150 + k * 150), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 230, 230), 2)
    ok, png = cv2.imencode(".png", img)
    return png.tobytes()


def fake_ocr(title_text, axis_text, conf=92.0):
    """Title reads have no whitelist; each axis row read (numeric whitelist) returns the next axis word."""
    axis = axis_text.split()
    calls = []

    def runner(png, psm, whitelist=None):
        if whitelist is None:
            return [{"text": t, "conf": conf, "left": 0, "top": 0} for t in title_text.split()]
        calls.append(psm)
        return [{"text": axis[(len(calls) - 1) % len(axis)], "conf": conf, "left": 0, "top": 0}]
    return runner


def test_header_dimensions_and_decompression_bomb_rejected_before_decode():
    png = draw_chart()
    assert image_dimensions(png) == (900, 500)
    ok, jpg = cv2.imencode(".jpg", cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR))
    assert image_dimensions(jpg.tobytes()) == (900, 500)
    ihdr = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)
    bomb = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    with pytest.raises(ValueError, match="exceed"):
        decode(bomb + b"\x00" * 100)
    with pytest.raises(ValueError):
        decode(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)
    with pytest.raises(ValueError):
        decode(b"<svg/>")


def test_candle_geometry_detects_colors_and_visual_drift():
    up = candle_geometry(decode(draw_chart(rising=True)))
    assert up["state"] == "DETECTED" and up["candles_detected"] == 30
    assert up["green"] > up["red"] and up["visual_drift"] == "UP" and up["last_visible_candle_color"] == "green"
    down = candle_geometry(decode(draw_chart(rising=False)))
    assert down["visual_drift"] == "DOWN" and down["red"] > down["green"]
    blank = np.full((300, 300, 3), 255, np.uint8)
    assert candle_geometry(blank)["state"] == "INSUFFICIENT_VISUAL_DATA"


def test_parse_chart_text_symbols_timeframes_indicators_prices():
    title = [{"text": t, "conf": 90} for t in "SANDUSDT Perpetual 1h · MA(25) EMA 7 4H".split()]
    axis = [{"text": t, "conf": c} for t, c in (("0.0500", 91), ("0.0480", 88), ("12a", 95), ("0.0470", 30))]
    p = parse_chart_text(title, axis)
    assert p["symbols"] == ["SANDUSDT"] and p["market_hint"] == "usdm_perpetual"
    assert p["timeframes"] == ["1h", "4h"] and p["indicator_labels"] == ["EMA7", "MA25"]
    assert p["price_axis_labels"] == [0.048, 0.05]  # low-confidence and non-numeric words dropped


def test_parse_tsv_skips_headers_and_negative_confidence():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t96.5\tSANDUSDT\n"
           "4\t1\t1\t1\t1\t0\t0\t0\t0\t0\t-1\t\n")
    assert parse_tsv(tsv) == [{"text": "SANDUSDT", "conf": 96.5, "left": 10, "top": 20}]


def test_cross_check_symbol_and_price_axis_against_exchange():
    ex, svc = make_exchange()
    snap = svc.snapshot(PERP, ("1h",))
    lo = min(c.low for c in snap["_series"]["1h"].candles[-300:])
    agree = cross_check({"symbols": ["SANDUSDT"], "timeframes": ["1h"], "price_axis_labels": [lo * 1.01]},
                        expected_symbol="SANDUSDT", snapshot=snap)
    assert [c["result"] for c in agree] == ["MATCH", "AGREES"]
    bad = cross_check({"symbols": ["BTCUSDT"], "timeframes": [], "price_axis_labels": [65000.0]},
                      expected_symbol="SANDUSDT", snapshot=snap)
    assert [c["result"] for c in bad] == ["MISMATCH", "DISAGREES"]
    none = cross_check({"symbols": [], "timeframes": [], "price_axis_labels": []}, expected_symbol="SANDUSDT",
                       snapshot=None)
    assert [c["result"] for c in none] == ["NOT_FOUND", "UNVERIFIABLE"]


def test_analyze_screenshot_end_to_end_with_injected_ocr():
    out = analyze_screenshot(draw_chart(), expected_symbol="BTCUSDT",
                             ocr_runner=fake_ocr("SANDUSDT Perpetual 1h", "0.0500 0.0480"))
    assert out["status"] == "OK" and out["cost_tokens"] == 0 and "not exact OHLCV" in out["label"]
    assert out["ocr"]["parsed"]["symbols"] == ["SANDUSDT"]
    assert out["cross_checks"][0]["result"] == "MISMATCH" and out["ambiguities"]


def test_missing_tesseract_reports_unavailable(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = analyze_screenshot(draw_chart())
    assert out["status"] == "OK" and out["ocr"]["status"] == "OCR_UNAVAILABLE"
    assert out["geometry"]["state"] == "DETECTED"  # OpenCV part still works


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_synthetic_title():
    out = analyze_screenshot(draw_chart(title="SANDUSDT 1h MA(25)"), expected_symbol="SANDUSDT")
    assert out["ocr"]["status"] == "OK"
    assert "SANDUSDT" in out["ocr"]["parsed"]["symbols"]


def test_copilot_runs_local_analysis_without_ai_and_without_consent():
    store = Store()
    store.save_settings(UserSettings())
    out = Copilot(store, None).analyze_chart_image(draw_chart(), "patterns?", None, expected_symbol="SANDUSDT")
    assert out["route"] == "local" and out["local_analysis"]["geometry"]["state"] == "DETECTED"
    assert out["visual_hypotheses"] is None and "No AI provider" in out["fallback_reason"]


def test_without_opencv_fake_images_are_still_rejected(monkeypatch):
    import sys
    from fakes import tiny_png
    monkeypatch.setitem(sys.modules, "cv2", None)  # simulate the vision extra not installed
    with pytest.raises(ValueError):
        analyze_screenshot(b"\x89PNG\r\n\x1a\n000")
    out = analyze_screenshot(tiny_png())
    assert out["status"] == "OPENCV_UNAVAILABLE"


# ---------- price-axis OCR: parsing and scale sanity (no tesseract needed) ----------

from my_ai_trading.chartvision import (axis_consistency, fits_grid, parse_axis_number,  # noqa: E402
                                       read_price_axis, run_ocr, tesseract_runner)


def test_parse_axis_number_separators_and_leading_zeros():
    assert parse_axis_number("65,312.50") == 65312.5
    assert parse_axis_number("1,234,567") == 1234567.0
    assert parse_axis_number("0.000123") == 0.000123
    assert parse_axis_number("0,0534") == 0.0534      # a decimal comma: thousands groups never start with 0
    assert parse_axis_number("13,10") == 13.1         # not a 3-digit group, so a decimal comma
    assert parse_axis_number("1,250") == 1250.0       # a valid thousands group wins over a decimal comma
    assert parse_axis_number(" 2200.0 ") == 2200.0
    for bad in ("", ".565", "1,2,3", "1..2", "12a", "-5", "1.2.3", "1,23,456", "1234567890"):
        assert parse_axis_number(bad) is None, bad


def _axis(*rows):
    """(text, y) rows -> OCR words shaped like read_price_axis output."""
    return [{"text": t, "conf": 95.0, "left": 0, "top": y - 5, "height": 10} for t, y in rows]


def test_axis_consistency_flags_misreads_off_the_scale():
    words = _axis(("2900.0", 100), ("2800.0", 150), ("2708.0", 200), ("2600.0", 250), ("26000", 300),
                  ("2400.0", 350))
    check = axis_consistency(words)
    assert check["step"] == 100.0 and check["slope"] < 0
    assert check["consistent"] == {0, 1, 3, 5}  # 2708 is off the step grid, 26000 off the line
    p = parse_chart_text([], words)
    assert p["price_axis_labels"] == [2400.0, 2600.0, 2800.0, 2900.0]
    assert p["price_axis_outliers"] == [2708.0, 26000.0] and p["price_axis_step"] == 100.0


def test_axis_consistency_tolerates_a_hidden_label_and_rejects_constant_offsets():
    # the 1.6 label is hidden behind the price tag: a double gap, still one step per spacing
    words = _axis(("1.9", 86), ("1.8", 147), ("1.7", 209), ("1.5", 331), ("1.4", 392), ("1.3", 454))
    assert axis_consistency(words)["consistent"] == set(range(6))
    # every label misread by the same offset still looks evenly spaced, but is not on whole steps
    shifted = _axis(("1,708,000", 70), ("1,658,000", 135), ("1,608,000", 200), ("1,558,000", 265))
    assert axis_consistency(shifted)["consistent"] == set()


def test_axis_consistency_needs_three_labels_and_a_downward_scale():
    two = _axis(("0.0500", 100), ("0.0480", 200))
    assert axis_consistency(two) == {"step": None, "consistent": {0, 1}}  # too few to judge: reported as read
    upside_down = _axis(("1.0", 100), ("1.1", 150), ("1.2", 200), ("1.3", 250))
    assert axis_consistency(upside_down)["step"] is None
    low_conf = [dict(w, conf=10.0) for w in _axis(("3.0", 100), ("2.0", 150), ("1.0", 200))]
    assert axis_consistency(low_conf) == {"step": None, "consistent": set()}


def test_fits_grid_is_exact():
    check = {"step": 0.005, "slope": -0.0001, "intercept": 0.6}
    assert fits_grid(0.59, 100, check)
    assert not fits_grid(0.591, 90, check)   # off the step grid
    assert not fits_grid(0.58, 100, check)   # a whole step, but at the wrong height


def _axis_image(labels, dark=True):
    img = np.full((80 + 50 * len(labels), 700, 3), 25 if dark else 255, np.uint8)
    color = (210, 210, 210) if dark else (40, 40, 40)
    for k, lab in enumerate(labels):
        cv2.putText(img, lab, (600, 60 + 50 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    y = 60 + 50 * len(labels) - 10
    cv2.rectangle(img, (595, y - 16), (699, y + 6), (8, 153, 129), -1)  # a colored price tag ...
    cv2.putText(img, "9.99", (604, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)  # ... is not a row
    return img


def test_read_price_axis_cuts_one_light_row_image_per_label():
    calls = []

    def runner(png, psm, whitelist=None):
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        calls.append((psm, whitelist, img.mean() > 128))  # dark text on a light background
        return []

    words = read_price_axis(_axis_image(["13.30", "13.25", "13.20", "13.15", "13.10"]), runner)
    assert len(words) == 5 and all(w["text"] == "" for w in words)
    assert set(calls) == {(7, "0123456789.,", True)}  # one text line per call, numeric whitelist


def test_axis_ocr_time_is_bounded_and_engine_failure_is_reported(monkeypatch):
    import my_ai_trading.chartvision as cvm
    calls = []
    monkeypatch.setattr(cvm, "AXIS_DEADLINE_S", -1.0)  # deadline already passed: no OCR call may start
    words = read_price_axis(_axis_image(["3.0", "2.0", "1.0"]), lambda *a: calls.append(a) or [])
    assert len(words) == 3 and calls == []
    monkeypatch.setattr(cvm, "AXIS_DEADLINE_S", 10.0)

    def broken(png, psm, whitelist=None):
        raise cvm.OcrUnavailable("OCR timed out")
    assert run_ocr(_axis_image(["3.0", "2.0", "1.0"]), broken)["status"] == "OCR_UNAVAILABLE"


def test_tesseract_runner_uses_fixed_argv_without_shell(monkeypatch):
    import subprocess
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout=b"level\n", stderr=b"")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tesseract_runner(b"png", 7, "0123456789.,") == []
    assert seen["argv"] == ["/usr/bin/tesseract", "stdin", "stdout", "--psm", "7", "--oem", "1", "--dpi", "300",
                            "-c", "tessedit_char_whitelist=0123456789.,", "tsv"]
    assert seen["input"] == b"png" and seen["timeout"] == 15 and "shell" not in seen
    assert seen["env"]["OMP_THREAD_LIMIT"] == "1"
    with pytest.raises(ValueError):
        tesseract_runner(b"png", 7, "0-9;rm")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_a_clean_axis_in_both_themes():
    labels = ["0.000140", "0.000130", "0.000120", "0.000110", "0.000100", "0.000090"]
    for dark in (True, False):
        out = run_ocr(_axis_image(labels, dark))
        assert out["status"] == "OK"
        p = out["parsed"]
        assert p["price_axis_labels"] == sorted(float(x) for x in labels), dark
        assert p["price_axis_outliers"] == [] and p["price_axis_step"] == 0.00001
