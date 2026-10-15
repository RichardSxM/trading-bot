"""covers: spec 0010 AC-1 (the record): a kill switch order is recorded with ``origin = kill_switch``
and no decision; a strategy order still needs its decision tag."""

import uuid
from decimal import Decimal

import pytest

from trading_core.recording.enums import Env, OrderOrigin
from trading_core.recording.mappers import MissingDecisionTagError, OrderFacts, order_origin, order_row

DECISION = uuid.uuid4()


def _facts(tags):
    return OrderFacts(
        client_order_id="O-1",
        venue_order_id=None,
        instrument_id="PF_XBTUSD.KRAKEN",
        side="SELL",
        order_type="MARKET",
        time_in_force="GTC",
        quantity=Decimal("0.0011"),
        price=None,
        trigger_price=None,
        reduce_only=True,
        post_only=False,
        status="SUBMITTED",
        filled_qty=Decimal(0),
        avg_px=None,
        tags=tags,
    )


def _row(tags):
    return order_row(_facts(tags), env=Env.DEMO, run_id=uuid.uuid4(), last_event_ns=1)


def test_a_kill_switch_order_has_its_origin_and_no_decision():
    row = _row(["origin:kill_switch", f"halt:{uuid.uuid4()}"])

    assert (row.origin, row.decision_id) == (OrderOrigin.KILL_SWITCH, None)


def test_a_strategy_order_keeps_its_decision():
    row = _row([f"decision:{DECISION}"])

    assert (row.origin, row.decision_id) == (OrderOrigin.STRATEGY, DECISION)


def test_a_strategy_order_without_a_decision_still_fails_closed():
    with pytest.raises(MissingDecisionTagError):
        _row([])


@pytest.mark.parametrize("value", ["strategy", "external", "reconciliation", "bogus"])
def test_only_the_kill_switch_origin_may_be_tagged(value):
    with pytest.raises(MissingDecisionTagError):
        order_origin([f"origin:{value}", f"decision:{DECISION}"])
