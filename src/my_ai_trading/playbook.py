"""Trading-playbook profiles and breakout-quality checks (spec: docs/specs/trading-playbook-features.md).

Two research profiles read the same geometric detections (``patterns.py``) with different rules:
``brandt`` [BP] and ``edwards_magee`` [EM]. Each point where the books disagree is one field of
``ProfileConfig`` (spec §3.3). Nothing here is a trade signal: ``breakout_quality`` measures a detection
and ``verdict`` says which of the profile's completion rules it meets.

Measured per detection (all from closed bars up to the detection's resolution bar):
- ``duration_weeks``: calendar time from the pattern's first bar to its resolution (or newest) bar.
- ``boundary_kind``: HORIZONTAL or DIAGONAL side that was broken (Brandt prefers horizontal) [BP ch3, ch11].
- ``close_beyond_pct``: how far the resolution close went beyond the broken level [EM ~3%].
- ``beyond_defining_swing``: whether that close also cleared the swing point that defines the side [BP ch3].
- ``volume_ratio``: resolution-bar volume / mean of the 20 bars before it [EM: upside breaks need volume].
- ``apex_fraction``: for triangles and wedges, how far toward the apex the break came [both: > 3/4 is late].

Parameter provenance: the profile values below paraphrase the playbook (§3.3 of the spec). The 1.5x volume
ratio for a "clear increase" is the engine's existing breakout threshold, chosen here, not a book number.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .domain import DAY_MS, Candle
from .indicators import volume_ratio
from .patterns import SWING_RIGHT, detect_candles, swing_points
from .stops import last_day_rule

WEEK_MS = 7 * DAY_MS
VOLUME_LOOKBACK = 20
PLAYBOOK_VERSION = "playbook/1"
COMPLETION_WINDOW = 5     # bars from the first close beyond the level to meet the profile's filter (spec Q2)

COMPLETED = "COMPLETED"
NOTE = "Rule check of a geometric detection; not proof of a pattern and not a trade signal."


@dataclass(frozen=True)
class ProfileConfig:
    profile_id: str
    close_beyond_min_pct: float      # minimum close beyond the broken level, as a fraction of the level
    require_beyond_swing: bool       # the close must also clear the swing that defines the side
    volume_required_up: bool         # upside breaks need volume_ratio >= volume_min_ratio
    volume_min_ratio: float
    min_standalone_weeks: float      # shorter patterns are not standalone entries
    major_weeks: float               # patterns at least this long count as major
    apex_cutoff: float               # triangle/wedge breaks beyond this fraction are late
    double_min_weeks: float          # double tops: time between the two extremes
    double_bottom_min_weeks: float   # double bottoms: time between the two extremes
    double_min_depth: float          # double tops/bottoms: depth of the middle reaction
    min_reversal_weeks: float        # reversal patterns (H&S, doubles) shorter than this are not standalone entries
    min_wedge_weeks: float           # wedges shorter than this are not counted
    entry_style: str                 # breakout_stop_order | pullback_40_50
    initial_stop: str                # last_day_rule | nearest_minor_swing
    risk_fraction: float             # default per-trade risk fraction for sizing scenarios
    uses_volume: bool
    source: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[str, ProfileConfig] = {
    "brandt": ProfileConfig(
        "brandt", close_beyond_min_pct=0.0, require_beyond_swing=True, volume_required_up=False,
        volume_min_ratio=1.5, min_standalone_weeks=6.0, major_weeks=10.0, apex_cutoff=0.75,
        double_min_weeks=8.7, double_bottom_min_weeks=8.7, double_min_depth=0.15, min_reversal_weeks=8.0,
        min_wedge_weeks=0.0,
        entry_style="breakout_stop_order", initial_stop="last_day_rule", risk_fraction=0.007, uses_volume=False,
        source="Brandt, Diary of a Professional Commodity Trader (2011): ch3-5, ch9-13"),
    "edwards_magee": ProfileConfig(
        "edwards_magee", close_beyond_min_pct=0.03, require_beyond_swing=False, volume_required_up=True,
        volume_min_ratio=1.5, min_standalone_weeks=0.0, major_weeks=10.0, apex_cutoff=0.75,
        double_min_weeks=4.3, double_bottom_min_weeks=6.0, double_min_depth=0.15, min_reversal_weeks=0.0,
        min_wedge_weeks=3.0,
        entry_style="pullback_40_50", initial_stop="nearest_minor_swing", risk_fraction=0.008, uses_volume=True,
        source="Edwards, Magee & Bassetti, Technical Analysis of Stock Trends, 9th ed. (2007): ch3, ch6-13, ch27-28"),
}

_TRIANGLES = {"ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE", "SYMMETRICAL_TRIANGLE", "RISING_WEDGE", "FALLING_WEDGE"}
_REVERSALS = {"HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS", "DOUBLE_TOP", "DOUBLE_BOTTOM"}
_WEDGES = {"RISING_WEDGE", "FALLING_WEDGE"}

LABELS = {
    "COMPLETED": "Completed by this profile's rules", "FORMING": "Still forming",
    "NOT_COMPLETED": "Broke out, but not decisively enough", "TOO_SHORT": "Too short to trade on its own",
    "LATE_APEX": "Broke too close to the triangle apex", "UNCONFIRMED_VOLUME": "Upside break without volume",
    "EXTREMES_TOO_CLOSE": "Tops or bottoms too close in time", "TOO_SHALLOW": "Middle reaction too shallow",
    "PATTERN_FAILURE": "Pattern failed (broke the other way)", "EXPIRED": "Expired without a break",
    "NOT_A_BREAKOUT_PATTERN": "Not a breakout pattern",
}


def _sides(d: dict[str, Any]) -> dict[str, tuple[float | None, float | None, str]] | None:
    """Per break direction: (level at resolution, defining swing, boundary kind). None = not a breakout pattern."""
    k, p = d["key_levels"], d["pattern"]
    if p in _TRIANGLES:
        up_kind = "HORIZONTAL" if p == "ASCENDING_TRIANGLE" else "DIAGONAL"
        down_kind = "HORIZONTAL" if p == "DESCENDING_TRIANGLE" else "DIAGONAL"
        return {"up": (k["upper_line_at_end"], k["upper_points"][-1], up_kind),
                "down": (k["lower_line_at_end"], k["lower_points"][-1], down_kind)}
    if p in ("HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS"):
        neck = "HORIZONTAL" if abs(k["neckline_point_1"] - k["neckline_point_2"]) <= \
            0.15 * d["conditions_met"]["pattern_height"] else "DIAGONAL"
        if p == "HEAD_AND_SHOULDERS":
            return {"down": (k["neckline_at_end"], k["neckline_point_2"], neck),
                    "up": (k["right_shoulder"], k["right_shoulder"], "HORIZONTAL")}
        return {"up": (k["neckline_at_end"], k["neckline_point_2"], neck),
                "down": (k["right_shoulder"], k["right_shoulder"], "HORIZONTAL")}
    if p == "RECTANGLE":
        return {"up": (k["upper"], k["upper"], "HORIZONTAL"), "down": (k["lower"], k["lower"], "HORIZONTAL")}
    if p == "DOUBLE_TOP":
        hi = max(k["first_extreme"], k["second_extreme"])
        return {"down": (k["middle"], k["middle"], "HORIZONTAL"), "up": (hi, hi, "HORIZONTAL")}
    if p == "DOUBLE_BOTTOM":
        lo = min(k["first_extreme"], k["second_extreme"])
        return {"up": (k["middle"], k["middle"], "HORIZONTAL"), "down": (lo, lo, "HORIZONTAL")}
    if p == "BULL_FLAG":
        return {"up": (k["breakout_level"], k["breakout_level"], "DIAGONAL"),
                "down": (k["failure_level"], k["failure_level"], "HORIZONTAL")}
    if p == "BEAR_FLAG":
        return {"down": (k["breakout_level"], k["breakout_level"], "DIAGONAL"),
                "up": (k["failure_level"], k["failure_level"], "HORIZONTAL")}
    if p == "CUP_AND_HANDLE":
        return {"up": (k["breakout_level"], k["breakout_level"], "HORIZONTAL"),
                "down": (k["handle_floor"], k["handle_floor"], "HORIZONTAL")}
    if p == "VCP":
        return {"up": (k["pivot"], k["pivot"], "HORIZONTAL"),
                "down": (k["last_contraction_low"], k["last_contraction_low"], "HORIZONTAL")}
    return None


def breakout_quality(d: dict[str, Any], candles: Sequence[Candle], cfg: ProfileConfig,
                     index: dict[int, int] | None = None) -> dict[str, Any] | None:
    """Measurements of one detection from ``detect_candles(candles)`` under ``cfg``; None for zones.

    The break is judged from the detection's end bar (its first close beyond the level, or the bar that confirms
    the pattern's last swing if that comes later), so nothing uses a swing before it is confirmed. A close that
    misses the profile's filter (3% for EM, the defining swing for BP) may be followed by one that meets it within
    ``COMPLETION_WINDOW`` bars; a close back inside the pattern first ends the search. The level of a diagonal side
    is its value at the first close beyond it."""
    sides = _sides(d)
    if sides is None:
        return None
    index = index if index is not None else {c.open_time_ms: i for i, c in enumerate(candles)}
    cond = d["conditions_met"]
    end_i = index[d["end_bar_open_ms"]]
    q: dict[str, Any] = {"duration_weeks": round((d["end_bar_open_ms"] - d["start_bar_open_ms"]) / WEEK_MS, 4),
                         "direction": None, "boundary_kind": None, "level": None, "completion": None,
                         "breakout_bar_open_ms": None, "signal_bar_open_ms": None, "close_beyond_pct": None,
                         "beyond_defining_swing": None, "volume_ratio": None,
                         "apex_fraction": cond.get("resolution_apex_fraction")}
    res = cond["resolution"]
    if res not in ("CLOSE_ABOVE_UPPER_LEVEL", "CLOSE_BELOW_LOWER_LEVEL"):
        return q
    direction = "up" if res == "CLOSE_ABOVE_UPPER_LEVEL" else "down"
    level, swing, kind = sides[direction]
    sign = 1 if direction == "up" else -1

    def met(j: int) -> bool:
        c = candles[j].close
        return sign * (c - level) / level >= cfg.close_beyond_min_pct and \
            (not cfg.require_beyond_swing or sign * (c - swing) > 0)

    completion, j = "PENDING", end_i
    for k in range(end_i, min(end_i + COMPLETION_WINDOW, len(candles))):
        j = k
        if sign * (candles[k].close - level) <= 0:
            completion = "BACK_INSIDE"
            break
        if met(k):
            completion = "MET"
            break
    else:
        if end_i + COMPLETION_WINDOW <= len(candles):
            completion = "NOT_MET"
    close = candles[j].close
    b = index[cond["resolution_bar_open_ms"]]
    ratios = [volume_ratio(candles[max(0, k - VOLUME_LOOKBACK):k + 1], VOLUME_LOOKBACK) for k in {b, j}]
    known = [r for r in ratios if r is not None]
    vr = max(known) if known else None  # volume on the breakout bar or on the bar that completed it
    q.update(direction=direction, boundary_kind=kind, level=level, completion=completion,
             breakout_bar_open_ms=cond["resolution_bar_open_ms"], signal_bar_open_ms=candles[j].open_time_ms,
             duration_weeks=round((candles[j].open_time_ms - d["start_bar_open_ms"]) / WEEK_MS, 4),
             close_beyond_pct=round(sign * (close - level) / level, 6),
             beyond_defining_swing=sign * (close - swing) > 0,
             volume_ratio=None if vr is None else round(vr, 4))
    return q


def verdict(d: dict[str, Any], q: dict[str, Any] | None, cfg: ProfileConfig) -> dict[str, Any]:
    """Which of the profile's completion rules a detection meets. A break against the pattern's expected side
    (H&S failure, triangle failure) is PATTERN_FAILURE whatever else holds; otherwise ``status`` is the first
    unmet rule, or COMPLETED. ``label`` and ``unmet_labels`` are display text for the same codes."""
    def out(status: str, unmet: list[str], failure: bool = False, major: bool = False) -> dict[str, Any]:
        return {"profile": cfg.profile_id, "status": status, "label": LABELS[status], "unmet": unmet,
                "unmet_labels": [LABELS[u] for u in unmet], "pattern_failure": failure, "major": major}
    if q is None:
        return out("NOT_A_BREAKOUT_PATTERN", [])
    if d["state"] == "FORMING":
        return out("FORMING", [])
    if q["direction"] is None:
        return out("EXPIRED", [])
    unmet: list[str] = []
    if q["completion"] != "MET":
        unmet.append("NOT_COMPLETED")
    if q["apex_fraction"] is not None and q["apex_fraction"] > cfg.apex_cutoff:
        unmet.append("LATE_APEX")
    pattern, weeks = d["pattern"], q["duration_weeks"]
    if weeks < cfg.min_standalone_weeks or (pattern in _REVERSALS and weeks < cfg.min_reversal_weeks) \
            or (pattern in _WEDGES and weeks < cfg.min_wedge_weeks):
        unmet.append("TOO_SHORT")
    if pattern in ("DOUBLE_TOP", "DOUBLE_BOTTOM"):
        c = d["conditions_met"]
        need = cfg.double_min_weeks if pattern == "DOUBLE_TOP" else cfg.double_bottom_min_weeks
        if (c["second_extreme_bar_open_ms"] - d["start_bar_open_ms"]) / WEEK_MS < need:
            unmet.append("EXTREMES_TOO_CLOSE")
        if c["depth"] < cfg.double_min_depth:
            unmet.append("TOO_SHALLOW")
    if cfg.volume_required_up and q["direction"] == "up" and (q["volume_ratio"] is None
                                                              or q["volume_ratio"] < cfg.volume_min_ratio):
        unmet.append("UNCONFIRMED_VOLUME")
    failure = d["state"] == "FAILED"
    status = "PATTERN_FAILURE" if failure else unmet[0] if unmet else COMPLETED
    return out(status, unmet, failure, weeks >= cfg.major_weeks)


def assess_detections(detections: Sequence[dict[str, Any]], candles: Sequence[Candle],
                      cfg: ProfileConfig) -> list[dict[str, Any]]:
    """Attach ``quality`` and ``verdict`` to each breakout detection (zones are left out). ``signal_index`` and
    ``breakout_index`` locate the judged bars in ``candles``."""
    index = {c.open_time_ms: i for i, c in enumerate(candles)}
    out = []
    for d in detections:
        q = breakout_quality(d, candles, cfg, index)
        if q is None:
            continue
        item = {**d, "quality": q, "verdict": verdict(d, q, cfg), "playbook_version": PLAYBOOK_VERSION}
        if q["signal_bar_open_ms"] is not None:
            item.update(signal_index=index[q["signal_bar_open_ms"]], breakout_index=index[q["breakout_bar_open_ms"]])
        out.append(item)
    return out


# ---------- backtest signals ----------

def measured_height(d: dict[str, Any]) -> float | None:
    """Pattern height used for the measured-move target (playbook §9.6); None when the pattern has none here."""
    k, c, p = d["key_levels"], d["conditions_met"], d["pattern"]
    if p == "RECTANGLE":
        return k["upper"] - k["lower"]
    if p == "DOUBLE_TOP":  # from the lower top: the smaller, more conservative height
        return min(k["first_extreme"], k["second_extreme"]) - k["middle"]
    if p == "DOUBLE_BOTTOM":
        return k["middle"] - max(k["first_extreme"], k["second_extreme"])
    if p in ("HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS"):
        return c["pattern_height"]
    if p in _TRIANGLES:
        return c["initial_height"]
    if p in ("BULL_FLAG", "BEAR_FLAG"):
        return c["pole_height"]
    if p == "CUP_AND_HANDLE":
        return k["right_rim"] - k["cup_bottom"]
    return None


def _segments(candles: Sequence[Candle], timeframe: Any) -> list[tuple[int, int]]:
    out, start = [], 0
    for i in range(1, len(candles)):
        if candles[i].open_time_ms != timeframe.next_open_ms(candles[i - 1].open_time_ms):
            out.append((start, i))
            start = i
    if candles:
        out.append((start, len(candles)))
    return out


def _signals_both(series: Any, profile_id: str) -> dict[str, tuple[list[str], list[dict[str, Any] | None]]]:
    """Signals and plans for both directions from ONE detection pass per gap-free segment."""
    cfg = PROFILES[profile_id]
    candles = series.candles
    out = {d: (["NO_MATCH"] * len(candles), [None] * len(candles)) for d in ("long", "short")}
    for s, e in _segments(candles, series.timeframe):
        run = candles[s:e]
        swings = swing_points(run)
        for d in assess_detections(detect_candles(run), run, cfg):
            q, v = d["quality"], d["verdict"]
            if v["status"] != COMPLETED:
                continue
            direction = "long" if q["direction"] == "up" else "short"
            sign = 1 if direction == "long" else -1
            signals, plans = out[direction]
            i, b = d["signal_index"], d["breakout_index"]
            if signals[s + i] == "MATCH":
                continue  # the first completed pattern on a bar wins
            level = q["level"]
            stop: float | None
            if cfg.initial_stop == "last_day_rule":
                try:
                    stop = last_day_rule(run, b, direction, level).price
                except ValueError:
                    stop = None
            else:
                stop = nearest_minor_swing(swings, run, i, direction)
            h = measured_height(d)
            target = None if h is None else level + sign * h
            if target is not None and sign * (target - run[i].close) <= 0:
                continue  # the measured move was already used up by the signal close: nothing left to trade
            signals[s + i] = "MATCH"
            plans[s + i] = {"pattern": d["pattern"], "level": level, "stop": stop, "target": target}
    return out


def nearest_minor_swing(swings: Sequence[Any], candles: Sequence[Candle], i: int, direction: str) -> float | None:
    """The latest swing low (long) or high (short) confirmed by bar ``i`` and beyond its close; EM's stop anchor
    (their percentage table is for stocks and is not applied)."""
    sign = 1 if direction == "long" else -1
    kind = "LOW" if sign == 1 else "HIGH"
    prior = [w.price for w in swings if w.kind == kind and w.index + SWING_RIGHT <= i
             and sign * (candles[i].close - w.price) > 0]
    return prior[-1] if prior else None


def pattern_signals(series: Any, profile_id: str, direction: str) -> tuple[list[str], list[dict[str, Any] | None]]:
    """Per bar of ``series``: MATCH on the bar whose close completes a break in ``direction`` under
    ``profile_id``'s rules, with a plan {pattern, level, stop, target}. Detection runs per gap-free segment. The
    signal bar is never before the bar that confirms the pattern's last swing, and every input to the signal and
    its plan comes from bars up to the signal bar, so no signal uses later bars."""
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")
    return _signals_both(series, profile_id)[direction]


# ---------- profile evaluation helpers ----------

def weekly_from_daily(candles: Sequence[Candle]) -> list[Candle]:
    """Complete UTC weeks (Monday 00:00 open, 7 consecutive daily bars) built from daily bars; a partial
    week at either end is dropped, so no weekly bar contains a day that has not closed."""
    out: list[Candle] = []
    i = 0
    while i < len(candles):
        c = candles[i]
        days = (c.open_time_ms // DAY_MS - 4) % 7  # epoch day 0 was a Thursday; Monday -> 0
        if days != 0 or i + 7 > len(candles):
            i += 1
            continue
        week = candles[i:i + 7]
        if any(b.open_time_ms != c.open_time_ms + k * DAY_MS for k, b in enumerate(week)):
            i += 1
            continue
        out.append(Candle(c.open_time_ms, c.open_time_ms + WEEK_MS - 1, c.open, max(b.high for b in week),
                          min(b.low for b in week), week[-1].close, sum(b.volume for b in week),
                          sum(b.quote_volume for b in week)))
        i += 7
    return out


def swing_trend(candles: Sequence[Candle]) -> dict[str, Any]:
    """Dow-style trend from the last two confirmed swing highs and lows [EM ch3-5]: higher highs and higher lows
    = UP, lower highs and lower lows = DOWN, anything else = MIXED. A latest close beyond the last swing against
    the trend is TRANSITION (the sequence is broken but not yet replaced)."""
    sw = swing_points(candles)
    highs = [s.price for s in sw if s.kind == "HIGH"][-2:]
    lows = [s.price for s in sw if s.kind == "LOW"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return {"state": "UNKNOWN", "reason": "needs two confirmed swing highs and two swing lows"}
    up = highs[1] > highs[0] and lows[1] > lows[0]
    down = highs[1] < highs[0] and lows[1] < lows[0]
    close = candles[-1].close
    # A close through the last swing on the other side breaks the sequence before new swings confirm it.
    if (up and close < lows[1]) or (down and close > highs[1]):
        state = "TRANSITION"
    else:
        state = "UP" if up else "DOWN" if down else "MIXED"
    return {"state": state, "swing_highs": highs, "swing_lows": lows,
            "definition": "last two confirmed swing highs and lows (3 bars each side)"}


# ---------- honesty checks ----------

TRACK_MAX_BARS = 60
SENSITIVITY_PIVOTS = (2, 3, 4)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; None without observations."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def track_record(series: Any, profile_id: str) -> dict[str, Any]:
    """How this profile's completed signals played out earlier on this same series: for each one with a stop and a
    target, did price reach the target or the stop first (a bar touching both counts as the stop) within
    ``TRACK_MAX_BARS`` bars after the signal bar. Costs are ignored; this is a count, not a backtest."""
    candles = series.candles
    target_first = stop_first = unresolved = no_levels = 0
    for direction, (_signals, plans) in _signals_both(series, profile_id).items():
        sign = 1 if direction == "long" else -1
        for i, plan in enumerate(plans):
            if plan is None:
                continue
            stop, target = plan["stop"], plan["target"]
            if stop is None or target is None:
                no_levels += 1
                continue
            outcome = None
            for c in candles[i + 1:i + 1 + TRACK_MAX_BARS]:
                if (c.low <= stop) if sign == 1 else (c.high >= stop):
                    outcome = "stop"
                    break
                if (c.high >= target) if sign == 1 else (c.low <= target):
                    outcome = "target"
                    break
            if outcome == "target":
                target_first += 1
            elif outcome == "stop":
                stop_first += 1
            else:
                unresolved += 1
    n = target_first + stop_first
    ci = wilson_interval(target_first, n)
    return {"resolved": n, "target_first": target_first, "stop_first": stop_first,
            "unresolved_or_recent": unresolved, "without_stop_or_target": no_levels,
            "target_first_rate": None if not n else round(target_first / n, 4),
            "target_first_rate_ci95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
            "max_bars": TRACK_MAX_BARS,
            "note": "Earlier signals of this profile on this symbol and timeframe only; a small sample says little. "
                    "Costs, slippage and gaps through levels are ignored."}


SENSITIVITY_TOLERANCE_BARS = 5
SENSITIVITY_MIN_OVERLAP = 0.5  # share of the original pattern's bars the other detection must also cover


def _overlap(x: dict[str, Any], d: dict[str, Any]) -> float:
    span = d["end_bar_open_ms"] - d["start_bar_open_ms"]
    common = min(x["end_bar_open_ms"], d["end_bar_open_ms"]) - max(x["start_bar_open_ms"], d["start_bar_open_ms"])
    return 1.0 if span <= 0 else max(0.0, common) / span


def pivot_sensitivity(candles: Sequence[Candle], d: dict[str, Any]) -> dict[str, Any]:
    """Whether the same pattern (kind, outcome, end within 5 bars, covering at least half of the original's bars)
    is still found when swing points use 2 or 4 bars on each side instead of the default 3 that found it. A
    pattern found under one setting only is fragile. The check uses bars up to 5 bars after the detection's end bar (a wider pivot needs those bars to
    confirm) and is PENDING until they have closed, so a finished answer never changes later."""
    end = next((i for i, c in enumerate(candles) if c.open_time_ms == d["end_bar_open_ms"]), None)
    base = {"pivot_bars_tried": list(SENSITIVITY_PIVOTS)}
    if end is None or end + SENSITIVITY_TOLERANCE_BARS >= len(candles):
        return {**base, "state": "PENDING", "found_with": [3], "robust": None}
    window = candles[:end + SENSITIVITY_TOLERANCE_BARS + 1]
    tol = SENSITIVITY_TOLERANCE_BARS * (candles[1].open_time_ms - candles[0].open_time_ms)
    found = []
    for k in SENSITIVITY_PIVOTS:
        if k == 3:  # the default setting found ``d``
            found.append(k)
            continue
        dets = detect_candles(window, kinds={d["pattern"]}, swing_left=k, swing_right=k)
        if any(x["state"] == d["state"] and abs(x["end_bar_open_ms"] - d["end_bar_open_ms"]) <= tol
               and _overlap(x, d) >= SENSITIVITY_MIN_OVERLAP for x in dets):
            found.append(k)
    return {**base, "state": "DONE", "found_with": found, "robust": len(found) == len(SENSITIVITY_PIVOTS)}
