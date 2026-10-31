from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from my_ai_trading.risk import MAX_RISK_FRACTION, position_size, reward_risk, risk_fraction


def test_playbook_worked_example_long():
    # Playbook §8: capital 100,000, risk 0.8% -> 800; entry 20, stop 18 -> 400 shares.
    r = position_size(100_000, 20, 18, fraction="0.008")
    assert r["side"] == "long"
    assert r["quantity"] == Decimal(400)
    assert r["risk_amount"] == Decimal(800)
    assert r["risk_fraction_used"] == Decimal("0.008")


def test_short_side_and_point_value():
    r = position_size(100_000, 50, 52, fraction="0.01", point_value=10)
    assert r["side"] == "short"
    assert r["quantity"] == Decimal(50)  # 1000 / (2 x 10)
    assert r["notional"] == Decimal(25_000)


def test_rounds_down_to_quantity_step_never_over_budget():
    r = position_size(10_000, 100, 97, fraction="0.01", quantity_step="0.001")
    assert r["quantity"] == Decimal("33.333")
    assert r["risk_amount"] <= Decimal(100)


def test_stop_too_far_gives_zero_with_reason_and_keeps_stop():
    r = position_size(1_000, 100, 50, fraction="0.005", quantity_step=1)
    assert r["quantity"] == 0
    assert "stop is too far" in r["reason"]
    assert r["inputs"]["stop"] == Decimal(50)


@pytest.mark.parametrize("kwargs", [
    {"capital": 1000, "entry": 10, "stop": 10},                 # no stop distance
    {"capital": 0, "entry": 10, "stop": 9},
    {"capital": 1000, "entry": -1, "stop": 9},
    {"capital": 1000, "entry": 10, "stop": "nan"},
    {"capital": 1000, "entry": 10, "stop": "inf"},
    {"capital": True, "entry": 10, "stop": 9},
    {"capital": 1000, "entry": "ten", "stop": 9},
    {"capital": 1000, "entry": 10, "stop": 9, "fraction": "0.025"},  # above the 2% cap
    {"capital": 1000, "entry": 10, "stop": 9, "fraction": 0},
    {"capital": 1000, "entry": 10, "stop": 9, "quantity_step": 0},
])
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        position_size(**kwargs)


def test_risk_fraction_cap_is_two_percent():
    assert risk_fraction("0.02") == MAX_RISK_FRACTION
    with pytest.raises(ValueError):
        risk_fraction("0.0201")


def test_reward_risk():
    assert reward_risk(100, 90, 130) == Decimal(3)
    assert reward_risk(100, 110, 70) == Decimal(3)
    with pytest.raises(ValueError):
        reward_risk(100, 90, 85)  # target on the loss side of a long
    with pytest.raises(ValueError):
        reward_risk(100, 110, 120)


prices = st.decimals(min_value="0.0001", max_value="1000000", allow_nan=False, allow_infinity=False, places=4)


@given(capital=prices, entry=prices, stop=prices,
       fraction=st.decimals(min_value="0.0001", max_value="0.02", places=4),
       step=st.sampled_from([None, Decimal("0.001"), Decimal(1)]))
def test_realized_risk_never_exceeds_budget(capital, entry, stop, fraction, step):
    if entry == stop:
        return
    r = position_size(capital, entry, stop, fraction=fraction, quantity_step=step)
    assert r["quantity"] >= 0
    assert r["risk_amount"] <= r["risk_budget"] <= capital * fraction
    assert r["side"] == ("long" if stop < entry else "short")


def test_risk_fraction_floor_rejects_typos_that_would_explode_the_output():
    from my_ai_trading.risk import MIN_RISK_FRACTION
    assert risk_fraction("0.0001") == MIN_RISK_FRACTION
    with pytest.raises(ValueError):
        risk_fraction("1e-999999")
