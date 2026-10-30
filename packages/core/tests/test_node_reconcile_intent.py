"""covers: spec 0023 AC-18, the actor publishes the trader's intent per instrument in ``trader_state``: an order
going out starts it, its terminal event ends it, and a stop fix shows ``stop_replacing`` while it runs.

The actor sits in a BacktestEngine for its clock, cache, and order factory. Needs the ``node`` extra.
"""

import asyncio
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.objects import Price
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_node_placeholder import INSTRUMENT
from test_node_reconcile import BTC, FakeExchange, _actor
from test_node_reconcile_actions import (
    FakeCancel,
    FakeExecutor,
    FakeFills,
    FakeKillSwitch,
    FakeReportSink,
    FakeUntrusted,
    Journal,
    _quote,
    _view,
)
from test_node_reconcile_resume import QTY, _rejected, _setup
from test_reconcile_compare import _held, _local, _position

from trading_core.node.control import NO_RECONCILE_FIELDS
from trading_core.node.reconcile import Actions
from trading_core.recording.enums import ReconcileTrigger


def _intent(actor):
    return actor.snapshot_fields()["intent"]


def _sent(actor, order):
    """The order as the engine publishes it: the event already applied, then the actor told."""
    actor.cache.add_order(order)
    event = TestEventStubs.order_submitted(order)
    order.apply(event)
    actor.note_order_event(event)
    return order


def _then(actor, order, event):
    order.apply(event)
    actor.cache.update_order(order)
    actor.note_order_event(event)


def test_trader_state_has_no_intent_before_the_actor_reports():
    assert NO_RECONCILE_FIELDS["intent"] == {}


def test_an_entry_is_entry_submitted_until_it_fills_then_hold():
    actor, owner, _, _ = _setup()
    order = _sent(actor, owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY))
    assert _intent(actor) == {BTC: "entry_submitted"}

    _then(actor, order, TestEventStubs.order_accepted(order))
    assert _intent(actor) == {BTC: "entry_submitted"}  # accepted is not terminal

    _then(actor, order, TestEventStubs.order_filled(order, INSTRUMENT))
    assert _intent(actor) == {BTC: "hold"}


def test_a_reduce_only_exit_is_flatten_submitted_until_kraken_rejects_it():
    actor, owner, _, _ = _setup()
    order = _sent(actor, owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.SELL, QTY, reduce_only=True))
    assert _intent(actor) == {BTC: "flatten_submitted"}

    _then(actor, order, _rejected(order, "postWouldExecute"))

    assert _intent(actor) == {BTC: "hold"}


def test_a_cancelled_entry_ends_its_intent():
    actor, owner, _, _ = _setup()
    order = _sent(actor, owner.order_factory.limit(INSTRUMENT.id, NautilusOrderSide.BUY, QTY, Price(50000, 0)))
    _then(actor, order, TestEventStubs.order_accepted(order))

    _then(actor, order, TestEventStubs.order_canceled(order))

    assert _intent(actor) == {BTC: "hold"}


def test_a_protective_stop_changes_nothing():
    actor, owner, _, _ = _setup()
    stop = owner.order_factory.stop_market(
        INSTRUMENT.id, NautilusOrderSide.SELL, QTY, trigger_price=Price(59000, 0), reduce_only=True
    )

    _sent(actor, stop)

    assert _intent(actor) == {}


def test_an_order_the_cache_loaded_in_flight_shows_from_the_start():
    actor, owner, _, _ = _setup()
    order = TestExecStubs.make_submitted_order(owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY))
    actor.cache.add_order(order)
    actor.cache.update_order(order)

    async def start():
        actor.on_start()
        shown = _intent(actor)
        actor.on_stop()
        return shown

    assert asyncio.run(start()) == {BTC: "entry_submitted"}


class _SeeingExecutor(FakeExecutor):
    """Records the published intent while the replacement stop goes out."""

    actor = None
    seen = None

    async def place_stop(self, step):
        type(self).seen = _intent(type(self).actor)
        return await super().place_stop(step)


def test_a_stop_fix_shows_stop_replacing_while_it_runs_then_hold():
    journal = Journal()
    actions = Actions(
        executor=_SeeingExecutor(journal, True),
        cancel=FakeCancel(journal, ()),
        reports=FakeReportSink(journal),
        kill_switch=FakeKillSwitch(journal, False, False),
        fills=FakeFills(()),
        untrusted=FakeUntrusted(journal, True),
    )
    actor, _, _ = _actor(
        FakeExchange(_view(positions=[_position()])),
        local=_local(positions=[_held()]),
        actions=actions,
        quiet_wait_s=0,
        stale_quote_s=0.05,
    )
    _SeeingExecutor.actor = actor
    _quote(actor, 65000)

    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))

    assert journal.calls[0][:2] == ("place_stop", Decimal("0.001"))
    assert _SeeingExecutor.seen == {BTC: "stop_replacing"}
    assert _intent(actor) == {BTC: "hold"}
