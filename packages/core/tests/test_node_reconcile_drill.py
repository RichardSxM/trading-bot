"""covers: spec 0023 AC-22 (``reconcile_drill`` sends orders only on a ``drill_step`` from ``cli``, refuses every other
entry, never sends more than the drill size, never opens a second position) and the drill steps' value sourcing
(``rest_order`` at the mid x (1 - ``DRILL_REST_DISTANCE_PCT``) post only; ``open_position`` a market buy whose
stop sits at the fill price x (1 - ``DRILL_STOP_PCT``); ``rest_order_and_die`` exits right after the submit).

The order flow runs in a BacktestEngine: scripted calls at set minutes stand in for the steps' submits, so the
fills, the stop, the flatten, and the cancels are real. The steps themselves are async and are run on their own
loop with ``submit_order`` replaced by a recorder. Needs the ``node`` extra.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from test_node_placeholder import INSTRUMENT, INSTRUMENT_ID, START, Prices, _ns, _quotes

from trading_core.control.halt import HaltReason, HaltSource, HaltState
from trading_core.control.messages import drill_step_command
from trading_core.node.reconcile_drill import ReconcileDrill, ReconcileDrillConfig
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import ControlOutcome, ControlSource
from trading_core.strategy.blocks import BlockReason, EntryBlocks

MINUTE_NS = 60_000_000_000
MID = 60_000  # bid 59 999, ask 60 001
NOW = datetime(2026, 10, 30, tzinfo=UTC)


def _min(minutes):
    return _ns(START) + int(minutes * MINUTE_NS)


def _step(step, source=ControlSource.CLI):
    msg = drill_step_command(NOW, 30, step)
    return dataclasses.replace(msg, source=source)


class _Scripted(ReconcileDrill):
    """Runs ``script``'s ``(minute, call)`` pairs on the engine's clock."""

    script: tuple[tuple[float, Callable[[ReconcileDrill], object]], ...] = ()

    def on_start(self):
        super().on_start()
        for index, (minute, call) in enumerate(type(self).script):
            self.clock.set_time_alert_ns(f"test_{index}", _min(minute), lambda _event, call=call: call(self))


def _buy_market(drill):
    instrument = drill.cache.instrument(INSTRUMENT_ID)
    drill.submit_order(drill.order_factory.market(INSTRUMENT_ID, NautilusOrderSide.BUY, drill._size(instrument)))


def _rest_far_below(drill):
    order = drill.order_factory.limit(
        INSTRUMENT_ID,
        NautilusOrderSide.BUY,
        Quantity.from_str("0.0001"),
        Price.from_str("30000"),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    drill.submit_order(order)


def _engine(script=(), minutes=8, run=True, gate=None, blocks=None, **config):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(1000, USD)], base_currency=USD
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_quotes(Prices(minutes, lambda t: MID)))
    died = []
    cls = type("Drill", (_Scripted,), {"script": tuple(script)})
    drill = cls(
        ReconcileDrillConfig(instrument_id=str(INSTRUMENT_ID), **config),
        gate or PauseGate(),
        blocks or EntryBlocks(),
        die=lambda: died.append(True),
    )
    engine.add_strategy(drill)
    if run:
        engine.run()
        # The engine's stop paused the gate in memory, as a real stop does; the steps below run as if live.
        if gate is None:
            drill._gate.unpause()
    return drill, died


def _recording(drill):
    sent = []
    drill.submit_order = lambda order, position_id=None, *args, **kwargs: sent.append(order)
    return sent


def _statuses(drill):
    found: dict[object, list[object]] = {}
    for order in sorted(drill.cache.orders(strategy_id=drill.id), key=lambda o: o.ts_init):
        found.setdefault(order.order_type, []).append(order.status)
    return found


def _orders(drill, order_type):
    return [o for o in drill.cache.orders(strategy_id=drill.id) if o.order_type == order_type]


# The order flow ------------------------------------------------------------------------------


def test_a_filled_buy_gets_its_stop_at_the_fill_price_less_the_stop_pct_and_flatten_closes_then_cancels_it():
    drill, _ = _engine([(1, _buy_market), (3, lambda d: d._flatten_step())])

    (entry,) = _orders(drill, OrderType.MARKET)[:1]
    (stop,) = _orders(drill, OrderType.STOP_MARKET)
    # Filled at the ask, 60 001; 60 001 x 0.98 = 58 800.98, rounded down to the 1 USD tick.
    assert stop.trigger_price == Price.from_str("58800")
    assert (stop.side, stop.quantity, stop.is_reduce_only) == (NautilusOrderSide.SELL, entry.quantity, True)
    assert f"parent:{entry.client_order_id}" in stop.tags
    assert stop.status == OrderStatus.CANCELED  # only after the close filled
    assert drill.cache.positions_open() == []
    close = _orders(drill, OrderType.MARKET)[1]
    assert close.is_reduce_only
    assert close.status == OrderStatus.FILLED


def test_the_kill_switch_flatten_closes_with_its_tags():
    halt_id = uuid.uuid4()
    drill, _ = _engine([(1, _buy_market), (3, lambda d: d.flatten(halt_id))])

    close = _orders(drill, OrderType.MARKET)[1]
    assert {"origin:kill_switch", f"halt:{halt_id}"} <= set(close.tags)
    assert drill.cache.positions_open() == []
    (stop,) = _orders(drill, OrderType.STOP_MARKET)
    assert stop.status == OrderStatus.CANCELED


def test_a_refused_stop_closes_the_position():
    def refuse(drill):
        (stop_id,) = drill._stops
        drill.on_order_rejected(TestEventStubs.order_rejected(drill.cache.order(stop_id)))

    drill, _ = _engine([(1, _buy_market), (3, refuse)])

    assert drill.cache.positions_open() == []
    assert _orders(drill, OrderType.MARKET)[1].is_reduce_only


def test_resume_keeps_the_stop_and_cancels_a_resting_entry():
    def resume(drill):
        (position,) = drill.cache.positions_open()
        drill.resume_position(position, None)

    seen = []
    drill, _ = _engine([(1, _buy_market), (2, _rest_far_below), (3, resume), (4, lambda d: seen.append(_statuses(d)))])

    # Read before the engine's own stop, which cancels every entry anyway.
    assert seen == [
        {
            OrderType.MARKET: [OrderStatus.FILLED],
            OrderType.LIMIT: [OrderStatus.CANCELED],
            OrderType.STOP_MARKET: [OrderStatus.ACCEPTED],
        }
    ]
    assert len(drill.cache.positions_open()) == 1


def test_a_resting_entry_from_before_the_restart_is_cancelled_when_entries_first_resume():
    def resume(drill):
        drill.signals_after(drill.clock.timestamp_ns())

    seen = []
    _engine(
        [(1, _rest_far_below), (2, resume), (3, _rest_far_below), (4, resume), (5, lambda d: seen.append(_statuses(d)))]
    )

    assert seen == [{OrderType.LIMIT: [OrderStatus.CANCELED, OrderStatus.ACCEPTED]}]


# The steps -----------------------------------------------------------------------------------


def test_rest_order_rests_a_post_only_buy_of_the_drill_size_far_below_the_mid():
    drill, _ = _engine()
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("rest_order")))

    (order,) = sent
    assert outcome.outcome is ControlOutcome.EXECUTED
    # 60 000 x 0.8 = 48 000.
    assert (order.order_type, order.side, order.price, order.quantity) == (
        OrderType.LIMIT,
        NautilusOrderSide.BUY,
        Price.from_str("48000"),
        Quantity.from_str("0.0001"),
    )
    assert order.is_post_only
    assert not order.is_reduce_only
    assert outcome.details == {"client_order_id": str(order.client_order_id), "quantity": "0.0001", "price": "48000"}


def test_the_rest_distance_comes_from_the_setting():
    drill, _ = _engine(rest_distance_pct=Decimal(50))
    sent = _recording(drill)

    asyncio.run(drill.run_step(_step("rest_order")))

    assert sent[0].price == Price.from_str("30000")


def test_rest_order_and_die_exits_right_after_the_submit():
    drill, died = _engine(die_after_submit_s=0)
    sent = _recording(drill)

    async def run():
        outcome = await drill.run_step(_step("rest_order_and_die"))
        assert died == []  # not before Nautilus hands it to Kraken
        drill.on_order_submitted(TestEventStubs.order_submitted(sent[0]))
        await asyncio.sleep(0.01)
        return outcome

    assert asyncio.run(run()).outcome is ControlOutcome.EXECUTED
    assert died == [True]


def test_a_plain_rest_order_never_exits():
    drill, died = _engine(die_after_submit_s=0)
    sent = _recording(drill)

    async def run():
        await drill.run_step(_step("rest_order"))
        drill.on_order_submitted(TestEventStubs.order_submitted(sent[0]))
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert died == []


def test_open_position_buys_the_drill_size_at_market_and_answers_once_the_stop_is_accepted():
    drill, _ = _engine()
    sent = _recording(drill)

    async def run():
        task = asyncio.create_task(drill.run_step(_step("open_position")))
        await asyncio.sleep(0)
        drill._confirm(sent[0].client_order_id, True)
        return await task

    outcome = asyncio.run(run())
    (order,) = sent
    assert (order.order_type, order.side, order.quantity) == (
        OrderType.MARKET,
        NautilusOrderSide.BUY,
        Quantity.from_str("0.0001"),
    )
    assert outcome.outcome is ControlOutcome.EXECUTED


def test_open_position_with_no_confirmation_in_time_fails():
    drill, _ = _engine(confirm_timeout_s=0.01)
    _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("open_position")))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "not_confirmed")


def test_flatten_while_flat_sends_nothing():
    drill, _ = _engine()
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("flatten")))

    assert (outcome.outcome, sent) == (ControlOutcome.EXECUTED, [])


# Refusals ------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", [ControlSource.DASHBOARD, ControlSource.DEPLOY])
def test_a_step_not_from_cli_is_refused(source):
    drill, _ = _engine(run=False)
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("rest_order", source)))

    assert (outcome.outcome, outcome.reason, sent) == (ControlOutcome.FAILED, "source_not_cli", [])


@pytest.mark.parametrize("step", [None, "buy_lots", "open_position "])
def test_an_unknown_step_is_refused(step):
    drill, _ = _engine(run=False)
    _recording(drill)

    outcome = asyncio.run(drill.run_step(dataclasses.replace(_step("flatten"), step=step)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "unknown_step")


def test_without_a_quote_rest_order_fails():
    drill, _ = _engine(run=False)
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("rest_order")))

    assert (outcome.outcome, outcome.reason, sent) == (ControlOutcome.FAILED, "no_quote", [])


@pytest.mark.parametrize("step", ["rest_order", "rest_order_and_die", "open_position"])
def test_no_entry_step_while_a_position_is_open(step):
    drill, _ = _engine([(1, _buy_market)])
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step(step)))

    assert (outcome.outcome, outcome.reason, sent) == (ControlOutcome.FAILED, "position_open", [])


def test_no_entry_step_while_an_entry_order_rests():
    outcomes = []

    def step(drill):
        # While it rests: the engine's own stop cancels it later. A refused step awaits nothing.
        outcomes.append(asyncio.new_event_loop().run_until_complete(drill.run_step(_step("open_position"))))

    drill, _ = _engine([(1, _rest_far_below), (2, step)])

    (outcome,) = outcomes
    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "order_open")
    assert _orders(drill, OrderType.MARKET) == []


@pytest.mark.parametrize(
    "reason", [BlockReason.RECONCILING, BlockReason.ORDER_UNRESOLVED, BlockReason.EXCHANGE_UNKNOWN]
)
def test_no_entry_step_while_entries_are_blocked(reason):
    blocks = EntryBlocks()
    blocks.add(reason)
    drill, _ = _engine(blocks=blocks)
    sent = _recording(drill)

    outcome = asyncio.run(drill.run_step(_step("rest_order")))

    assert (outcome.outcome, outcome.reason, sent) == (ControlOutcome.FAILED, "blocked", [])


def test_no_entry_step_while_halted_but_flatten_still_runs():
    gate = PauseGate()
    gate.halt(HaltState(HaltReason.MANUAL_STOP, HaltSource.CLI, NOW, uuid.uuid4()))
    drill, _ = _engine(gate=gate)
    _recording(drill)

    entry = asyncio.run(drill.run_step(_step("open_position")))
    flatten = asyncio.run(drill.run_step(_step("flatten")))

    assert (entry.outcome, entry.reason) == (ControlOutcome.FAILED, "blocked")
    assert flatten.outcome is ControlOutcome.EXECUTED
