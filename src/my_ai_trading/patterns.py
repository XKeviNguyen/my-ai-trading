"""Deterministic chart-pattern detectors over CLOSED candles.

Every detector applies explicit geometric criteria and reports the numbers it checked. A detection
is not proof that a pattern exists and is not a trade signal. "VCP" here is a generic geometric
approximation of a volatility contraction pattern; it is not a Minervini VCP or SEPA evaluation.

Conventions:
- Input is ``series.contiguous_tail()``: detectors never span a data gap. Bars are addressed by
  position, and times are reported as each bar's own ``open_time_ms`` (no fixed bar duration is
  assumed, so calendar-month bars work too).
- Swing points: a swing high at bar i has a high strictly above the ``left`` bars before it and at
  least as high as the ``right`` bars after it (lows mirrored). The tie rule keeps one swing per
  plateau. A swing at bar i is only CONFIRMED once bar i + right has closed, so the newest
  ``right`` bars can never hold a confirmed swing (no look-ahead).
- A pattern's structure is confirmed at the confirmation bar of its last swing. Its state is then
  resolved from later CLOSES only, never from intrabar highs/lows: a close beyond the breakout
  level gives BREAKOUT_CONFIRMED / BREAKDOWN_CONFIRMED, a close beyond the invalidation level gives
  FAILED, and no resolution within the pending window gives FAILED with resolution
  ``EXPIRED_NO_CLOSE_BEYOND_LEVELS``. Otherwise the state is FORMING.
- ``end_bar_open_ms`` is the bar at which the reported state became known: the resolution bar
  (never earlier than the structure confirmation bar), or the newest bar for FORMING. Hence any
  detection whose end bar is k depends only on bars up to k; FORMING detections are "as of" the
  newest bar and can change when more bars arrive.
- Supply/demand zones use FORMING for an active zone (no close beyond its far side yet) and FAILED
  once a close breaks through it; whether price has returned into the zone is reported separately.
- Several candidates of the same pattern that resolve on the same bar with the same state are
  deduplicated, keeping the one that starts earliest.

Parameter provenance: common technical-analysis conventions; the numeric thresholds below were
chosen here, not taken from a single authoritative source. They are module constants so callers
can see (and tests can pin) exactly what was applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .domain import Candle, CandleSeries

NOTE = "Deterministic geometric criteria; not proof of a pattern and not a trade signal."

# Swing points
SWING_LEFT = 3
SWING_RIGHT = 3
# Resolution window after structure confirmation: max(PENDING_MIN_BARS, pattern width in bars).
PENDING_MIN_BARS = 5
ATR_PERIOD = 14

# VCP (generic volatility contraction approximation)
VCP_MIN_CONTRACTIONS = 2
VCP_MAX_CONTRACTIONS = 4
VCP_MIN_FIRST_DEPTH = 0.05          # first pullback at least 5% of its high
VCP_MAX_FIRST_DEPTH = 0.50
VCP_MAX_DEPTH_RATIO = 0.90          # each pullback <= 90% of the previous pullback's depth
VCP_MAX_HIGH_DRIFT = 0.03           # later contraction highs at most 3% above the first high
VCP_MAX_LAST_VOLUME_RATIO = 1.0     # last contraction mean volume strictly below earlier contractions'

# Cup and handle
CUP_MIN_BARS = 20
CUP_MAX_BARS = 250
CUP_MIN_DEPTH = 0.10
CUP_MAX_DEPTH = 0.50
CUP_RIM_TOLERANCE = 0.05            # |right rim - left rim| / left rim
CUP_MIN_BOTTOM_FRACTION = 0.20      # share of cup bars with low in the bottom third (rounded, not V)
HANDLE_MIN_BARS = 3
HANDLE_MAX_FRACTION = 1 / 3         # handle may last up to 1/3 of the cup width (at least HANDLE_MIN_BARS)

# Head and shoulders (and inverse)
HS_MIN_HEAD_EXCESS = 0.10           # head beyond the higher shoulder by >= 10% of pattern height
HS_SHOULDER_TOLERANCE = 0.50        # |left - right shoulder| <= 50% of pattern height
HS_MIN_SHOULDER_HEIGHT = 0.30       # each shoulder >= 30% of pattern height beyond the neckline
HS_MAX_TIME_ASYMMETRY = 2.5         # wider half / narrower half, in bars

# Flags
POLE_MAX_BARS = 15
POLE_MIN_ATR = 4.0                  # pole height >= 4 x ATR at the pole start
FLAG_MIN_BARS = 3
FLAG_MAX_BARS = 15
FLAG_MAX_RETRACE = 0.50             # a close retracing more than 50% of the pole fails the flag

# Triangles and wedges (two swing highs + two swing lows, consecutive and alternating)
TRIANGLE_MIN_BARS = 10
LINE_FLAT_TOLERANCE = 0.15          # line change over the pattern / initial height counted as flat
MAX_CONVERGENCE_RATIO = 0.75        # final height <= 75% of initial height
CONTAINMENT_TOLERANCE = 0.10        # closes inside the lines +/- 10% of initial height

# Supply / demand zones
ZONE_MAX_BASE_BARS = 3
ZONE_BASE_MAX_RANGE_ATR = 0.5       # base bars: high - low <= 0.5 x ATR
ZONE_IMPULSE_MIN_RANGE_ATR = 2.0    # impulse bar: high - low >= 2 x ATR

PATTERN_KINDS = frozenset({
    "VCP", "CUP_AND_HANDLE", "HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS", "BULL_FLAG",
    "BEAR_FLAG", "ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE", "SYMMETRICAL_TRIANGLE", "RISING_WEDGE",
    "FALLING_WEDGE", "DEMAND_ZONE", "SUPPLY_ZONE",
})

EXPIRED = "EXPIRED_NO_CLOSE_BEYOND_LEVELS"


@dataclass(frozen=True, slots=True)
class Swing:
    index: int          # position in the candle sequence
    kind: str           # "HIGH" or "LOW"
    price: float        # the bar's high (HIGH) or low (LOW)


def _check_bars(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


RECENT_BARS = 5


def recent_patterns(series: CandleSeries, *, bars: int = RECENT_BARS) -> list[dict[str, Any]]:
    """Detections still forming, or resolved within the last ``bars`` closed bars (for display and alerts)."""
    tail = series.contiguous_tail()
    if not tail:
        return []
    cutoff = tail[max(0, len(tail) - bars)].open_time_ms
    return [d for d in detect_patterns(series) if d["state"] == "FORMING" or d["end_bar_open_ms"] >= cutoff]


def swing_points(candles: Sequence[Candle], left: int = SWING_LEFT, right: int = SWING_RIGHT) -> list[Swing]:
    """Confirmed swing highs/lows, ordered by index (HIGH before LOW on the same bar)."""
    _check_bars(left, "left")
    _check_bars(right, "right")
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    out: list[Swing] = []
    for i in range(left, len(candles) - right):
        h, lo = highs[i], lows[i]
        if h > max(highs[i - left:i]) and h >= max(highs[i + 1:i + right + 1]):
            out.append(Swing(i, "HIGH", h))
        if lo < min(lows[i - left:i]) and lo <= min(lows[i + 1:i + right + 1]):
            out.append(Swing(i, "LOW", lo))
    return out


def _atr_series(candles: Sequence[Candle], period: int = ATR_PERIOD) -> list[float | None]:
    """Causal Wilder ATR: value at i uses bars 0..i only (same convention as indicators.atr_wilder)."""
    out: list[float | None] = [None] * len(candles)
    tr = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
          for p, c in zip(candles, candles[1:])]
    if len(tr) < period:
        return out
    value = sum(tr[:period]) / period
    out[period] = value
    for i in range(period, len(tr)):
        value = (value * (period - 1) + tr[i]) / period
        out[i + 1] = value
    return out


class _Ctx:
    def __init__(self, candles: Sequence[Candle], left: int, right: int) -> None:
        self.candles = candles
        self.n = len(candles)
        self.right = right
        self.opens_ms = [c.open_time_ms for c in candles]
        self.highs = [c.high for c in candles]
        self.lows = [c.low for c in candles]
        self.closes = [c.close for c in candles]
        self.volumes = [c.volume for c in candles]
        self.swings = swing_points(candles, left, right)
        self.atr = _atr_series(candles)
        self.max_high = _SparseTable(self.highs, max)
        self.min_low = _SparseTable(self.lows, min)


class _SparseTable:
    """O(1) range max/min queries over inclusive index ranges."""

    def __init__(self, values: Sequence[float], fn: Callable[[float, float], float]) -> None:
        self.fn = fn
        self.table = [list(values)]
        j = 1
        while (1 << j) <= len(values):
            prev, half = self.table[-1], 1 << (j - 1)
            self.table.append([fn(prev[i], prev[i + half]) for i in range(len(values) - (1 << j) + 1)])
            j += 1

    def query(self, lo: int, hi: int) -> float:
        k = (hi - lo + 1).bit_length() - 1
        row = self.table[k]
        return self.fn(row[lo], row[hi - (1 << k) + 1])


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 10)


def _line(i0: int, p0: float, i1: int, p1: float) -> Callable[[float], float]:
    slope = (p1 - p0) / (i1 - i0)
    return lambda x: p0 + slope * (x - i0)


def _resolve(ctx: _Ctx, first: int, confirm: int, expire: int,
             upper: Callable[[int], float], lower: Callable[[int], float],
             up_state: str, down_state: str) -> tuple[str, int, str | None, int | None]:
    """Scan closes from ``first``; returns (state, end_index, resolution, resolution_index)."""
    for j in range(first, ctx.n):
        if j > expire:
            return "FAILED", max(j, confirm), EXPIRED, j
        c = ctx.closes[j]
        if c > upper(j):
            return up_state, max(j, confirm), "CLOSE_ABOVE_UPPER_LEVEL", j
        if c < lower(j):
            return down_state, max(j, confirm), "CLOSE_BELOW_LOWER_LEVEL", j
    return "FORMING", ctx.n - 1, None, None


def _emit(ctx: _Ctx, pattern: str, state: str, start: int, end: int, key_levels: dict[str, Any],
          conditions: dict[str, Any], parameters: dict[str, Any], invalidation: str,
          resolution: str | None, resolution_index: int | None, confirm: int) -> dict[str, Any]:
    cond = {k: _r(v) if isinstance(v, float) else v for k, v in conditions.items()}
    cond.update(structure_confirmed_bar_open_ms=ctx.opens_ms[confirm], resolution=resolution,
                resolution_bar_open_ms=None if resolution_index is None else ctx.opens_ms[resolution_index])
    return {
        "pattern": pattern, "state": state,
        "start_bar_open_ms": ctx.opens_ms[start], "end_bar_open_ms": ctx.opens_ms[end],
        "key_levels": {k: _r(v) if isinstance(v, float) else v for k, v in key_levels.items()},
        "conditions_met": cond, "parameters": parameters, "invalidation": invalidation,
        "rule_version": f"{pattern.lower()}/1", "note": NOTE,
    }


def _pending(span: int) -> int:
    return max(PENDING_MIN_BARS, span)


def _alternating_windows(swings: Sequence[Swing], size: int):
    for s in range(len(swings) - size + 1):
        w = swings[s:s + size]
        if all(a.kind != b.kind and a.index < b.index for a, b in zip(w, w[1:])):
            yield w


# --------------------------------------------------------------------------------------------- VCP

def _detect_vcp(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"min_contractions": VCP_MIN_CONTRACTIONS, "max_contractions": VCP_MAX_CONTRACTIONS,
              "min_first_depth": VCP_MIN_FIRST_DEPTH, "max_first_depth": VCP_MAX_FIRST_DEPTH,
              "max_depth_ratio": VCP_MAX_DEPTH_RATIO, "max_high_drift": VCP_MAX_HIGH_DRIFT,
              "max_last_volume_ratio": VCP_MAX_LAST_VOLUME_RATIO}
    sw = ctx.swings
    for end in range(len(sw)):
        if sw[end].kind != "LOW":
            continue
        for m in range(VCP_MAX_CONTRACTIONS, VCP_MIN_CONTRACTIONS - 1, -1):
            s = end - 2 * m + 1
            if s < 0:
                continue
            w = sw[s:end + 1]
            if w[0].kind != "HIGH" or not all(a.kind != b.kind and a.index < b.index for a, b in zip(w, w[1:])):
                continue
            pairs = [(w[2 * i], w[2 * i + 1]) for i in range(m)]
            depths = [(h.price - lo.price) / h.price for h, lo in pairs]
            if not VCP_MIN_FIRST_DEPTH <= depths[0] <= VCP_MAX_FIRST_DEPTH:
                continue
            if not all(b <= VCP_MAX_DEPTH_RATIO * a for a, b in zip(depths, depths[1:])):
                continue
            first_high = pairs[0][0].price
            if not all(h.price <= first_high * (1 + VCP_MAX_HIGH_DRIFT) for h, _ in pairs[1:]):
                continue
            if not all(b[1].price >= a[1].price for a, b in zip(pairs, pairs[1:])):
                continue
            last_h, last_l = pairs[-1]
            earlier = [v for h, lo in pairs[:-1] for v in ctx.volumes[h.index:lo.index + 1]]
            last = ctx.volumes[last_h.index:last_l.index + 1]
            earlier_mean = sum(earlier) / len(earlier)
            last_mean = sum(last) / len(last)
            if earlier_mean <= 0 or last_mean / earlier_mean >= VCP_MAX_LAST_VOLUME_RATIO:
                continue
            start = w[0].index
            confirm = last_l.index + ctx.right
            span = last_l.index - start
            pivot, floor = last_h.price, last_l.price
            state, e, res, ri = _resolve(ctx, last_l.index + 1, confirm, confirm + _pending(span),
                                         lambda j: pivot, lambda j: floor, "BREAKOUT_CONFIRMED", "FAILED")
            out.append(_emit(
                ctx, "VCP", state, start, e,
                {"pivot": pivot, "last_contraction_low": floor, "first_high": first_high},
                {"definition": "geometric approximation of a volatility contraction pattern; "
                               "not a Minervini VCP or SEPA evaluation",
                 "contractions": m, "depths": [round(d, 6) for d in depths],
                 "last_volume_ratio": round(last_mean / earlier_mean, 6), "lows_rising": True},
                params, f"a close below {floor:g} (low of the last contraction) before a close above {pivot:g}",
                res, ri, confirm))
            break  # longest valid contraction count for this ending swing
    return out


# -------------------------------------------------------------------------------- Cup and handle

def _detect_cup_and_handle(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"min_cup_bars": CUP_MIN_BARS, "max_cup_bars": CUP_MAX_BARS, "min_depth": CUP_MIN_DEPTH,
              "max_depth": CUP_MAX_DEPTH, "rim_tolerance": CUP_RIM_TOLERANCE,
              "min_bottom_fraction": CUP_MIN_BOTTOM_FRACTION, "handle_min_bars": HANDLE_MIN_BARS,
              "handle_max_fraction": round(HANDLE_MAX_FRACTION, 6)}
    highs = [s for s in ctx.swings if s.kind == "HIGH"]
    for ci, c in enumerate(highs):
        for a in reversed(highs[:ci]):
            width = c.index - a.index
            if width > CUP_MAX_BARS:
                break
            if width < CUP_MIN_BARS:
                continue
            rim = min(a.price, c.price)
            if ctx.max_high.query(a.index + 1, c.index - 1) > max(a.price, c.price):
                continue  # no bar inside the cup may exceed the higher rim
            if abs(c.price - a.price) / a.price > CUP_RIM_TOLERANCE:
                continue
            bottom = ctx.min_low.query(a.index + 1, c.index - 1)
            depth = (a.price - bottom) / a.price
            if not CUP_MIN_DEPTH <= depth <= CUP_MAX_DEPTH:
                continue
            third = bottom + (rim - bottom) / 3
            frac = sum(1 for lo in ctx.lows[a.index:c.index + 1] if lo <= third) / (width + 1)
            if frac < CUP_MIN_BOTTOM_FRACTION:
                continue
            mid = bottom + 0.5 * (rim - bottom)
            handle_max = max(HANDLE_MIN_BARS, int(width * HANDLE_MAX_FRACTION))
            confirm = c.index + ctx.right
            state, e, res, ri, level = "FORMING", ctx.n - 1, None, None, ctx.highs[c.index]
            drop = False
            for j in range(c.index + 1, ctx.n):
                handle_bars = j - c.index - 1
                level = ctx.max_high.query(c.index, j - 1)
                if handle_bars > handle_max:
                    state, e, res, ri = "FAILED", max(j, confirm), EXPIRED, j
                    break
                if ctx.closes[j] > level:
                    if handle_bars < HANDLE_MIN_BARS:
                        drop = True  # no handle formed: not this pattern
                    else:
                        state, e, res, ri = "BREAKOUT_CONFIRMED", max(j, confirm), "CLOSE_ABOVE_UPPER_LEVEL", j
                    break
                if ctx.closes[j] < mid:
                    state, e, res, ri = "FAILED", max(j, confirm), "CLOSE_BELOW_LOWER_LEVEL", j
                    break
            else:
                if ctx.n - 1 - c.index < HANDLE_MIN_BARS:
                    drop = True
                else:
                    level = ctx.max_high.query(c.index, ctx.n - 1)
            if drop:
                break
            out.append(_emit(
                ctx, "CUP_AND_HANDLE", state, a.index, e,
                {"left_rim": a.price, "right_rim": c.price, "cup_bottom": bottom, "breakout_level": level,
                 "handle_floor": mid},
                {"cup_bars": width, "depth": round(depth, 6), "rim_difference": round(abs(c.price - a.price) / a.price, 6),
                 "bottom_fraction": round(frac, 6), "handle_max_bars": handle_max},
                params, f"a close below {mid:g} (cup midpoint) during the handle, or no breakout within "
                        f"{handle_max} handle bars", res, ri, confirm))
            break  # nearest valid left rim for this right rim
    return out


# ------------------------------------------------------------------------ Head and shoulders

def _detect_head_and_shoulders(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"min_head_excess": HS_MIN_HEAD_EXCESS, "shoulder_tolerance": HS_SHOULDER_TOLERANCE,
              "min_shoulder_height": HS_MIN_SHOULDER_HEIGHT, "max_time_asymmetry": HS_MAX_TIME_ASYMMETRY}
    for w in _alternating_windows(ctx.swings, 5):
        top = w[0].kind == "HIGH"
        sign = 1.0 if top else -1.0
        s1, t1, head, t2, s2 = w
        neck = _line(t1.index, t1.price, t2.index, t2.price)
        height = sign * (head.price - neck(head.index))
        if height <= 0:
            continue
        sh1 = sign * (s1.price - neck(s1.index))
        sh2 = sign * (s2.price - neck(s2.index))
        excess = sign * (head.price - (max if top else min)(s1.price, s2.price))
        d1, d2 = head.index - s1.index, s2.index - head.index
        asym = max(d1, d2) / min(d1, d2)
        if (excess < HS_MIN_HEAD_EXCESS * height or abs(s1.price - s2.price) > HS_SHOULDER_TOLERANCE * height
                or min(sh1, sh2) < HS_MIN_SHOULDER_HEIGHT * height or asym > HS_MAX_TIME_ASYMMETRY):
            continue
        confirm = s2.index + ctx.right
        span = s2.index - s1.index
        expire = confirm + _pending(span)
        right_shoulder = s2.price
        if top:
            state, e, res, ri = _resolve(ctx, s2.index + 1, confirm, expire, lambda j: right_shoulder,
                                         neck, "FAILED", "BREAKDOWN_CONFIRMED")
            name = "HEAD_AND_SHOULDERS"
            inval = f"a close above {right_shoulder:g} (right shoulder high) before a close below the neckline"
        else:
            state, e, res, ri = _resolve(ctx, s2.index + 1, confirm, expire, neck,
                                         lambda j: right_shoulder, "BREAKOUT_CONFIRMED", "FAILED")
            name = "INVERSE_HEAD_AND_SHOULDERS"
            inval = f"a close below {right_shoulder:g} (right shoulder low) before a close above the neckline"
        level_at = ri if ri is not None else ctx.n - 1
        out.append(_emit(
            ctx, name, state, s1.index, e,
            {"left_shoulder": s1.price, "head": head.price, "right_shoulder": s2.price,
             "neckline_point_1": t1.price, "neckline_point_2": t2.price,
             "neckline_at_end": neck(level_at)},
            {"pattern_height": height, "head_excess": excess, "shoulder_difference": abs(s1.price - s2.price),
             "left_shoulder_height": sh1, "right_shoulder_height": sh2, "time_asymmetry": round(asym, 6)},
            params, inval, res, ri, confirm))
    return out


# --------------------------------------------------------------------------------------- Flags

def _detect_flags(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"pole_max_bars": POLE_MAX_BARS, "pole_min_atr": POLE_MIN_ATR, "flag_min_bars": FLAG_MIN_BARS,
              "flag_max_bars": FLAG_MAX_BARS, "flag_max_retrace": FLAG_MAX_RETRACE, "atr_period": ATR_PERIOD}
    for p in ctx.swings:
        bull = p.kind == "HIGH"
        lo_i = max(0, p.index - POLE_MAX_BARS)
        if p.index - lo_i < 2:
            continue
        seg = range(lo_i, p.index)
        s = min(seg, key=lambda i: ctx.lows[i]) if bull else max(seg, key=lambda i: ctx.highs[i])
        base = ctx.lows[s] if bull else ctx.highs[s]
        pole = (p.price - base) if bull else (base - p.price)
        atr = ctx.atr[s]
        if atr is None or atr <= 0 or pole < POLE_MIN_ATR * atr or p.index - s < 2:
            continue
        fail_level = p.price - FLAG_MAX_RETRACE * pole if bull else p.price + FLAG_MAX_RETRACE * pole
        confirm = p.index + ctx.right
        state, e, res, ri, level, drop = "FORMING", ctx.n - 1, None, None, None, False
        for j in range(p.index + 1, ctx.n):
            flag_bars = j - p.index - 1
            if flag_bars > FLAG_MAX_BARS:
                state, e, res, ri = "FAILED", max(j, confirm), EXPIRED, j
                break
            c = ctx.closes[j]
            if flag_bars >= 1:
                level = (ctx.max_high.query(p.index + 1, j - 1) if bull
                         else ctx.min_low.query(p.index + 1, j - 1))
            beyond = level is not None and (c > level if bull else c < level)
            beyond_pole = c > p.price if bull else c < p.price
            if beyond or beyond_pole:
                if flag_bars < FLAG_MIN_BARS:
                    drop = True
                else:
                    state = "BREAKOUT_CONFIRMED" if bull else "BREAKDOWN_CONFIRMED"
                    e, ri = max(j, confirm), j
                    res = "CLOSE_ABOVE_UPPER_LEVEL" if bull else "CLOSE_BELOW_LOWER_LEVEL"
                break
            if (c < fail_level) if bull else (c > fail_level):
                state, e, ri = "FAILED", max(j, confirm), j
                res = "CLOSE_BELOW_LOWER_LEVEL" if bull else "CLOSE_ABOVE_UPPER_LEVEL"
                break
        else:
            if ctx.n - 1 - p.index < FLAG_MIN_BARS:
                drop = True
            else:
                level = (ctx.max_high.query(p.index + 1, ctx.n - 1) if bull
                         else ctx.min_low.query(p.index + 1, ctx.n - 1))
        if drop:
            continue
        flag_end = (ri if ri is not None else ctx.n) - 1
        flag_vol = ctx.volumes[p.index + 1:flag_end + 1]
        pole_vol = ctx.volumes[s:p.index + 1]
        vol_ratio = (sum(flag_vol) / len(flag_vol)) / (sum(pole_vol) / len(pole_vol)) \
            if flag_vol and sum(pole_vol) > 0 else None
        name = "BULL_FLAG" if bull else "BEAR_FLAG"
        out.append(_emit(
            ctx, name, state, s, e,
            {"pole_start": base, "pole_end": p.price, "breakout_level": level, "failure_level": fail_level},
            {"pole_bars": p.index - s, "pole_height": pole, "atr_at_pole_start": atr,
             "pole_height_atr": round(pole / atr, 6), "flag_bars": flag_end - p.index,
             "flag_to_pole_volume_ratio": None if vol_ratio is None else round(vol_ratio, 6)},
            params,
            f"a close {'below' if bull else 'above'} {fail_level:g} (more than {FLAG_MAX_RETRACE:.0%} of the pole "
            f"retraced) or no breakout within {FLAG_MAX_BARS} flag bars", res, ri, confirm))
    return out


# --------------------------------------------------------------------- Triangles and wedges

def _classify(rel_upper: float, rel_lower: float) -> str | None:
    def d(rel: float) -> str:
        return "flat" if abs(rel) <= LINE_FLAT_TOLERANCE else "up" if rel > 0 else "down"
    return {("flat", "up"): "ASCENDING_TRIANGLE", ("down", "flat"): "DESCENDING_TRIANGLE",
            ("down", "up"): "SYMMETRICAL_TRIANGLE", ("up", "up"): "RISING_WEDGE",
            ("down", "down"): "FALLING_WEDGE"}.get((d(rel_upper), d(rel_lower)))


# (state on close above upper line, state on close below lower line)
_TRIANGLE_STATES = {
    "ASCENDING_TRIANGLE": ("BREAKOUT_CONFIRMED", "FAILED"),
    "DESCENDING_TRIANGLE": ("FAILED", "BREAKDOWN_CONFIRMED"),
    "SYMMETRICAL_TRIANGLE": ("BREAKOUT_CONFIRMED", "BREAKDOWN_CONFIRMED"),
    "RISING_WEDGE": ("FAILED", "BREAKDOWN_CONFIRMED"),
    "FALLING_WEDGE": ("BREAKOUT_CONFIRMED", "FAILED"),
}


def _detect_triangles(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"min_bars": TRIANGLE_MIN_BARS, "line_flat_tolerance": LINE_FLAT_TOLERANCE,
              "max_convergence_ratio": MAX_CONVERGENCE_RATIO, "containment_tolerance": CONTAINMENT_TOLERANCE}
    for w in _alternating_windows(ctx.swings, 4):
        hs = [s for s in w if s.kind == "HIGH"]
        ls = [s for s in w if s.kind == "LOW"]
        x0, x1 = w[0].index, w[-1].index
        span = x1 - x0
        if span < TRIANGLE_MIN_BARS:
            continue
        upper = _line(hs[0].index, hs[0].price, hs[1].index, hs[1].price)
        lower = _line(ls[0].index, ls[0].price, ls[1].index, ls[1].price)
        h0, h1 = upper(x0) - lower(x0), upper(x1) - lower(x1)
        if h0 <= 0 or h1 <= 0 or h1 / h0 > MAX_CONVERGENCE_RATIO:
            continue
        rel_u, rel_l = (upper(x1) - upper(x0)) / h0, (lower(x1) - lower(x0)) / h0
        name = _classify(rel_u, rel_l)
        if name is None:
            continue
        tol = CONTAINMENT_TOLERANCE * h0
        if any(not lower(i) - tol <= ctx.closes[i] <= upper(i) + tol for i in range(x0, x1 + 1)):
            continue
        # Lines meet where height reaches zero (height shrinks linearly).
        apex = x1 + h1 / ((h0 - h1) / span)
        confirm = x1 + ctx.right
        expire = min(confirm + _pending(span), int(apex))
        up_state, down_state = _TRIANGLE_STATES[name]
        state, e, res, ri = _resolve(ctx, x1 + 1, confirm, expire, upper, lower, up_state, down_state)
        at = ri if ri is not None else ctx.n - 1
        out.append(_emit(
            ctx, name, state, x0, e,
            {"upper_line_at_end": upper(at), "lower_line_at_end": lower(at),
             "upper_points": [hs[0].price, hs[1].price], "lower_points": [ls[0].price, ls[1].price],
             "apex_bars_after_last_swing": round(apex - x1, 4)},
            {"bars": span, "initial_height": h0, "final_height": h1, "convergence_ratio": round(h1 / h0, 6),
             "upper_change_rel": round(rel_u, 6), "lower_change_rel": round(rel_l, 6),
             "closes_contained": True},
            params,
            "a close beyond the opposite line, or no close beyond either line before the apex/pending window",
            res, ri, confirm))
    return out


# ------------------------------------------------------------------------ Supply / demand zones

def _detect_zones(ctx: _Ctx) -> list[dict[str, Any]]:
    out = []
    params = {"max_base_bars": ZONE_MAX_BASE_BARS, "base_max_range_atr": ZONE_BASE_MAX_RANGE_ATR,
              "impulse_min_range_atr": ZONE_IMPULSE_MIN_RANGE_ATR, "atr_period": ATR_PERIOD}
    for i in range(1, ctx.n):
        atr = ctx.atr[i - 1]
        if atr is None or atr <= 0:
            continue
        c = ctx.candles[i]
        rng = c.high - c.low
        if rng < ZONE_IMPULSE_MIN_RANGE_ATR * atr:
            continue
        b = 0
        while b < ZONE_MAX_BASE_BARS and i - b - 1 >= 0 and \
                ctx.highs[i - b - 1] - ctx.lows[i - b - 1] <= ZONE_BASE_MAX_RANGE_ATR * atr:
            b += 1
        if b == 0:
            continue
        zhigh = max(ctx.highs[i - b:i])
        zlow = min(ctx.lows[i - b:i])
        if c.close > c.open and c.close > zhigh:
            demand = True
        elif c.close < c.open and c.close < zlow:
            demand = False
        else:
            continue
        returned_at = None
        state, e, res, ri = "FORMING", ctx.n - 1, None, None
        for j in range(i + 1, ctx.n):
            if returned_at is None and (ctx.lows[j] <= zhigh if demand else ctx.highs[j] >= zlow):
                returned_at = j
            if (ctx.closes[j] < zlow) if demand else (ctx.closes[j] > zhigh):
                state, e, ri = "FAILED", j, j
                res = "CLOSE_BELOW_LOWER_LEVEL" if demand else "CLOSE_ABOVE_UPPER_LEVEL"
                break
        name = "DEMAND_ZONE" if demand else "SUPPLY_ZONE"
        out.append(_emit(
            ctx, name, state, i - b, e,
            {"zone_high": zhigh, "zone_low": zlow, "returned": returned_at is not None,
             "first_return_bar_open_ms": None if returned_at is None else ctx.opens_ms[returned_at]},
            {"base_bars": b, "atr_before_impulse": atr, "impulse_range": rng,
             "impulse_range_atr": round(rng / atr, 6), "impulse_close": c.close},
            params,
            f"a close {'below' if demand else 'above'} {(zlow if demand else zhigh):g} breaks the zone",
            res, ri, i))
    return out


_DETECTORS: tuple[tuple[frozenset[str], Callable[[_Ctx], list[dict[str, Any]]]], ...] = (
    (frozenset({"VCP"}), _detect_vcp),
    (frozenset({"CUP_AND_HANDLE"}), _detect_cup_and_handle),
    (frozenset({"HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS"}), _detect_head_and_shoulders),
    (frozenset({"BULL_FLAG", "BEAR_FLAG"}), _detect_flags),
    (frozenset({"ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE", "SYMMETRICAL_TRIANGLE", "RISING_WEDGE",
                "FALLING_WEDGE"}), _detect_triangles),
    (frozenset({"DEMAND_ZONE", "SUPPLY_ZONE"}), _detect_zones),
)


def detect_candles(candles: Sequence[Candle], *, kinds: set[str] | None = None,
                   swing_left: int = SWING_LEFT, swing_right: int = SWING_RIGHT) -> list[dict[str, Any]]:
    """Detect patterns in a gap-free, ordered sequence of closed candles (see ``detect_patterns``)."""
    wanted = PATTERN_KINDS if kinds is None else frozenset(kinds)
    unknown = wanted - PATTERN_KINDS
    if unknown:
        raise ValueError(f"Unknown pattern kind(s): {sorted(unknown)}")
    ctx = _Ctx(candles, swing_left, swing_right)
    if ctx.n == 0:
        return []
    found: list[dict[str, Any]] = []
    for names, fn in _DETECTORS:
        if names & wanted:
            found.extend(d for d in fn(ctx) if d["pattern"] in wanted)
    best: dict[tuple[str, int, str], dict[str, Any]] = {}
    for d in found:
        key = (d["pattern"], d["end_bar_open_ms"], d["state"])
        if key not in best or d["start_bar_open_ms"] < best[key]["start_bar_open_ms"]:
            best[key] = d
    return sorted(best.values(), key=lambda d: (d["end_bar_open_ms"], d["start_bar_open_ms"], d["pattern"]))


def detect_patterns(series: CandleSeries, *, kinds: set[str] | None = None,
                    swing_left: int = SWING_LEFT, swing_right: int = SWING_RIGHT) -> list[dict[str, Any]]:
    """All detections in ``series.contiguous_tail()``, sorted by end bar then start bar.

    ``kinds`` restricts output to a subset of ``PATTERN_KINDS`` (unknown names raise ValueError).
    """
    return detect_candles(series.contiguous_tail(), kinds=kinds, swing_left=swing_left,
                          swing_right=swing_right)
