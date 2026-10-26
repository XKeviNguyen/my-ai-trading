"""Local chart-screenshot analysis (PRD F08): OpenCV geometry and Tesseract OCR, zero tokens.

A screenshot is a picture, not market data. Every result here is a visual hypothesis with its
method and confidence, and numbers read from an image are cross-checked against exchange OHLCV,
never used in place of it. Images are processed in memory and never stored or sent anywhere.

OpenCV comes from the optional ``vision`` extra. OCR runs the system ``tesseract`` program
(``sudo apt install tesseract-ocr``) with fixed arguments and no shell. Each part reports
UNAVAILABLE when its dependency is missing.
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
from statistics import median
from typing import Any, Callable

from .copilot import validate_image
from .domain import Timeframe

METHOD_VERSION = "chartvision/1"
MAX_SIDE, MAX_PIXELS = 8_192, 16_000_000  # checked from the header before decoding (decompression bombs)
OCR_TIMEOUT_S = 15
LABEL = "Local image analysis — visual hypotheses from a screenshot, not exact OHLCV"

OcrRunner = Callable[[bytes, int], list[dict[str, Any]]]


class OcrUnavailable(Exception):
    pass


# ---------- safe decode ----------

def image_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) from the PNG/JPEG/WEBP header, without decoding pixels."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
        raise ValueError("JPEG size not found")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L":
            b = int.from_bytes(data[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
    raise ValueError("could not read image size")


def decode(data: bytes):
    """Validated BGR image array. Raises ValueError for unsafe or undecodable input."""
    validate_image(data)
    w, h = image_dimensions(data)
    if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE) or w * h > MAX_PIXELS:
        raise ValueError(f"image dimensions {w}x{h} exceed {MAX_SIDE}px per side / {MAX_PIXELS} pixels")
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("image could not be decoded")
    return img


# ---------- geometry (OpenCV) ----------

def candle_geometry(img) -> dict[str, Any]:
    """Find green/red candle bodies by color and shape. Uncalibrated; describes the picture only."""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (40, 70, 70), (90, 255, 255))
    red = cv2.inRange(hsv, (0, 70, 70), (10, 255, 255)) | cv2.inRange(hsv, (170, 70, 70), (180, 255, 255))
    min_area = max(6, img.shape[0] * img.shape[1] // 200_000)
    bodies = []
    for color, mask in (("green", green), ("red", red)):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w * h >= min_area and h >= w * 0.8 and w <= img.shape[1] // 15:
                bodies.append({"color": color, "x": int(x), "y": int(y), "w": int(w), "h": int(h)})
    bodies.sort(key=lambda b: b["x"])
    out: dict[str, Any] = {"candles_detected": len(bodies),
                           "green": sum(b["color"] == "green" for b in bodies),
                           "red": sum(b["color"] == "red" for b in bodies)}
    if len(bodies) < 6:
        out.update(state="INSUFFICIENT_VISUAL_DATA",
                   note="Fewer than 6 candle-like shapes found; colors or chart style may differ.")
        return out
    third = max(1, len(bodies) // 3)
    mid = [b["y"] + b["h"] / 2 for b in bodies]
    early, late = float(np.mean(mid[:third])), float(np.mean(mid[-third:]))
    span = max(1.0, float(max(mid) - min(mid)))
    drift = (early - late) / span  # image y grows downwards, so a positive drift means higher on screen
    out.update(
        state="DETECTED",
        plot_area_px={"x0": bodies[0]["x"], "x1": bodies[-1]["x"] + bodies[-1]["w"],
                      "y0": min(b["y"] for b in bodies), "y1": max(b["y"] + b["h"] for b in bodies)},
        last_visible_candle_color=bodies[-1]["color"],
        visual_drift="UP" if drift > 0.15 else "DOWN" if drift < -0.15 else "FLAT",
        visual_drift_score=round(drift, 3),
        note="Pixel-based reading of the visible candles; the visible window, scale and colors are unknown.")
    return out


# ---------- OCR (Tesseract via subprocess) ----------

def tesseract_runner(png: bytes, psm: int) -> list[dict[str, Any]]:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrUnavailable("tesseract is not installed (sudo apt install tesseract-ocr)")
    try:
        proc = subprocess.run([exe, "stdin", "stdout", "--psm", str(int(psm)), "tsv"], input=png,
                              capture_output=True, timeout=OCR_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OcrUnavailable("OCR timed out") from exc
    if proc.returncode != 0:
        raise OcrUnavailable("OCR engine failed")
    return parse_tsv(proc.stdout.decode("utf-8", "replace"))


def parse_tsv(tsv: str) -> list[dict[str, Any]]:
    words = []
    for line in tsv.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf >= 0:
            words.append({"text": cols[11].strip(), "conf": conf, "left": int(cols[6]), "top": int(cols[7])})
    return words


def _ocr_ready(region):
    """Gray, upscaled, binarized, dark-on-light: the input Tesseract reads best."""
    import cv2
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if bw.mean() < 128:
        bw = 255 - bw
    ok, png = cv2.imencode(".png", bw)
    if not ok:
        raise ValueError("could not encode OCR region")
    return png.tobytes()


SYMBOL_RE = re.compile(r"\b([A-Z0-9]{2,15})[/\-]?(USDT|USDC|FDUSD|BUSD|USD|BTC|ETH)\b")
TF_RE = re.compile(r"^(1|3|5|15|30)m$|^(1|2|4|6|8|12)[hH]$|^(1|3)[dD]$|^1[wW]$")
MA_RE = re.compile(r"\b(MA|SMA|EMA)\s*\(?\s*(\d{1,3})\s*\)?", re.I)
PRICE_RE = re.compile(r"^\d{1,7}(?:[.,]\d{1,8})?$")


def parse_chart_text(title_words: list[dict[str, Any]], axis_words: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join(w["text"] for w in title_words)
    symbols = sorted({a + b for a, b in SYMBOL_RE.findall(text.upper())})
    tfs = []
    for w in title_words:
        t = w["text"].strip("()[],")
        if TF_RE.match(t):
            code = t.lower()
            if code in Timeframe.all_codes():
                tfs.append(code)
    mas = sorted({f"{k.upper()}{n}" for k, n in MA_RE.findall(text)})
    prices = []
    for w in axis_words:
        t = w["text"].replace(",", "")
        if PRICE_RE.match(t) and w["conf"] >= 60:
            prices.append(float(t))
    market = "usdm_perpetual" if re.search(r"\bPERP(ETUAL)?\b", text.upper()) else None
    return {"symbols": symbols, "timeframes": sorted(set(tfs)), "indicator_labels": mas,
            "price_axis_labels": sorted(prices), "market_hint": market}


def run_ocr(img, runner: OcrRunner = tesseract_runner) -> dict[str, Any]:
    h, w = img.shape[:2]
    title = img[: max(1, h // 8), :]
    axis = img[:, int(w * 0.88):]
    try:
        tw = runner(_ocr_ready(title), 6)
        aw = runner(_ocr_ready(axis), 6)
    except OcrUnavailable as exc:
        return {"status": "OCR_UNAVAILABLE", "reason": str(exc)}
    words = tw + aw
    return {"status": "OK", "engine": "tesseract", "words": len(words),
            "mean_confidence": round(sum(x["conf"] for x in words) / len(words), 1) if words else None,
            "confidence_note": "Tesseract word confidence (0-100); not calibrated, not a probability.",
            "parsed": parse_chart_text(tw, aw)}


# ---------- cross-checks against exchange data ----------

def cross_check(parsed: dict[str, Any] | None, *, expected_symbol: str | None,
                snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not parsed:
        return checks
    if expected_symbol:
        seen = parsed["symbols"]
        checks.append({"check": "symbol", "expected": expected_symbol, "read": seen,
                       "result": "NOT_FOUND" if not seen else "MATCH" if expected_symbol in seen else "MISMATCH"})
    labels = parsed["price_axis_labels"]
    series = (snapshot or {}).get("_series", {})
    tf = next((t for t in parsed["timeframes"] if t in series), None) or next(iter(series), None)
    if not labels or tf is None:
        checks.append({"check": "price_axis", "result": "UNVERIFIABLE",
                       "reason": "no readable axis labels" if not labels else "no exchange data for comparison"})
        return checks
    bars = series[tf].candles[-300:]
    lo, hi = min(c.low for c in bars), max(c.high for c in bars)
    mid = median(labels)
    result = "AGREES" if lo * 0.8 <= mid <= hi * 1.2 else "DISAGREES"
    checks.append({"check": "price_axis", "timeframe": tf, "read_median": mid,
                   "exchange_range_last_300_bars": [lo, hi], "result": result,
                   "note": "Screenshot numbers are only compared with exchange data, never used as prices."})
    return checks


def analyze_screenshot(data: bytes, *, expected_symbol: str | None = None,
                       snapshot: dict[str, Any] | None = None, ocr_runner: OcrRunner = tesseract_runner) -> dict[str, Any]:
    """Raises ValueError for invalid images (callers map it to 422)."""
    out: dict[str, Any] = {"method": METHOD_VERSION, "label": LABEL, "cost_tokens": 0,
                           "privacy": "Processed in memory on this computer; not stored or sent anywhere."}
    try:
        import cv2  # noqa: F401
    except ImportError:
        validate_image(data)
        out["status"] = "OPENCV_UNAVAILABLE"
        out["reason"] = "install the vision extra: pip install -e '.[vision]'"
        return out
    img = decode(data)
    out["status"] = "OK"
    out["image"] = {"width": int(img.shape[1]), "height": int(img.shape[0])}
    out["geometry"] = candle_geometry(img)
    out["ocr"] = run_ocr(img, ocr_runner)
    out["cross_checks"] = cross_check(out["ocr"].get("parsed"), expected_symbol=expected_symbol, snapshot=snapshot)
    ambiguities = []
    if any(c["result"] in ("MISMATCH", "DISAGREES") for c in out["cross_checks"]):
        ambiguities.append("The screenshot disagrees with exchange data; it may show another symbol, venue, "
                           "market type or time window. Exchange data is authoritative.")
    if out["ocr"]["status"] == "OK" and (out["ocr"]["mean_confidence"] or 0) < 60:
        ambiguities.append("Low OCR confidence; treat read text as unreliable.")
    out["ambiguities"] = ambiguities
    return out
