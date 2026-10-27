#!/usr/bin/env python3
"""Deterministic, labeled benchmark for chart price-axis OCR (``chartvision.run_ocr``).

Every image is generated on the fly from a seed: nothing is downloaded and nothing is written to disk
unless ``--dump DIR`` is passed. Ground truth is the exact label text drawn on the price axis and its
numeric value. Only stdlib + numpy + OpenCV are used. Real TrueType fonts installed on this computer
(DejaVu, Liberation, Noto) are drawn with the small glyph-outline rasterizer below, because OpenCV's
``putText`` only has the Hershey stroke fonts; fonts that are not installed are skipped.

Usage (from the repository root):
    PYTHONPATH=src python tools/ocr_benchmark.py --split heldout
    PYTHONPATH=src python tools/ocr_benchmark.py --split dev --limit 50 --workers 8
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import struct
import sys
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

# Seed ranges. Tune only on "dev"; the reported number comes from "heldout".
# "mono_stress" uses only monospace fonts with dotted/slashed zeros, reported separately (see the doc).
SPLITS = {"dev": range(0, 300), "heldout": range(100_000, 100_400), "mono_stress": range(200_000, 200_100)}
MONO_FONTS = ("DejaVuSansMono", "LiberationMono-Regular")

TTF_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)
HERSHEY = {
    "hershey_simplex": cv2.FONT_HERSHEY_SIMPLEX,
    "hershey_duplex": cv2.FONT_HERSHEY_DUPLEX,
    "hershey_complex": cv2.FONT_HERSHEY_COMPLEX,
    "hershey_triplex": cv2.FONT_HERSHEY_TRIPLEX,
    "hershey_plain": cv2.FONT_HERSHEY_PLAIN,
    "hershey_complex_small": cv2.FONT_HERSHEY_COMPLEX_SMALL,
}
CHARSET = "0123456789.,-: ABCDHLMOPSTUhm"
# BGR palettes modeled on public screenshots of the two chart styles (approximate, not official values).
THEMES = {
    "tv_dark": dict(bg=(34, 23, 19), text=(190, 181, 178), grid=(57, 46, 42), border=(57, 46, 42),
                    up=(129, 153, 8), down=(69, 54, 242)),
    "tv_light": dict(bg=(255, 255, 255), text=(34, 23, 19), grid=(250, 243, 240), border=(224, 220, 217),
                     up=(129, 153, 8), down=(69, 54, 242)),
    "binance_dark": dict(bg=(30, 26, 22), text=(156, 142, 132), grid=(49, 43, 37), border=(57, 49, 43),
                         up=(129, 203, 14), down=(93, 70, 246)),
    "binance_light": dict(bg=(255, 255, 255), text=(138, 122, 112), grid=(239, 236, 234), border=(230, 228, 226),
                          up=(129, 203, 14), down=(93, 70, 246)),
}
# (name, low, high, allowed decimals). Prices are drawn log-uniformly inside [low, high).
MAGNITUDES = (
    ("1e-4", 0.00005, 0.0009, (6, 7, 8)),
    ("0.x", 0.05, 0.95, (3, 4, 5)),
    ("1-99", 1.0, 99.0, (1, 2, 3, 4)),
    ("1k", 1_000.0, 9_999.0, (0, 1, 2)),
    ("10k", 10_000.0, 99_999.0, (0, 1, 2)),
    ("1M", 1_000_000.0, 3_000_000.0, (0,)),
)


# ---------- minimal TrueType rasterizer (glyf outlines, cmap format 4) ----------

class TrueTypeFont:
    """Just enough of the TrueType format to draw digits and punctuation without new dependencies."""

    def __init__(self, path: str):
        self.name = Path(path).stem
        data = Path(path).read_bytes()
        if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
            raise ValueError("not a glyf-based TrueType font")
        num = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(num):
            tag, _, off, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            tables[tag.decode("latin-1")] = (off, length)
        self.data, self.tables = data, tables
        head = tables["head"][0]
        self.upem = struct.unpack(">H", data[head + 18: head + 20])[0]
        loca_long = struct.unpack(">h", data[head + 50: head + 52])[0] == 1
        n_glyphs = struct.unpack(">H", data[tables["maxp"][0] + 4: tables["maxp"][0] + 6])[0]
        lo = tables["loca"][0]
        if loca_long:
            self.loca = struct.unpack(f">{n_glyphs + 1}I", data[lo: lo + 4 * (n_glyphs + 1)])
        else:
            self.loca = [2 * v for v in struct.unpack(f">{n_glyphs + 1}H", data[lo: lo + 2 * (n_glyphs + 1)])]
        hh = tables["hhea"][0]
        n_hm = struct.unpack(">H", data[hh + 34: hh + 36])[0]
        hm = tables["hmtx"][0]
        self.advances = [struct.unpack(">H", data[hm + 4 * i: hm + 4 * i + 2])[0] for i in range(n_hm)]
        self.cmap = self._cmap()

    def _cmap(self) -> dict[int, int]:
        d = self.data
        base = self.tables["cmap"][0]
        n = struct.unpack(">H", d[base + 2: base + 4])[0]
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", d[base + 4 + 8 * i: base + 12 + 8 * i])
            sub = base + off
            if (pid, eid) in ((3, 1), (0, 3)) and struct.unpack(">H", d[sub: sub + 2])[0] == 4:
                seg = struct.unpack(">H", d[sub + 6: sub + 8])[0] // 2
                ends = struct.unpack(f">{seg}H", d[sub + 14: sub + 14 + 2 * seg])
                p = sub + 16 + 2 * seg
                starts = struct.unpack(f">{seg}H", d[p: p + 2 * seg])
                deltas = struct.unpack(f">{seg}h", d[p + 2 * seg: p + 4 * seg])
                ro_pos = p + 4 * seg
                ranges = struct.unpack(f">{seg}H", d[ro_pos: ro_pos + 2 * seg])
                out = {}
                for ch in CHARSET:
                    c = ord(ch)
                    for k in range(seg):
                        if starts[k] <= c <= ends[k]:
                            if ranges[k] == 0:
                                g = (c + deltas[k]) & 0xFFFF
                            else:
                                a = ro_pos + 2 * k + ranges[k] + 2 * (c - starts[k])
                                g = struct.unpack(">H", d[a: a + 2])[0]
                                g = (g + deltas[k]) & 0xFFFF if g else 0
                            out[c] = g
                            break
                return out
        raise ValueError("no usable cmap")

    def advance(self, ch: str) -> int:
        g = self.cmap[ord(ch)]
        return self.advances[min(g, len(self.advances) - 1)]

    @lru_cache(maxsize=64)
    def contours(self, ch: str) -> tuple:
        """Glyph outline as polylines in font units (y up)."""
        d = self.data
        g = self.cmap[ord(ch)]
        start = self.tables["glyf"][0] + self.loca[g]
        if self.loca[g] == self.loca[g + 1]:
            return ()
        n_contours = struct.unpack(">h", d[start: start + 2])[0]
        if n_contours < 0:
            raise ValueError("compound glyphs are not supported")
        p = start + 10
        end_pts = struct.unpack(f">{n_contours}H", d[p: p + 2 * n_contours])
        p += 2 * n_contours
        p += 2 + struct.unpack(">H", d[p: p + 2])[0]
        n_pts = end_pts[-1] + 1
        flags = []
        while len(flags) < n_pts:
            f = d[p]
            p += 1
            flags.append(f)
            if f & 8:
                flags.extend([f] * d[p])
                p += 1
        coords = []
        for short_bit, same_bit in ((2, 16), (4, 32)):
            v, vals = 0, []
            for f in flags[:n_pts]:
                if f & short_bit:
                    delta = d[p]
                    p += 1
                    v += delta if f & same_bit else -delta
                elif not f & same_bit:
                    v += struct.unpack(">h", d[p: p + 2])[0]
                    p += 2
                vals.append(v)
            coords.append(vals)
        xs, ys = coords
        out, first = [], 0
        for last in end_pts:
            pts = [(xs[i], ys[i], bool(flags[i] & 1)) for i in range(first, last + 1)]
            first = last + 1
            out.append(tuple(_flatten(pts)))
        return tuple(out)


def _flatten(pts, steps: int = 8):
    """Quadratic B-spline contour (on/off-curve points) -> polyline."""
    if not any(on for _, _, on in pts):
        x0, y0, _ = pts[0]
        x1, y1, _ = pts[1]
        pts = [((x0 + x1) / 2, (y0 + y1) / 2, True)] + pts
    k = next(i for i, (_, _, on) in enumerate(pts) if on)
    pts = pts[k:] + pts[:k]
    expanded = []
    for i, (x, y, on) in enumerate(pts):
        nx, ny, non = pts[(i + 1) % len(pts)]
        expanded.append((x, y, on))
        if not on and not non:
            expanded.append(((x + nx) / 2, (y + ny) / 2, True))
    poly = [(expanded[0][0], expanded[0][1])]
    i, n = 0, len(expanded)
    while i < n:
        x0, y0, _ = expanded[i]
        x1, y1, on1 = expanded[(i + 1) % n]
        if on1:
            poly.append((x1, y1))
            i += 1
        else:
            x2, y2, _ = expanded[(i + 2) % n]
            for s in range(1, steps + 1):
                t = s / steps
                poly.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * x1 + t * t * x2,
                             (1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t * t * y2))
            i += 2
    return poly


@lru_cache(maxsize=1)
def available_ttf() -> tuple[TrueTypeFont, ...]:
    fonts = []
    for path in TTF_CANDIDATES:
        try:
            f = TrueTypeFont(path)
            for ch in CHARSET:
                f.contours(ch)
            fonts.append(f)
        except (OSError, ValueError, KeyError, struct.error):
            continue
    return tuple(fonts)


def font_names(seed: int | None = None) -> list[str]:
    """Fonts a seed draws from: monospace dotted-zero faces only in the mono_stress split."""
    ttf = [f.name for f in available_ttf()]
    if seed is None:
        return list(HERSHEY) + ttf
    if seed in SPLITS["mono_stress"]:
        return [n for n in ttf if n in MONO_FONTS]
    return list(HERSHEY) + [n for n in ttf if n not in MONO_FONTS]


# ---------- text drawing (both font kinds) ----------

@lru_cache(maxsize=None)
def _hershey_digit_height(face: int) -> int:
    im = np.zeros((80, 300), np.uint8)
    cv2.putText(im, "0123456789", (5, 60), face, 1.0, 255, 1)
    ys = np.flatnonzero(im.max(axis=1))
    return int(ys.max() - ys.min() + 1)


class Pen:
    """Measures and draws one string in one font at one size; positions are top-left of the text box.

    ``scale`` is a Hershey Simplex font scale. Every face is sized to the digit height Simplex has at that
    scale (22 px x scale: 7.7-15.4 px at 1x), so small faces such as Hershey Plain are not unreadably tiny.
    """

    def __init__(self, font: str, scale: float, hidpi: int, aa: bool):
        self.font, self.scale, self.hidpi, self.aa = font, scale, hidpi, aa
        if font in HERSHEY:
            self.face = HERSHEY[font]
            self.thick = hidpi
            self.scale = scale * 22 / _hershey_digit_height(self.face)
        else:
            self.ttf = next(f for f in available_ttf() if f.name == font)
            self.px = scale * 30 * hidpi  # em size: digits are ~0.73 em tall, matching Simplex

    def size(self, text: str) -> tuple[int, int]:
        if self.font in HERSHEY:
            (w, h), _ = cv2.getTextSize(text, self.face, self.scale * self.hidpi, self.thick)
            return w, h
        w = sum(self.ttf.advance(c) for c in text) * self.px / self.ttf.upem
        return int(math.ceil(w)), int(round(0.73 * self.px))

    def draw(self, img, text: str, x: float, y: float, color) -> tuple[int, int, int, int]:
        w, h = self.size(text)
        if self.font in HERSHEY:
            cv2.putText(img, text, (int(round(x)), int(round(y + h))), self.face, self.scale * self.hidpi,
                        color, self.thick, cv2.LINE_AA if self.aa else cv2.LINE_8)
            return int(x), int(y), w, h
        ss = 4  # supersampling factor; downsampling with INTER_AREA gives anti-aliased coverage
        pad = 2
        cw, ch = w + 2 * pad, int(math.ceil(self.px * 1.1)) + 2 * pad
        x0, y0 = int(math.floor(x)) - pad, int(math.floor(y)) - pad
        fx, fy = x - math.floor(x) + pad, y - math.floor(y) + pad  # sub-pixel placement
        mask = np.zeros((ch * ss, cw * ss), np.uint8)
        k = self.px / self.ttf.upem
        pen, polys = fx, []
        for c in text:
            for contour in self.ttf.contours(c):
                pts = [((pen + px_ * k) * ss, (fy + h - py * k) * ss) for px_, py in contour]
                polys.append(np.array(pts, np.float64).round().astype(np.int32))
            pen += self.ttf.advance(c) * k
        cv2.fillPoly(mask, polys, 255)
        alpha = cv2.resize(mask, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        if not self.aa:
            alpha = (alpha >= 0.5).astype(np.float32)
        H, W = img.shape[:2]
        ax0, ay0, ax1, ay1 = max(0, x0), max(0, y0), min(W, x0 + cw), min(H, y0 + ch)
        if ax1 <= ax0 or ay1 <= ay0:
            return int(x), int(y), w, h
        a = alpha[ay0 - y0: ay1 - y0, ax0 - x0: ax1 - x0, None]
        roi = img[ay0:ay1, ax0:ax1].astype(np.float32)
        img[ay0:ay1, ax0:ax1] = (roi * (1 - a) + np.array(color, np.float32) * a).round().astype(np.uint8)
        return int(x), int(y), w, h


# ---------- chart generator ----------

def _fmt(v: Decimal, decimals: int, sep: bool) -> str:
    q = v.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{q:,.{decimals}f}" if sep else f"{q:.{decimals}f}"


def _nice_step(raw: float) -> Decimal:
    e = math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * 10 ** e >= raw:
            return Decimal(str(m)).scaleb(e) if m != 10 else Decimal(1).scaleb(e + 1)
    return Decimal(1).scaleb(e + 1)


def generate(seed: int) -> dict:
    """One labeled chart image. Same seed -> same pixels and same ground truth."""
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    theme_name = rng.choice(sorted(THEMES))
    t = THEMES[theme_name]
    hidpi = rng.choice((1, 1, 2))
    font = rng.choice(font_names(seed))
    scale = round(rng.uniform(0.35, 0.7), 3)
    aa = rng.random() < 0.8
    pen = Pen(font, scale, hidpi, aa)

    mag, lo, hi, decs = rng.choice(MAGNITUDES)
    decimals = rng.choice(decs)
    sep = mag in ("1k", "10k", "1M") and rng.random() < (0.7 if mag == "1M" else 0.5)
    price = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    n_labels = rng.randint(5, 12)

    W = rng.randint(640, 1500) * hidpi
    H = rng.randint(380, 900) * hidpi
    _, th = pen.size("0")
    min_gap = int(th * 2.4)
    top, bottom = rng.randint(30, 60) * hidpi, rng.randint(28, 40) * hidpi  # time axis at the bottom
    span_px = H - top - bottom
    n_labels = max(5, min(n_labels, span_px // min_gap + 1))

    rel = rng.uniform(0.004, 0.25)
    step = _nice_step(price * rel / n_labels)
    unit = Decimal(1).scaleb(-decimals)
    step = max(step, unit)
    while step % unit:  # the step must be a multiple of the last shown digit (e.g. 0.25 -> 0.5 at 1 decimal)
        step *= 2
    step_f = float(step)
    first = Decimal(math.floor(price / step_f)) * step
    values = [first + step * i for i in range(n_labels)]
    while values[0] <= 0:
        values = [v + step for v in values]

    texts = [_fmt(v, decimals, sep) for v in values]
    widest = max(pen.size(s)[0] for s in texts + [_fmt(values[-1] + step * 3, decimals, sep)])
    axis_w = widest + rng.randint(12, 28) * hidpi
    axis_x = W - axis_w
    img = np.full((H, W, 3), t["bg"], np.uint8)

    # value -> y: labels evenly spaced, the visible range extends a fraction of a step beyond them
    spacing = span_px / (n_labels - 1 + rng.uniform(0.2, 1.4))
    y_first = H - bottom - rng.uniform(0.1, 0.6) * spacing
    def y_of(v: float) -> float:
        return y_first - (v - float(values[0])) / step_f * spacing

    # gridlines, separator, ticks
    for v in values:
        y = int(round(y_of(float(v))))
        cv2.line(img, (0, y), (axis_x - 1, y), t["grid"], hidpi)
    for gx in range(rng.randint(40, 160) * hidpi, axis_x, rng.randint(90, 200) * hidpi):
        cv2.line(img, (gx, 0), (gx, H - bottom), t["grid"], hidpi)
    if rng.random() < 0.7:
        cv2.line(img, (axis_x, 0), (axis_x, H), t["border"], hidpi)
    cv2.line(img, (0, H - bottom), (W, H - bottom), t["border"], hidpi)
    ticks = rng.random() < 0.4

    # candles (random walk inside the visible range), one moving average
    v_lo = float(values[0]) - 0.3 * step_f
    v_hi = float(values[-1]) + 0.3 * step_f
    cw = rng.choice((3, 5, 7, 9)) * hidpi
    gap = max(1, cw // 3)
    right_margin = rng.choice((0, 0, 1, 3, 8)) * (cw + gap)
    n_c = max(10, (axis_x - 10 - right_margin) // (cw + gap))
    walk = np.cumsum(nrng.normal(0, 0.08, n_c + 1)) * (v_hi - v_lo) * 0.25
    walk = (walk - walk.min()) / max(1e-12, np.ptp(walk)) * (v_hi - v_lo) * 0.8 + v_lo + 0.1 * (v_hi - v_lo)
    closes, mids = [], []
    for i in range(n_c):
        x = axis_x - right_margin - (n_c - i) * (cw + gap)
        if x < 0:
            continue
        o, c = walk[i], walk[i + 1]
        wick = abs(c - o) + abs(nrng.normal(0, 0.03)) * (v_hi - v_lo)
        hi_, lo_ = max(o, c) + wick * 0.5, min(o, c) - wick * 0.5
        col = t["up"] if c >= o else t["down"]
        xc = x + cw // 2
        cv2.line(img, (xc, int(y_of(hi_))), (xc, int(y_of(lo_))), col, max(1, hidpi))
        y0, y1 = sorted((int(y_of(o)), int(y_of(c))))
        cv2.rectangle(img, (x, y0), (x + cw - 1, max(y0 + 1, y1)), col, -1)
        closes.append(c)
        mids.append((xc, y_of(c)))
    if len(mids) > 20:
        ma = np.convolve([m[1] for m in mids], np.ones(7) / 7, mode="valid")
        pts = np.array([[mids[i + 3][0], int(ma[i])] for i in range(len(ma))], np.int32)
        cv2.polylines(img, [pts], False, rng.choice(((0, 200, 255), (255, 120, 40), (200, 80, 200))), hidpi,
                      cv2.LINE_AA)

    # title / OHLC text and time-axis labels (distractors outside the price axis)
    tpen = Pen(font, min(0.7, scale * 1.15), hidpi, aa)
    tpen.draw(img, f"BTCUSDT 1h O{texts[0]} H{texts[-1]}", 10 * hidpi, 8 * hidpi, t["text"])
    for k, tx in enumerate(range(80 * hidpi, axis_x - 60 * hidpi, 160 * hidpi)):
        pen.draw(img, f"{(k * 4) % 24:02d}:00", tx, H - bottom + 8 * hidpi, t["text"])

    # current-price tag: a colored box with the last price; labels it covers are hidden (as on real charts)
    last = Decimal(str(closes[-1] if closes else float(values[len(values) // 2])))
    tag_text = _fmt(last, decimals, sep)
    tag_y = y_of(float(Decimal(tag_text.replace(",", ""))))
    tag_h = th + rng.randint(6, 10) * hidpi
    countdown = rng.random() < 0.3
    if countdown:
        tag_h += th + 4 * hidpi
    tag_box = (axis_x + 1, int(tag_y - (th + 6 * hidpi) / 2), W - 1, int(tag_y - (th + 6 * hidpi) / 2) + tag_h)
    tag_color = t["up"] if len(closes) < 2 or closes[-1] >= closes[-2] else t["down"]

    tag_visible = 0 < tag_box[1] and tag_box[3] < H - bottom
    align_right = rng.random() < 0.5
    labels = []
    for v, s in zip(values, texts):
        yc = y_of(float(v))
        lw, lh = pen.size(s)
        ytop = yc - lh / 2
        if not (2 <= ytop and ytop + lh <= H - bottom - 2):
            continue
        if tag_visible and ytop + lh + 2 * hidpi >= tag_box[1] and ytop - 2 * hidpi <= tag_box[3]:
            continue  # hidden behind the price tag
        x = (W - lw - rng.randint(4, 8) * hidpi) if align_right else axis_x + rng.randint(5, 9) * hidpi
        pen.draw(img, s, x + rng.uniform(0, 0.99), ytop + rng.uniform(0, 0.99), t["text"])
        if ticks:
            cv2.line(img, (axis_x, int(round(yc))), (axis_x + 3 * hidpi, int(round(yc))), t["border"], hidpi)
        labels.append({"text": s, "value": str(v.quantize(Decimal(1).scaleb(-decimals))), "y": round(yc, 1)})

    if tag_visible:
        cv2.rectangle(img, tag_box[:2], tag_box[2:], tag_color, -1)
        for x in range(0, axis_x, 8 * hidpi):
            cv2.line(img, (x, int(tag_y)), (x + 4 * hidpi, int(tag_y)), tag_color, hidpi)
        pen.draw(img, tag_text, axis_x + 6 * hidpi, tag_box[1] + 3 * hidpi, (255, 255, 255))
        if countdown:
            pen.draw(img, f"{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}", axis_x + 6 * hidpi,
                     tag_box[1] + th + 7 * hidpi, (255, 255, 255))
    else:
        tag_text = None

    # degradations
    blur = rng.random() < 0.3
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.3, 0.7))
    jpeg_q = rng.randint(60, 95) if rng.random() < 0.6 else None
    noise = jpeg_q is not None and rng.random() < 0.5  # noisy lossless PNGs would exceed the 5 MB upload limit
    if noise:
        img = np.clip(img.astype(np.float32) + nrng.normal(0, rng.uniform(1, 5), img.shape), 0, 255).astype(np.uint8)
    if jpeg_q:
        data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])[1].tobytes()
    else:
        data = cv2.imencode(".png", img)[1].tobytes()
    cond = {"theme": theme_name, "font": font, "font_kind": "hershey" if font in HERSHEY else "ttf",
            "scale": scale, "scale_bin": "0.35-0.47" if scale < 0.47 else "0.47-0.58" if scale < 0.58 else "0.58-0.70",
            "hidpi": f"{hidpi}x", "aa": aa, "magnitude": mag, "decimals": decimals, "thousands_sep": sep,
            "jpeg": f"q{(jpeg_q // 10) * 10}s" if jpeg_q else "png", "noise": noise, "blur": blur,
            "ticks": ticks, "align": "right" if align_right else "left"}
    return {"seed": seed, "image": data, "labels": labels, "tag": tag_text, "conditions": cond,
            "size": [W, H]}


# ---------- scoring ----------

def score_image(sample: dict, predicted: list[float]) -> dict:
    """One-to-one numeric matching of predicted axis values against the ground-truth labels."""
    truth = [float(Decimal(lab["value"])) for lab in sample["labels"]]
    remaining = list(predicted)
    matched, missed = 0, []
    for v in truth:
        hit = next((i for i, p in enumerate(remaining) if math.isclose(p, v, rel_tol=1e-9, abs_tol=1e-12)), None)
        if hit is None:
            missed.append(v)
        else:
            matched += 1
            remaining.pop(hit)
    return {"truth": len(truth), "pred": len(predicted), "matched": matched, "missed": missed, "extra": remaining}


def evaluate(seeds, workers: int = 1, runner=None) -> dict:
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        os.environ["OMP_THREAD_LIMIT"] = "1"  # parallel tesseract processes would oversubscribe the CPU
        with ProcessPoolExecutor(workers) as ex:
            rows = list(ex.map(_evaluate_one, seeds, chunksize=4))
    else:
        rows = [_evaluate_one(s, runner) for s in seeds]
    return summarize(rows)


def _evaluate_one(seed: int, runner=None) -> dict:
    from my_ai_trading import chartvision
    sample = generate(seed)
    img = chartvision.decode(sample["image"])
    t0 = time.perf_counter()
    out = chartvision.run_ocr(img, runner) if runner else chartvision.run_ocr(img)
    dt = time.perf_counter() - t0
    if out.get("status") != "OK":
        raise RuntimeError(f"OCR unavailable: {out.get('reason')}")
    pred = list(out["parsed"]["price_axis_labels"])
    s = score_image(sample, pred)
    s.update(seed=seed, seconds=dt, conditions=sample["conditions"], tag=sample["tag"],
             truth_text=[lab["text"] for lab in sample["labels"]])
    return s


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def summarize(rows: list[dict]) -> dict:
    tot = {k: sum(r[k] for r in rows) for k in ("truth", "pred", "matched")}
    lat = sorted(r["seconds"] for r in rows)
    by = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    for r in rows:
        for k, v in r["conditions"].items():
            if k == "scale":
                continue
            cell = by[k][str(v)]
            cell[0] += r["truth"]
            cell[1] += r["matched"]
            cell[2] += r["pred"]
            cell[3] += 1
    breakdown = {k: {v: {"images": c[3], "labels": c[0], "recall": _ratio(c[1], c[0]),
                         "precision": _ratio(c[1], c[2])} for v, c in sorted(vals.items())}
                 for k, vals in sorted(by.items())}
    failures = [{"seed": r["seed"], "missed": r["missed"], "extra": r["extra"], "truth": r["truth_text"],
                 "font": r["conditions"]["font"], "theme": r["conditions"]["theme"]}
                for r in rows if r["missed"] or r["extra"]]
    return {
        "images": len(rows), "labels": tot["truth"], "predicted": tot["pred"], "matched": tot["matched"],
        "accuracy_exact": _ratio(tot["matched"], tot["truth"]),  # = label recall with exact numeric match
        "precision": _ratio(tot["matched"], tot["pred"]),
        "images_all_correct": sum(1 for r in rows if not r["missed"] and not r["extra"]),
        "latency_s": {"median": round(lat[len(lat) // 2], 3), "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 3),
                      "max": round(lat[-1], 3)} if lat else None,
        "breakdown": breakdown, "failures": failures,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--split", choices=sorted(SPLITS), default="heldout")
    ap.add_argument("--limit", type=int, default=None, help="first N seeds of the split")
    ap.add_argument("--workers", type=int, default=1, help=">1 runs in parallel; latency is then contended")
    ap.add_argument("--json", type=Path, help="write the full result (no images) to this file")
    ap.add_argument("--dump", type=Path, help="write the generated images to this directory for inspection")
    ap.add_argument("--failures", type=int, default=15, help="failure examples to print")
    args = ap.parse_args(argv)
    seeds = list(SPLITS[args.split])[: args.limit]
    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)
        for s in seeds:
            smp = generate(s)
            ext = "jpg" if smp["conditions"]["jpeg"] != "png" else "png"
            (args.dump / f"{s}.{ext}").write_bytes(smp["image"])
            (args.dump / f"{s}.json").write_text(json.dumps({k: v for k, v in smp.items() if k != "image"}, indent=1))
        print(f"wrote {len(seeds)} images to {args.dump}")
        return 0
    res = evaluate(seeds, args.workers)
    print(f"split={args.split} images={res['images']} labels={res['labels']} fonts={font_names(seeds[0])}")
    print(f"accuracy (exact numeric, label recall) = {res['accuracy_exact']}  precision = {res['precision']}  "
          f"images fully correct = {res['images_all_correct']}/{res['images']}")
    print(f"latency per image (s): {res['latency_s']}  workers={args.workers}")
    for k, vals in res["breakdown"].items():
        print(f"  {k}: " + ", ".join(f"{v}={c['recall']}/{c['precision']} (n={c['labels']})" for v, c in vals.items()))
    for f in res["failures"][: args.failures]:
        print("  FAIL", json.dumps(f))
    if args.json:
        args.json.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
