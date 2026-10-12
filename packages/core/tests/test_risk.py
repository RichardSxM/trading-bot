"""covers: spec 0006 AC-4 (sizing) and AC-5 (the risk check, its reasons and their order)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_core.recording.enums import Severity
from trading_core.risk.check import (
    EntryRequest,
    InvalidRiskInput,
    RiskLimits,
    RiskVerdict,
    check_entry,
    refusal_severity,
)
from trading_core.risk.sizing import entry_quantity

LIMITS = RiskLimits(max_order_notional=Decimal(250), max_leverage=Decimal(2))


BASE = EntryRequest(
    quantity=Decimal("0.0012"),
    mid=Decimal(85000),
    equity=Decimal(1000),
    open_notional=Decimal(0),
    has_position=False,
    paused=False,
    halted=False,
    blocks=(),
)


def request(**changes):
    return replace(BASE, **changes)


def test_a_small_entry_is_approved():
    assert check_entry(request(), LIMITS) == RiskVerdict(True, None)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"halted": True}, "halted"),
        ({"paused": True}, "paused"),
        ({"blocks": ("stale_feed",)}, "blocked:stale_feed"),
        ({"has_position": True}, "position_open"),
        ({"quantity": Decimal(0)}, "zero_quantity"),
        ({"quantity": Decimal("0.003")}, "notional_cap"),  # 255 USD
        ({"equity": Decimal(50)}, "leverage_cap"),  # 102 USD on 50 USD
        ({"open_notional": Decimal(1950)}, "leverage_cap"),
    ],
)
def test_each_refusal(changes, reason):
    assert check_entry(request(**changes), LIMITS) == RiskVerdict(False, reason)


def test_refusals_come_in_the_spec_order():
    everything = request(
        halted=True,
        paused=True,
        blocks=("stale_feed",),
        has_position=True,
        quantity=Decimal(0),
        equity=Decimal(1),
    )
    order = []
    for undo in (
        {},
        {"halted": False},
        {"paused": False},
        {"blocks": ()},
        {"has_position": False},
        {"quantity": Decimal("0.003")},
    ):
        everything = EntryRequest(**(everything.__dict__ | undo))
        order.append(check_entry(everything, LIMITS).reason)
    everything = EntryRequest(**(everything.__dict__ | {"quantity": Decimal("0.0012")}))
    order.append(check_entry(everything, LIMITS).reason)

    assert order == [
        "halted",
        "paused",
        "blocked:stale_feed",
        "position_open",
        "zero_quantity",
        "notional_cap",
        "leverage_cap",
    ]


def test_the_first_block_in_sorted_order_is_named():
    verdict = check_entry(request(blocks={"stale_feed", "exit_failed", "recording_unhealthy"}), LIMITS)
    assert verdict.reason == "blocked:exit_failed"


def test_exactly_at_the_caps_is_allowed():
    at_notional = request(quantity=Decimal("0.005"), mid=Decimal(50000))  # exactly 250 USD
    assert check_entry(at_notional, LIMITS).approved
    at_leverage = request(open_notional=Decimal(1898), quantity=Decimal("0.0012"))  # 1898 + 102 = 2000
    assert check_entry(at_leverage, LIMITS).approved


@pytest.mark.parametrize("changes", [{"mid": Decimal(0)}, {"mid": Decimal(-1)}, {"equity": Decimal(0)}])
def test_unjudgeable_inputs_raise(changes):
    with pytest.raises(InvalidRiskInput):
        check_entry(request(**changes), LIMITS)


@pytest.mark.parametrize(
    ("reason", "severity"),
    [
        ("halted", Severity.INFO),
        ("paused", Severity.INFO),
        ("blocked:exit_failed", Severity.INFO),
        ("position_open", Severity.INFO),
        ("zero_quantity", Severity.WARNING),
        ("notional_cap", Severity.WARNING),
        ("leverage_cap", Severity.WARNING),
    ],
)
def test_refusal_severity(reason, severity):
    assert refusal_severity(reason) == severity


def test_entry_quantity_rounds_down_to_the_size_step():
    assert entry_quantity(Decimal(100), Decimal(85250), Decimal("0.0001")) == Decimal("0.0011")


def test_entry_quantity_is_exact_on_a_whole_step():
    assert entry_quantity(Decimal(100), Decimal(50000), Decimal("0.0001")) == Decimal("0.0020")


def test_entry_quantity_is_zero_below_one_step():
    assert entry_quantity(Decimal(1), Decimal(85250), Decimal("0.0001")) == Decimal("0.0000")


@pytest.mark.parametrize(("notional", "mid", "step"), [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-1, 1, 1)])
def test_entry_quantity_refuses_non_positive_inputs(notional, mid, step):
    with pytest.raises(ValueError, match="must be above zero"):
        entry_quantity(Decimal(notional), Decimal(mid), Decimal(step))
