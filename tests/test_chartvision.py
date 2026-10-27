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
    calls = []

    def runner(png, psm):
        calls.append(psm)
        text = title_text if len(calls) == 1 else axis_text
        return [{"text": t, "conf": conf, "left": 0, "top": 0} for t in text.split()]
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
