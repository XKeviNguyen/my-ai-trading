"""Typed custom-strategy rule DSL (PRD F05). Rules are data: validated, never executed as code.

Every metric names its own timeframe, so ``SMA(30, 1w)`` and ``SMA(30, 1h)`` are distinct and are
never converted into each other. All metrics read CLOSED bars as of the evaluation instant; a
higher-timeframe metric uses the last closed bar of that timeframe, never a partial one.
"""
from __future__ import annotations

import hashlib
import json
from math import isfinite
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import CandleSeries, Timeframe
from .engine import trend_state, utc_iso
from .indicators import (adx, atr_wilder, bollinger, ema, macd, prior_high, prior_low, rsi_wilder, sma,
                         volume_ratio)

RULE_SCHEMA_VERSION = 1
MAX_NODES, MAX_DEPTH = 32, 6
TimeframeCode = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"]
PERIOD_METRICS = {"sma", "ema", "volume_ratio", "prior_high", "prior_low", "atr_wilder", "rsi_wilder", "adx",
                  "bollinger_percent_b"}  # macd_line and macd_histogram use the standard 12/26/9 periods


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricRef(_Strict):
    name: Literal["close", "open", "high", "low", "volume", "sma", "ema", "volume_ratio", "prior_high",
                  "prior_low", "atr_wilder", "rsi_wilder", "adx", "bollinger_percent_b", "macd_line",
                  "macd_histogram"]
    timeframe: TimeframeCode
    period: int | None = Field(default=None, ge=1, le=500)
    bar: Literal["last_closed", "previous_closed"] = "last_closed"

    @model_validator(mode="after")
    def _period_rules(self) -> "MetricRef":
        if self.name in PERIOD_METRICS and self.period is None:
            raise ValueError(f"{self.name} requires a period")
        if self.name not in PERIOD_METRICS and self.period is not None:
            raise ValueError(f"{self.name} does not take a period")
        return self

    def label(self) -> str:
        p = f"({self.period})" if self.period else ""
        prev = " [previous closed bar]" if self.bar == "previous_closed" else ""
        return f"{self.name.upper()}{p} on {self.timeframe}{prev}"


class Compare(_Strict):
    kind: Literal["compare"] = "compare"
    left: MetricRef
    op: Literal[">", ">=", "<", "<=", "crosses_above", "crosses_below"]
    right: MetricRef | float

    @field_validator("right")
    @classmethod
    def _finite(cls, v: Any) -> Any:
        if isinstance(v, bool) or (isinstance(v, float) and not isfinite(v)):
            raise ValueError("comparison value must be a finite number or a metric reference")
        return v

    @model_validator(mode="after")
    def _cross_needs_last_bar(self) -> "Compare":
        if self.op.startswith("crosses") and self.left.bar != "last_closed":
            raise ValueError("crosses_* compares previous and last closed bars itself")
        return self


class Trend(_Strict):
    kind: Literal["trend"] = "trend"
    timeframe: TimeframeCode
    state: Literal["UP", "DOWN", "MIXED"]


class AllOf(_Strict):
    kind: Literal["all"] = "all"
    items: list["Condition"] = Field(min_length=1, max_length=16)


class AnyOf(_Strict):
    kind: Literal["any"] = "any"
    items: list["Condition"] = Field(min_length=1, max_length=16)


class Not(_Strict):
    kind: Literal["not"] = "not"
    item: "Condition"


Condition = Annotated[Union[Compare, Trend, AllOf, AnyOf, Not], Field(discriminator="kind")]
AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


class StrategyRule(_Strict):
    schema_version: Literal[1] = RULE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .,:()+/\-]+$")
    description: str = Field(default="", max_length=500)
    trigger_timeframe: TimeframeCode
    condition: Condition
    on_match: Literal["ALERT"] = "ALERT"
    otherwise: Literal["WAIT", "NO_ALERT"] = "WAIT"
    cooldown_bars: int = Field(default=1, ge=0, le=1000,
                               description="minimum bars between alerts; 0 or 1 = may alert on every bar")

    @model_validator(mode="after")
    def _bounded(self) -> "StrategyRule":
        nodes, depth = _size(self.condition)
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError(f"rule too complex (max {MAX_NODES} nodes, depth {MAX_DEPTH})")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def rule_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    def timeframes(self) -> set[str]:
        out: set[str] = {self.trigger_timeframe}
        _walk(self.condition, lambda n: out.update(_node_tfs(n)))
        return out


def _size(node: Any, depth: int = 1) -> tuple[int, int]:
    if isinstance(node, (AllOf, AnyOf)):
        sizes = [_size(i, depth + 1) for i in node.items]
        return 1 + sum(s[0] for s in sizes), max(s[1] for s in sizes)
    if isinstance(node, Not):
        n, d = _size(node.item, depth + 1)
        return n + 1, d
    return 1, depth


def _walk(node: Any, fn: Callable[[Any], None]) -> None:
    fn(node)
    if isinstance(node, (AllOf, AnyOf)):
        for i in node.items:
            _walk(i, fn)
    elif isinstance(node, Not):
        _walk(node.item, fn)


def _node_tfs(node: Any) -> set[str]:
    if isinstance(node, Compare):
        tfs = {node.left.timeframe}
        if isinstance(node.right, MetricRef):
            tfs.add(node.right.timeframe)
        return tfs
    if isinstance(node, Trend):
        return {node.timeframe}
    return set()


# ---------- human-readable preview ----------

OP_TEXT = {">": "is above", ">=": "is at or above", "<": "is below", "<=": "is at or below",
           "crosses_above": "crosses above", "crosses_below": "crosses below"}
TREND_DEFINITION = "close > SMA50 > SMA200 with SMA50 rising over 10 bars (UP); mirror for DOWN; else MIXED"


def describe(node: Any) -> str:
    if isinstance(node, Compare):
        right = node.right.label() if isinstance(node.right, MetricRef) else f"{node.right:g}"
        return f"{node.left.label()} {OP_TEXT[node.op]} {right}"
    if isinstance(node, Trend):
        return f"trend on {node.timeframe} is {node.state} (definition: {TREND_DEFINITION})"
    if isinstance(node, Not):
        return f"NOT ({describe(node.item)})"
    joiner = " AND " if isinstance(node, AllOf) else " OR "
    return "(" + joiner.join(describe(i) for i in node.items) + ")"


def preview(rule: StrategyRule) -> dict[str, Any]:
    return {
        "name": rule.name, "rule_hash": rule.rule_hash(), "schema_version": rule.schema_version,
        "when": f"on each CLOSED {rule.trigger_timeframe} bar",
        "condition": describe(rule.condition),
        "then": f"{rule.on_match}; otherwise {rule.otherwise}",
        "cooldown_bars": rule.cooldown_bars,
        "timeframes_used": sorted(rule.timeframes(), key=lambda t: Timeframe(t).duration_ms),
        "bar_finality": "All values come from closed bars; higher-timeframe values use that timeframe's last "
                        "closed bar, never a forming one.",
        "attribution": "Custom strategy — no trader attribution.",
    }


# ---------- evaluation ----------

SeriesGetter = Callable[[str], CandleSeries]


class _Ctx:
    def __init__(self, get_series: SeriesGetter, as_of_ms: int):
        self.get, self.as_of = get_series, as_of_ms
        self.tails: dict[str, tuple] = {}
        self.bar_times: dict[str, str | None] = {}
        self.leaves: list[dict[str, Any]] = []

    def tail(self, tf: str) -> tuple:
        if tf not in self.tails:
            s = self.get(tf).up_to(self.as_of)
            if s.is_stale(self.as_of):
                self.tails[tf] = ()
                self.bar_times[tf] = "STALE"
            else:
                t = s.contiguous_tail()
                self.tails[tf] = t
                self.bar_times[tf] = utc_iso(t[-1].close_time_ms + 1) if t else None
        return self.tails[tf]


def metric_value(m: MetricRef, tail: tuple) -> float | None:
    bars = tail[:-1] if m.bar == "previous_closed" else tail
    if not bars:
        return None
    closes = [c.close for c in bars]
    n = m.period
    match m.name:
        case "close" | "open" | "high" | "low" | "volume":
            return getattr(bars[-1], m.name)
        case "sma":
            return sma(closes, n)
        case "ema":
            return ema(closes, n)
        case "volume_ratio":
            return volume_ratio(bars, n)
        case "prior_high":
            return prior_high(bars, n)
        case "prior_low":
            return prior_low(bars, n)
        case "atr_wilder":
            return atr_wilder(bars, n)
        case "rsi_wilder":
            return rsi_wilder(closes, n)
        case "adx":
            d = adx(bars, n)
            return d and d["adx"]
        case "bollinger_percent_b":
            b = bollinger(closes, n)
            return b and b["percent_b"]
        case "macd_line":
            ml = macd(closes)
            return ml and ml["macd"]
        case "macd_histogram":
            mh = macd(closes)
            return mh and mh["histogram"]
    return None


def _value(ref: MetricRef | float, ctx: _Ctx, *, previous: bool = False) -> float | None:
    if not isinstance(ref, MetricRef):
        return float(ref)
    if previous:
        ref = ref.model_copy(update={"bar": "previous_closed"})
    return metric_value(ref, ctx.tail(ref.timeframe))


def _eval(node: Any, ctx: _Ctx) -> bool | None:
    if isinstance(node, AllOf):
        outcomes = [_eval(i, ctx) for i in node.items]
        return False if False in outcomes else None if None in outcomes else True
    if isinstance(node, AnyOf):
        outcomes = [_eval(i, ctx) for i in node.items]
        return True if True in outcomes else None if None in outcomes else False
    if isinstance(node, Not):
        r = _eval(node.item, ctx)
        return None if r is None else not r
    if isinstance(node, Trend):
        tail = ctx.tail(node.timeframe)
        closes = [c.close for c in tail]
        state = trend_state(closes, {50: sma(closes, 50), 200: sma(closes, 200)})["state"] if tail else "UNKNOWN"
        result = None if state == "UNKNOWN" else state == node.state
        ctx.leaves.append({"condition": describe(node), "observed": state, "result": result})
        return result
    left, right = _value(node.left, ctx), _value(node.right, ctx)
    result: bool | None
    if left is None or right is None:
        result = None
    elif node.op in (">", ">=", "<", "<="):
        result = {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[node.op]
    else:
        pl, pr = _value(node.left, ctx, previous=True), _value(node.right, ctx, previous=True)
        if pl is None or pr is None:
            result = None
        elif node.op == "crosses_above":
            result = pl <= pr and left > right
        else:
            result = pl >= pr and left < right
    ctx.leaves.append({"condition": describe(node), "left": left, "right": right, "result": result})
    return result


def evaluate(rule: StrategyRule, get_series: SeriesGetter, *, as_of_ms: int) -> dict[str, Any]:
    ctx = _Ctx(get_series, as_of_ms)
    r = _eval(rule.condition, ctx)
    result = "MATCH" if r is True else "NO_MATCH" if r is False else "INSUFFICIENT_DATA"
    action = rule.on_match if r is True else rule.otherwise if r is False else "INSUFFICIENT_DATA"
    return {"rule_hash": rule.rule_hash(), "result": result, "action": action,
            "as_of_utc": utc_iso(as_of_ms), "bar_close_times": ctx.bar_times, "leaves": ctx.leaves,
            "note": "Rule check on closed bars; not a trade instruction."}
