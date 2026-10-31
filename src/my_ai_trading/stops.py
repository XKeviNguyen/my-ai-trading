"""Stop rules from the trading playbook (§9.4-9.5), as pure functions over CLOSED candles.

Each rule returns a ``StopLevel`` naming the bar the level came from, so a reader can check it on a
chart. Nothing here places or moves an order; the levels are study figures (see ``risk.py``).

- ``last_day_rule`` (Brandt, LDR) [BP ch3, ch6, ch9-10]: long stop = low of the breakout bar. If the bar
  gapped across the level (its low above the prior bar's high), use the close before the gap. If the bar
  opened at or beyond the level, or most of its range lies beyond the level, use the prior bar (the last full
  bar inside the pattern). Shorts mirror. On weekly bars the same function is Brandt's "Last Week Rule".
- ``TrailingStopRule`` / ``trailing_three_day`` (Brandt's Trailing Stop Rule) [BP ch3, ch13]: the peak bar is
  the highest high since entry; the setup bar is the first later bar to CLOSE below the peak bar's low; the
  trigger is the setup bar's low, live from the bar after the setup bar. A new peak starts over (the playbook:
  the peak day is "updated when a new high comes"), so the trigger is dropped until a new setup bar. The
  backtester uses the same class.
- ``basing_points`` (Edwards & Magee "three days away") [EM ch28]: after a top, the lowest bar of the
  pullback becomes a basing point once three later bars trade entirely above that bar's high before any
  lower low. A new high does not cancel the candidate; only a lower low replaces it. Only the 3-bar clause is
  implemented; the "new high 3% above the prior high" clause and the percentage stop table (equity price x
  beta) are not.

Parameter provenance: ``MOSTLY_OUTSIDE = 0.5`` and ``BASING_BARS = 3`` (the book's three days) - the first
is a definition chosen here (spec Q1), not a number from the books.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from .domain import Candle

Side = Literal["long", "short"]
MOSTLY_OUTSIDE = 0.5
BASING_BARS = 3
NOTE = "Stop level for study on closed bars; not an order instruction."


@dataclass(frozen=True, slots=True)
class StopLevel:
    price: float
    rule: str
    case: str
    source_bar_open_ms: int
    rule_version: str

    def as_dict(self) -> dict[str, Any]:
        return {"price": self.price, "rule": self.rule, "case": self.case,
                "source_bar_open_ms": self.source_bar_open_ms, "rule_version": self.rule_version, "note": NOTE}


def _side(side: str) -> int:
    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    return 1 if side == "long" else -1


def _index(candles: Sequence[Candle], i: int, name: str) -> None:
    if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(candles):
        raise ValueError(f"{name} is outside the candle sequence")


def last_day_rule(candles: Sequence[Candle], breakout_index: int, side: Side, level: float) -> StopLevel:
    """LDR stop for a breakout whose bar CLOSED beyond ``level`` at ``breakout_index``."""
    sign = _side(side)
    _index(candles, breakout_index, "breakout_index")
    b = candles[breakout_index]
    if sign * (b.close - level) <= 0:
        raise ValueError("the breakout bar must close beyond the level")
    if breakout_index == 0:
        raise ValueError("LDR needs the bar before the breakout")
    p = candles[breakout_index - 1]
    if (b.low > p.high) if sign == 1 else (b.high < p.low):
        return StopLevel(p.close, "LAST_DAY_RULE", "GAP_CLOSE_BEFORE_GAP", p.open_time_ms, "ldr/1")
    if sign * (b.open - level) >= 0:
        return StopLevel(p.low if sign == 1 else p.high, "LAST_DAY_RULE", "PRIOR_BAR_OPENED_AT_LEVEL",
                         p.open_time_ms, "ldr/1")
    rng = b.high - b.low
    beyond = (b.high - max(level, b.low)) if sign == 1 else (min(level, b.high) - b.low)
    if rng > 0 and beyond / rng > MOSTLY_OUTSIDE:
        return StopLevel(p.low if sign == 1 else p.high, "LAST_DAY_RULE", "PRIOR_BAR_MOSTLY_OUTSIDE",
                         p.open_time_ms, "ldr/1")
    return StopLevel(b.low if sign == 1 else b.high, "LAST_DAY_RULE", "BREAKOUT_BAR", b.open_time_ms, "ldr/1")


class TrailingStopRule:
    """Brandt's Trailing Stop Rule, fed one CLOSED bar at a time. Check ``hit(bar)`` before ``update(bar)``."""

    def __init__(self, side: Side) -> None:
        self.sign = _side(side)
        self.peak: Candle | None = None
        self.setup: Candle | None = None

    def _fav(self, c: Candle) -> float:
        return c.high if self.sign == 1 else c.low

    def _adv(self, c: Candle) -> float:
        return c.low if self.sign == 1 else c.high

    @property
    def trigger(self) -> float | None:
        return None if self.setup is None else self._adv(self.setup)

    def hit(self, c: Candle) -> bool:
        t = self.trigger
        return t is not None and self.sign * (self._adv(c) - t) < 0

    def update(self, c: Candle) -> None:
        if self.peak is None or self.sign * (self._fav(c) - self._fav(self.peak)) > 0:
            self.peak, self.setup = c, None
        elif self.setup is None and self.sign * (c.close - self._adv(self.peak)) < 0:
            self.setup = c


def trailing_three_day(candles: Sequence[Candle], entry_index: int, side: Side) -> dict[str, Any]:
    """State of Brandt's Trailing Stop Rule from ``entry_index`` through the newest bar."""
    _index(candles, entry_index, "entry_index")
    rule = TrailingStopRule(side)
    rule.update(candles[entry_index])
    hit = None
    for j in range(entry_index + 1, len(candles)):
        if rule.hit(candles[j]):
            hit = j
            break
        rule.update(candles[j])
    assert rule.peak is not None
    trigger = None if rule.setup is None else StopLevel(rule.trigger, "TRAILING_STOP_RULE", "SETUP_BAR",
                                                         rule.setup.open_time_ms, "trailing_3day/2").as_dict()
    return {"state": "TRIGGERED" if hit is not None else "ARMED" if rule.setup is not None else "TRACKING_PEAK",
            "peak_bar_open_ms": rule.peak.open_time_ms,
            "setup_bar_open_ms": None if rule.setup is None else rule.setup.open_time_ms,
            "trigger": trigger, "triggered_bar_index": hit,
            "triggered_bar_open_ms": None if hit is None else candles[hit].open_time_ms}


def basing_points(candles: Sequence[Candle], side: Side, *, start: int = 0) -> list[StopLevel]:
    """Edwards & Magee basing points from ``start`` on; each one is tighter than the one before."""
    sign = _side(side)
    _index(candles, start, "start")

    def ext(c: Candle) -> float:
        return c.high if sign == 1 else c.low

    def adverse(c: Candle) -> float:
        return c.low if sign == 1 else c.high

    out: list[StopLevel] = []
    top, cand, away = start, None, 0
    i = start + 1
    while i < len(candles):
        c = candles[i]
        if cand is not None and sign * (adverse(c) - ext(candles[cand])) > 0:  # whole bar beyond the low bar
            away += 1
            if away == BASING_BARS:
                price = adverse(candles[cand])
                if not out or sign * (price - out[-1].price) > 0:
                    out.append(StopLevel(price, "BASING_POINT", "THREE_BARS_AWAY",
                                         candles[cand].open_time_ms, "basing_point/3"))
                # Rescan from the bar after the confirmed low, so a higher reaction low inside the counting window
                # can become the next basing point instead of being skipped.
                i = cand + 1
                top, cand, away = i, None, 0
                i += 1
                continue
            if sign * (ext(c) - ext(candles[top])) > 0:
                top = i
            i += 1
            continue
        if sign * (ext(c) - ext(candles[top])) > 0:
            top = i  # a new high keeps the candidate: only a lower low replaces it
        elif cand is None or sign * (adverse(c) - adverse(candles[cand])) < 0:
            cand, away = i, 0
        i += 1
    return out
