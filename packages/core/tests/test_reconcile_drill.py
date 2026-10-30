"""covers: spec 0023 AC-22 (the drill's pure rules: only ``cli`` may send a step, no entry step while a position or
an order is open, the drill size is the larger of ``size_increment`` and ``min_quantity``) and the value sourcing of
``rest_order`` and ``open_position`` (both prices below the market, rounded down to the tick)."""

from decimal import Decimal

import pytest

from trading_core.reconcile.drill import (
    DrillRefusal,
    DrillStep,
    drill_size,
    parse_step,
    rest_price,
    step_refusal,
    stop_trigger,
)
from trading_core.recording.enums import ControlSource

D = Decimal


@pytest.mark.parametrize("step", list(DrillStep))
def test_every_step_parses_by_its_value(step):
    assert parse_step(step.value) is step


@pytest.mark.parametrize("text", [None, "", "open", "FLATTEN"])
def test_a_missing_or_unknown_step_is_none(text):
    assert parse_step(text) is None


@pytest.mark.parametrize("source", [ControlSource.DASHBOARD, ControlSource.DEPLOY])
@pytest.mark.parametrize("step", [*DrillStep, None])
def test_only_cli_may_send_a_step(source, step):
    assert step_refusal(step, source, position_open=False, order_open=False) is DrillRefusal.SOURCE_NOT_CLI


def test_an_unknown_step_from_cli_is_refused():
    assert step_refusal(None, ControlSource.CLI, position_open=False, order_open=False) is DrillRefusal.UNKNOWN_STEP


@pytest.mark.parametrize("step", [DrillStep.REST_ORDER, DrillStep.REST_ORDER_AND_DIE, DrillStep.OPEN_POSITION])
def test_an_entry_step_needs_the_drill_flat_with_no_order_open(step):
    assert step_refusal(step, ControlSource.CLI, position_open=False, order_open=False) is None
    assert step_refusal(step, ControlSource.CLI, position_open=True, order_open=False) is DrillRefusal.POSITION_OPEN
    assert step_refusal(step, ControlSource.CLI, position_open=False, order_open=True) is DrillRefusal.ORDER_OPEN


def test_flatten_always_runs():
    assert step_refusal(DrillStep.FLATTEN, ControlSource.CLI, position_open=True, order_open=True) is None


@pytest.mark.parametrize(
    ("increment", "minimum", "size"),
    [(D("0.0001"), None, D("0.0001")), (D("0.0001"), D("0.001"), D("0.001")), (D("1"), D("0.5"), D("1"))],
)
def test_the_drill_size_is_the_larger_of_the_increment_and_the_minimum(increment, minimum, size):
    assert drill_size(increment, minimum) == size


def test_a_zero_increment_is_refused():
    with pytest.raises(ValueError, match="size_increment"):
        drill_size(D(0), None)


def test_the_resting_buy_sits_the_distance_below_the_mid_rounded_down():
    assert rest_price(D("60000.5"), D(20), D(1)) == D(48000)
    assert rest_price(D("76971.43"), D(20), D("0.5")) == D("61577")


def test_the_stop_sits_the_stop_pct_below_the_fill_rounded_down():
    assert stop_trigger(D(60001), D(2), D(1)) == D(58800)
    assert stop_trigger(D(60000), D("0.5"), D("0.5")) == D(59700)


@pytest.mark.parametrize("price", [D(0), D(-1)])
def test_a_price_at_or_below_zero_is_refused(price):
    with pytest.raises(ValueError, match="above 0"):
        rest_price(price, D(20), D(1))
    with pytest.raises(ValueError, match="above 0"):
        stop_trigger(price, D(2), D(1))
