"""Risk-based position sizing and reward/risk (trading playbook §8). Pure Decimal math, no exchange calls.

Conventions:
- Risk budget = capital x risk_fraction. The default fraction is 0.8% and the hard cap is 2% per trade
  (playbook §12 default; Brandt normalizes risk at ~0.7-0.8% [BP ch13]).
- Size = budget / (|entry - stop| x point_value), rounded DOWN to ``quantity_step`` so the realized risk
  never exceeds the budget. The side follows from the stop: a stop below the entry is a long.
- The stop is an input and is never moved. A stop too far away gives a smaller size, never a wider stop
  [BP ch3, ch5]. When even one step would exceed the budget, the quantity is 0 and the reason is reported.
- Output is a study figure for a human, not an order instruction.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from typing import Any

DEFAULT_RISK_FRACTION = Decimal("0.008")
MAX_RISK_FRACTION = Decimal("0.02")
MIN_RISK_FRACTION = Decimal("0.0001")  # 0.01%: smaller values are typos, and their exact output is enormous
NOTE = "Sizing arithmetic for study; not an order instruction. Gaps can fill a stop worse than its price."


def _dec(name: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not d.is_finite():
        raise ValueError(f"{name} must be finite")
    return d


def _positive(name: str, value: Any) -> Decimal:
    d = _dec(name, value)
    if d <= 0:
        raise ValueError(f"{name} must be > 0")
    return d


def risk_fraction(value: Any = DEFAULT_RISK_FRACTION) -> Decimal:
    """Validate a per-trade risk fraction: 0.01% <= r <= 2%."""
    r = _positive("risk_fraction", value)
    if r < MIN_RISK_FRACTION:
        raise ValueError(f"risk_fraction must be >= {MIN_RISK_FRACTION} (0.01%)")
    if r > MAX_RISK_FRACTION:
        raise ValueError(f"risk_fraction must be <= {MAX_RISK_FRACTION} (2% hard cap per trade)")
    return r


def side_of(entry: Decimal, stop: Decimal) -> str:
    if entry == stop:
        raise ValueError("entry and stop must differ")
    return "long" if stop < entry else "short"


def position_size(capital: Any, entry: Any, stop: Any, *, fraction: Any = DEFAULT_RISK_FRACTION,
                  point_value: Any = 1, quantity_step: Any = None) -> dict[str, Any]:
    """Largest quantity whose loss at ``stop`` stays within ``capital x fraction``."""
    cap, e, s = _positive("capital", capital), _positive("entry", entry), _positive("stop", stop)
    r, pv = risk_fraction(fraction), _positive("point_value", point_value)
    step = None if quantity_step is None else _positive("quantity_step", quantity_step)
    side = side_of(e, s)
    with localcontext() as ctx:
        ctx.rounding = ROUND_DOWN  # every inexact step rounds toward a smaller size, never past the budget
        budget = cap * r
        per_unit = abs(e - s) * pv
        raw = budget / per_unit
        qty = (raw / step).to_integral_value() * step if step else raw
        risk = qty * per_unit
    out: dict[str, Any] = {
        "side": side, "quantity": qty, "risk_budget": budget, "risk_per_unit": per_unit,
        "risk_amount": risk, "risk_fraction_used": risk / cap, "notional": qty * e * pv,
        "stop_distance_fraction": abs(e - s) / e, "inputs": {"capital": cap, "entry": e, "stop": s,
                                                             "risk_fraction": r, "point_value": pv,
                                                             "quantity_step": step},
        "note": NOTE,
    }
    if qty == 0:
        out["reason"] = ("one quantity step would risk more than the budget; the stop is too far for this "
                         "capital and risk fraction (do not widen the budget or move the stop to fit)")
    return out


def reward_risk(entry: Any, stop: Any, target: Any) -> Decimal:
    """|target - entry| / |entry - stop|; the target must lie on the profit side of the entry."""
    e, s, t = _positive("entry", entry), _positive("stop", stop), _positive("target", target)
    side = side_of(e, s)
    if (side == "long" and t <= e) or (side == "short" and t >= e):
        raise ValueError("target must be above the entry for a long and below it for a short")
    return abs(t - e) / abs(e - s)
