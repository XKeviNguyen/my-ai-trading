"""Local chart-screenshot analysis (PRD F08): OpenCV geometry and Tesseract OCR, zero tokens.

A screenshot is a picture, not market data. Every result here is a visual hypothesis with its
method and confidence, and numbers read from an image are cross-checked against exchange OHLCV,
never used in place of it. Images are processed in memory and never stored or sent anywhere.

OpenCV comes from the optional ``vision`` extra. OCR runs the system ``tesseract`` program
(``sudo apt install tesseract-ocr``) with fixed arguments and no shell. Each part reports
UNAVAILABLE when its dependency is missing.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import time
from statistics import median
from typing import Any, Callable

from .copilot import validate_image
from .domain import Timeframe

METHOD_VERSION = "chartvision/2"
MAX_SIDE, MAX_PIXELS = 8_192, 16_000_000  # checked from the header before decoding (decompression bombs)
OCR_TIMEOUT_S = 15
LABEL = "Local image analysis — visual hypotheses from a screenshot, not exact OHLCV"

OcrRunner = Callable[..., list[dict[str, Any]]]  # (png, psm, whitelist=None) -> words


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

NUMERIC_WHITELIST = "0123456789.,"  # price axes of price charts are non-negative: no minus sign


def tesseract_runner(png: bytes, psm: int, whitelist: str | None = None) -> list[dict[str, Any]]:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrUnavailable("tesseract is not installed (sudo apt install tesseract-ocr)")
    argv = [exe, "stdin", "stdout", "--psm", str(int(psm))]
    if whitelist is not None:
        if not re.fullmatch(r"[0-9.,]+", whitelist):
            raise ValueError("whitelist may only contain digits, '.' and ','")
        argv += ["--oem", "1", "--dpi", "300", "-c", f"tessedit_char_whitelist={whitelist}"]
    # One OpenMP thread per process: axis rows are read by several processes in parallel.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    try:
        proc = subprocess.run([*argv, "tsv"], input=png, capture_output=True, timeout=OCR_TIMEOUT_S,
                              check=False, env=env)
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


def _png(gray) -> bytes:
    import cv2
    ok, png = cv2.imencode(".png", gray)
    if not ok:
        raise ValueError("could not encode OCR region")
    return png.tobytes()


def _ocr_ready(region):
    """Gray, upscaled, binarized, dark-on-light: the input Tesseract reads best."""
    import cv2
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if bw.mean() < 128:
        bw = 255 - bw
    return _png(bw)


# ----- price axis: find the label column, cut one image per label row, read each row as one number

AXIS_STRIP = 0.40        # the price axis is searched for in the right 40% of the image
MAX_AXIS_ROWS = 40       # bounded work per image
AXIS_TEXT_HEIGHT = 36    # label rows are rescaled so digits are about this tall (px) for Tesseract
OCR_WORKERS = min(8, os.cpu_count() or 1)  # parallel tesseract processes for one image
AXIS_DEADLINE_S = 10.0   # no new axis OCR call starts after this; each call also has OCR_TIMEOUT_S
MIN_AXIS_CONF = 60.0     # Tesseract word confidence below this is not used


def _chroma(img):
    """max(B, G, R) - min(B, G, R): near 0 for gray text, large for candles and colored boxes.

    Unlike HSV saturation it stays low for dark tinted backgrounds such as navy chart themes."""
    import numpy as np
    return (img.max(axis=2).astype(np.int16) - img.min(axis=2)).astype(np.uint8)


def _axis_rows(gray, chroma) -> tuple[list[dict[str, int]], bool]:
    """Boxes of the label rows in the right-hand price axis, and whether the background is dark.

    Text is neutral (low chroma) and contrasts with the background. Colored things (candles, moving
    averages, the current-price tag and its text) are masked out with a small margin, and long straight
    lines (grid, borders) are removed. The label column is the largest group of text rows that share a
    left or right edge near the right side of the image; rows are then rebuilt from that column alone.
    """
    import cv2
    import numpy as np
    h, w = gray.shape
    x_off = int(w * (1 - AXIS_STRIP))
    g = gray[:, x_off:].astype(np.int16)
    bg = int(np.median(g))
    dark = bg < 128
    diff = np.abs(g - bg).astype(np.uint8)
    colored = cv2.dilate((chroma[:, x_off:] > 60).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    neutral = (chroma[:, x_off:] < 45) & ~colored
    level = float(np.percentile(diff[neutral], 99.5)) if neutral.any() else 0.0
    mask = ((diff > max(40.0, 0.5 * level)) & neutral).astype(np.uint8) * 255
    for k in ((41, 1), (1, 41)):  # long horizontal / vertical runs are grid lines or borders, not text
        lines = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, k))
        mask = cv2.subtract(mask, lines)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = [tuple(int(v) for v in stats[i][:4]) for i in range(1, n) if stats[i][4] >= 2]
    words = _row_words(comps, h, rightmost=True)
    column = _label_column(words)
    if not column:
        return [], dark
    x0, x1 = column
    words = _row_words([c for c in comps if c[0] >= x0 and c[0] + c[2] <= x1], h, rightmost=False)
    if words:
        wide = [wd["y1"] - wd["y0"] for wd in words if wd["x1"] - wd["x0"] >= 1.2 * (wd["y1"] - wd["y0"])]
        hm = median(wide) if wide else 0
        words = [wd for wd in words if 0.6 * hm <= wd["y1"] - wd["y0"] <= 1.6 * hm
                 and not _on_colored_box(chroma[:, x_off:], wd)]
    for wd in words:
        wd["x0"] += x_off
        wd["x1"] += x_off
    return words[:MAX_AXIS_ROWS], dark


def _on_colored_box(chroma, box: dict[str, int]) -> bool:
    """Text on a colored box (the current-price tag, alerts) rather than on the axis background."""
    import numpy as np
    pad = max(2, (box["y1"] - box["y0"]) // 3)
    region = chroma[max(0, box["y0"] - pad): box["y1"] + pad, max(0, box["x0"] - pad): box["x1"] + pad]
    return bool(region.size) and float(np.median(region)) > 60


def _row_words(comps: list[tuple[int, int, int, int]], h: int, *, rightmost: bool) -> list[dict[str, int]]:
    """Group component boxes into text rows; per row its rightmost word, or all of its components."""
    import numpy as np
    if not comps:
        return []
    occupied = np.zeros(h + 1, bool)
    for _, cy, _, ch in comps:
        occupied[cy:cy + ch] = True
    edges = np.flatnonzero(np.diff(np.concatenate(([0], occupied.astype(np.int8)))))
    words = []
    for r0, r1 in zip(edges[::2], edges[1::2]):
        rh = int(r1 - r0)
        if rh < 4 or rh > h // 4:
            continue
        cs = sorted((c for c in comps if r0 <= c[1] and c[1] + c[3] <= r1), key=lambda c: c[0] + c[2])
        word = [cs[-1]]
        for c in reversed(cs[:-1]):  # walk left while the gaps stay small
            if not rightmost or min(x for x, *_ in word) - (c[0] + c[2]) <= max(3, int(0.9 * rh)):
                word.append(c)
            else:
                break
        words.append({"x0": min(c[0] for c in word), "x1": max(c[0] + c[2] for c in word),
                      "y0": min(c[1] for c in word), "y1": max(c[1] + c[3] for c in word)})
    return words


def _label_column(words: list[dict[str, int]]) -> tuple[int, int] | None:
    """(x0, x1) of the price-label column: most rows aligned on one edge (labels are left- or
    right-aligned), preferring the group furthest to the right."""
    # labels are at least a few characters wide; this drops specks and broken line remnants
    words = [wd for wd in words if wd["x1"] - wd["x0"] >= 1.2 * (wd["y1"] - wd["y0"])]
    if len(words) < 2:
        return None
    hm = median(wd["y1"] - wd["y0"] for wd in words)
    tol = max(3.0, 0.6 * hm)
    best: tuple[int, int, list] | None = None
    for wd in words:
        for edge in ("x0", "x1"):
            group = [o for o in words if abs(o[edge] - wd[edge]) <= tol]
            key = (len(group), max(o["x1"] for o in group))
            if best is None or key > best[:2]:
                best = (*key, group)
    assert best is not None
    group = best[2]
    if len(group) < 2:
        return None
    # a row whose word swallowed something beside the label must not widen the column much
    reach = 0.5 * median(o["x1"] - o["x0"] for o in group)
    x0 = max(min(o["x0"] for o in group), median(o["x0"] for o in group) - reach)
    x1 = min(max(o["x1"] for o in group), median(o["x1"] for o in group) + reach)
    return int(x0) - 2, int(x1) + 2


def _row_image(gray, box: dict[str, int], dark: bool, *, binarize: bool = True, target: int = AXIS_TEXT_HEIGHT):
    """One label row as dark text on white, contrast-stretched, cubic-upscaled, Otsu-binarized, padded."""
    import cv2
    import numpy as np
    h, w = gray.shape
    th = box["y1"] - box["y0"]
    pad = max(2, th // 3)
    crop = gray[max(0, box["y0"] - pad): min(h, box["y1"] + pad),
                max(0, box["x0"] - pad): min(w, box["x1"] + pad)].astype(np.float32)
    if dark:
        crop = 255 - crop
    lo, hi = np.percentile(crop, 1), np.percentile(crop, 99)
    crop = np.clip((crop - lo) * 255.0 / max(1.0, hi - lo), 0, 255).astype(np.uint8)
    f = min(8.0, max(1.0, target / max(1, th)))
    big = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    otsu, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # Dotted or slashed zeros (common in monospace fonts) carry a mark inside the counter that the
    # English model reads as 8, 6 or 9. Marks enclosed by a hole of another glyph are erased. (At small
    # sizes the mark merges with the outline and this cannot help; see docs/OCR_BENCHMARK.md.)
    contours, hier = cv2.findContours((bw == 0).astype(np.uint8), cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hier is not None:
        for i, (_, _, _, parent) in enumerate(hier[0]):
            if parent >= 0 and hier[0][parent][3] >= 0:  # outline -> hole -> enclosed mark
                cv2.drawContours(big, contours, i, 255, -1)
                cv2.drawContours(big, contours, i, 255, max(1, int(f / 2)))
        _, bw = cv2.threshold(big, otsu, 255, cv2.THRESH_BINARY)
    if binarize:
        big = bw
    m = AXIS_TEXT_HEIGHT // 2
    return cv2.copyMakeBorder(big, m, m, m, m, cv2.BORDER_CONSTANT, value=255)


def _read_rows(images: list, runner: OcrRunner, deadline: float) -> list[tuple[str, float]]:
    """OCR each row image as one text line (psm 7); returns (text, lowest word confidence or -1).

    No new OCR call starts after ``deadline`` (time.monotonic()); such rows come back unread."""
    from concurrent.futures import ThreadPoolExecutor

    def one(img) -> tuple[str, float]:
        if time.monotonic() > deadline:
            return "", -1.0
        words = runner(_png(img), 7, NUMERIC_WHITELIST)
        if not words:
            return "", -1.0
        return "".join(wd["text"] for wd in words), min(wd["conf"] for wd in words)

    if len(images) <= 1:
        return [one(i) for i in images]
    with ThreadPoolExecutor(max_workers=min(OCR_WORKERS, len(images))) as pool:
        return list(pool.map(one, images))


# Each row is read from these renderings: (Otsu-binarized?, digit height in px). They fail differently:
# binarizing helps clean text and hurts blurred thin strokes; the scale changes how glyphs merge.
ROW_VARIANTS = ((False, 28), (True, 36), (False, 40))
RETRY_VARIANTS = ((False, 52), (True, 26), (False, 22))  # only for rows that break the scale


def _separator_variants(text: str) -> list[str]:
    """The reading with each '.'/',' swapped: the two marks are a few pixels and easily confused."""
    marks = [i for i, ch in enumerate(text) if ch in ".,"]
    if not marks or len(marks) > 4:
        return []
    out = []
    for bits in range(1, 2 ** len(marks)):
        chars = list(text)
        for k, i in enumerate(marks):
            if bits >> k & 1:
                chars[i] = "," if chars[i] == "." else "."
        out.append("".join(chars))
    return out


def _vote(readings: list[tuple[str, float]]) -> tuple[str, float]:
    """Most frequent digit sequence among parseable readings (ties: higher confidence); its best reading."""
    groups: dict[str, list[tuple[float, str]]] = {}
    for t, c in readings:
        if parse_axis_number(t) is not None:
            groups.setdefault(re.sub(r"\D", "", t), []).append((c, t))
    if not groups:
        return readings[0]
    best = max(groups.values(), key=lambda g: (len(g), max(g)))
    conf, text = max(best)
    return text, conf


def read_price_axis(img, runner: OcrRunner = tesseract_runner) -> list[dict[str, Any]]:
    """Axis label rows as OCR words {"text", "conf", "left", "top", "height", "readings"} (image coordinates).

    Every row is read from several renderings and the most frequent digit sequence wins. Rows whose
    winner breaks the scale formed by the other labels are read again from more renderings and then
    re-decided among their own readings, those readings with '.'/',' swapped, and their digits with the
    axis's usual number of decimals: a candidate that lands exactly on the scale is taken and marked
    ``scale_fit``. Values are only ever chosen from what OCR read, never filled in from the scale.
    """
    import cv2
    deadline = time.monotonic() + AXIS_DEADLINE_S
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    boxes, dark = _axis_rows(gray, _chroma(img))
    if not boxes:
        return []
    nv = len(ROW_VARIANTS)
    reads = _read_rows([_row_image(gray, b, dark, binarize=bz, target=t) for b in boxes for bz, t in ROW_VARIANTS],
                       runner, deadline)
    words = []
    for i, b in enumerate(boxes):
        rr = reads[i * nv:(i + 1) * nv]
        text, conf = _vote(rr)
        words.append({"text": text, "conf": conf, "left": b["x0"], "top": b["y0"], "height": b["y1"] - b["y0"],
                      "readings": [t for t, _ in rr]})
    check = axis_consistency(words)
    if check["step"] is None:
        return words
    redo = [i for i in range(len(words)) if i not in check["consistent"]]
    nr = len(RETRY_VARIANTS)
    more = _read_rows([_row_image(gray, boxes[i], dark, binarize=bz, target=t)
                       for i in redo for bz, t in RETRY_VARIANTS], runner, deadline)
    shown = [_decimals(words[i]["text"]) for i in check["consistent"]]
    decimals = max(set(shown), key=shown.count) if shown else None
    for k, i in enumerate(redo):
        wd = words[i]
        readings = wd["readings"] + [t for t, _ in more[k * nr:(k + 1) * nr]]
        cands = [r for read in readings for r in (read, *_separator_variants(read))]
        if decimals is not None:  # the same digits with the axis's own decimal places (a lost or extra mark)
            cands += [_with_decimals(read, decimals) for read in readings]
        y = _center(wd)
        words[i] = {**wd, "readings": readings}
        for t in dict.fromkeys(c for c in cands if c):
            v = parse_axis_number(t)
            if v is not None and fits_grid(v, y, check):
                words[i] = {**wd, "text": t, "readings": readings, "scale_fit": True}
                break
    return words


def _decimals(text: str) -> int:
    """Digits after the decimal mark of a parseable axis reading."""
    t = text.strip()
    if re.fullmatch(r"\d{1,9},\d{1,8}", t) and not re.fullmatch(r"[1-9]\d{0,2}(,\d{3})+", t):
        return len(t.split(",")[1])
    return len(t.split(".")[1]) if "." in t else 0


def _with_decimals(text: str, decimals: int) -> str:
    digits = re.sub(r"\D", "", text)
    if not digits or len(digits) <= decimals:
        return ""
    return digits if decimals == 0 else f"{digits[:-decimals]}.{digits[-decimals:]}"


def _usable(word: dict[str, Any]) -> bool:
    return word.get("conf", 0) >= MIN_AXIS_CONF or bool(word.get("scale_fit"))


def parse_axis_number(text: str) -> float | None:
    """Axis label -> value. Accepts thousands separators ("65,312.50") and a decimal comma ("0,0534")."""
    t = text.strip().replace(" ", "")
    if re.fullmatch(r"[1-9]\d{0,2}(,\d{3})+(\.\d{1,8})?", t):
        return float(t.replace(",", ""))
    if re.fullmatch(r"\d{1,9}(\.\d{1,8})?", t):
        return float(t)
    if re.fullmatch(r"\d{1,9},\d{1,8}", t):  # not a thousands pattern (matched above): decimal comma
        return float(t.replace(",", "."))
    return None


def _center(word: dict[str, Any]) -> float:
    return word["top"] + word.get("height", 0) / 2


def fits_grid(value: float, y: float, check: dict[str, Any]) -> bool:
    """On the fitted line (within a third of a step) and a whole multiple of the step."""
    step = check["step"]
    k = value / step  # chart scales put labels on whole multiples of the step (1e-6: float rounding only)
    return abs(k - round(k)) < 1e-6 and abs(value - (check["intercept"] + check["slope"] * y)) < 0.35 * step


def axis_consistency(words: list[dict[str, Any]]) -> dict[str, Any]:
    """Which axis readings agree: a linear price scale has evenly spaced labels with a constant step.

    1. Robust line value = intercept + slope * y: every pair of readings proposes a line and the line
       with the most readings within a third of a label spacing wins (exhaustive RANSAC over at most
       MAX_AXIS_ROWS rows). Higher on screen must mean a higher price.
    2. The step is the value gap per label spacing between neighboring inliers that occurs most often
       (at least twice) and agrees with the line's slope.
    3. A reading is consistent if it is on the line and a whole multiple of the step.
    Returns {"step", "slope", "intercept", "consistent": indexes into words}. With fewer than
    three usable positioned readings, or no agreement, step is None and every usable reading counts.
    """
    pts, readable = [], set()
    for i, wd in enumerate(words):
        v = parse_axis_number(wd["text"]) if _usable(wd) else None
        if v is not None:
            readable.add(i)
            if "top" in wd:
                pts.append((_center(wd), v, i))
    none: dict[str, Any] = {"step": None, "consistent": readable}
    centers = sorted(_center(wd) for wd in words if "top" in wd)
    gaps = [y2 - y1 for y1, y2 in zip(centers, centers[1:]) if y2 - y1 > 1]
    if len(pts) < 3 or not gaps:
        return none
    unit = median(gaps)  # label spacing in pixels, from all rows (readable or not)
    pts.sort()
    best: tuple[int, float, list] | None = None
    for k, (y1, v1, _) in enumerate(pts):
        for y2, v2, _ in pts[k + 1:]:
            if y2 - y1 < 0.5 * unit or v2 >= v1:
                continue
            slope = (v2 - v1) / (y2 - y1)
            tol = 0.35 * abs(slope) * unit
            res = [(abs(p[1] - (v1 + slope * (p[0] - y1))), p) for p in pts]
            inl = [p for r, p in res if r < tol]
            key = (len(inl), -sum(r for r, _ in res if r < tol) / tol)
            if best is None or key > best[:2]:
                best = (*key, inl)
    if best is None or best[0] < 3:
        return none
    inl = best[2]
    b = median((q[1] - p[1]) / (q[0] - p[0]) for k, p in enumerate(inl) for q in inl[k + 1:] if q[0] - p[0] > 1)
    if b >= 0:
        return none
    a = median(v - b * y for y, v, _ in inl)
    steps = []
    for (y1, v1, _), (y2, v2, _) in zip(inl, inl[1:]):
        n = round((y2 - y1) / unit)
        if n >= 1 and abs((y2 - y1) / unit - n) < 0.25:
            steps.append(round((v1 - v2) / n, 10))
    if not steps:
        return none
    step = max(sorted(set(steps)), key=steps.count)
    if steps.count(step) < 2 or step <= 0 or abs(step - abs(b) * unit) > 0.15 * step:
        return none  # neighbors do not agree on a step: too few or too noisy readings to judge
    check = {"step": step, "slope": b, "intercept": a}
    check["consistent"] = {i for y, v, i in pts if fits_grid(v, y, check)}
    return check


SYMBOL_RE = re.compile(r"\b([A-Z0-9]{2,15})[/\-]?(USDT|USDC|FDUSD|BUSD|USD|BTC|ETH)\b")
TF_RE = re.compile(r"^(1|3|5|15|30)m$|^(1|2|4|6|8|12)[hH]$|^(1|3)[dD]$|^1[wW]$|^1M$")
MA_RE = re.compile(r"\b(MA|SMA|EMA)\s*\(?\s*(\d{1,3})\s*\)?", re.I)


def parse_chart_text(title_words: list[dict[str, Any]], axis_words: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join(w["text"] for w in title_words)
    symbols = sorted({a + b for a, b in SYMBOL_RE.findall(text.upper())})
    tfs = []
    for w in title_words:
        t = w["text"].strip("()[],")
        if TF_RE.match(t):
            code = "1M" if t == "1M" else t.lower()  # 1M is a month, 1m a minute
            if code in Timeframe.all_codes():
                tfs.append(code)
    mas = sorted({f"{k.upper()}{n}" for k, n in MA_RE.findall(text)})
    check = axis_consistency(axis_words)
    prices, outliers = [], []
    for i, w in enumerate(axis_words):
        v = parse_axis_number(w["text"])
        if v is not None and _usable(w):
            (prices if i in check["consistent"] else outliers).append(v)
    market = "usdm_perpetual" if re.search(r"\bPERP(ETUAL)?\b", text.upper()) else None
    return {"symbols": symbols, "timeframes": sorted(set(tfs)), "indicator_labels": mas,
            "price_axis_labels": sorted(prices), "price_axis_outliers": sorted(outliers),
            "price_axis_step": check["step"], "market_hint": market}


def run_ocr(img, runner: OcrRunner = tesseract_runner) -> dict[str, Any]:
    h = img.shape[0]
    title = img[: max(1, h // 8), :]
    try:
        tw = runner(_ocr_ready(title), 6)
        aw = read_price_axis(img, runner)
    except OcrUnavailable as exc:
        return {"status": "OCR_UNAVAILABLE", "reason": str(exc)}
    words = tw + [x for x in aw if x["conf"] >= 0]
    return {"status": "OK", "engine": "tesseract", "words": len(words),
            "mean_confidence": round(sum(x["conf"] for x in words) / len(words), 1) if words else None,
            "confidence_note": "Tesseract word confidence (0-100); not calibrated, not a probability.",
            "axis_note": "Axis readings that break the evenly spaced, evenly stepped scale are listed as "
                         "price_axis_outliers and not used; text on colored boxes (price tags) is ignored.",
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
        image_dimensions(data)  # still reject files that only mimic an image signature
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
