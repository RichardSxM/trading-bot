"""covers: spec 0023 AC-14 (the actor tracks entries with no answer from Kraken and blocks entries with
``order_unresolved``), AC-15 (a clean check that lets entries resume tells each strategy its finish time) and AC-16
(the first clean check hands each open position to its owner, with its resting stop; a resume that raises
flattens and halts), plus ``KfsStrategy``'s own side of AC-15 and AC-16.

The actor and one recording strategy sit in a BacktestEngine for their clock, cache, and order factory; the
actor is driven directly inside ``asyncio.run``. Needs the ``node`` extra.
"""

import asyncio
import uuid

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import PositionId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_node_placeholder import INSTRUMENT
from test_node_reconcile import BTC, EMPTY, FOREIGN, FakeExchange
from test_node_reconcile_actions import (
    FakeCancel,
    FakeExecutor,
    FakeFills,
    FakeKillSwitch,
    FakeReportSink,
    FakeUntrusted,
    Journal,
)
from test_reconcile_compare import _local

from trading_core.control.halt import HaltReason
from trading_core.node.reconcile import Actions, ReconcileActor, resting_stop
from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate, decision_of
from trading_core.reconcile.views import ExchangeView
from trading_core.recording.enums import Env, ReconcileOutcome, ReconcileTrigger
from trading_core.recording.mappers import DECISION_TAG
from trading_core.strategy.blocks import BlockReason, EntryBlocks

QTY = Quantity.from_str("0.0010")
FOREIGN_VIEW = ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1)


class _Owner(KfsStrategy):
    """Records what the actor tells it; ``fail`` makes its resume raise."""

    fail = False

    def __init__(self, config, gate):
        super().__init__(config, gate)
        self.after = []
        self.resumed = []

    def signals_after(self, finished_ns):
        super().signals_after(finished_ns)
        self.after.append(finished_ns)

    def resume_position(self, position, stop_order):
        if type(self).fail:
            raise RuntimeError("cannot resume")
        self.resumed.append((position.id, None if stop_order is None else stop_order.client_order_id))


class _FailingOwner(_Owner):
    fail = True


def _setup(*views, acting=False, fail=False, quiet_wait_s=0.0):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(1000, USD)])
    engine.add_instrument(INSTRUMENT)
    blocks = EntryBlocks()
    blocks.add(BlockReason.RECONCILING)  # on_start's job
    owner = (_FailingOwner if fail else _Owner)(KfsStrategyConfig(instrument_id=BTC), PauseGate())
    journal = Journal()
    actions = None
    if acting:
        actions = Actions(
            executor=FakeExecutor(journal, True),
            cancel=FakeCancel(journal, ()),
            reports=FakeReportSink(journal),
            kill_switch=FakeKillSwitch(journal),
            fills=FakeFills(()),
            untrusted=FakeUntrusted(journal, True),
        )
    actor = ReconcileActor(
        env=Env.DEMO,
        exchange=FakeExchange(*(views or (EMPTY,))),
        blocks=blocks,
        local=lambda _cache: _local(),
        strategies=lambda: [owner],
        actions=actions,
        quiet_wait_s=quiet_wait_s,
    )
    engine.add_actor(actor)
    engine.add_strategy(owner)
    return actor, owner, blocks, journal


def _open_position(actor, owner, decision=None):
    tags = None if decision is None else [f"{DECISION_TAG}{decision}"]
    entry = owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY, tags=tags)
    fill = TestEventStubs.order_filled(
        entry, INSTRUMENT, strategy_id=owner.id, position_id=PositionId("P-1"), last_px=Price(60000, 0)
    )
    entry.apply(TestEventStubs.order_submitted(entry))
    entry.apply(TestEventStubs.order_accepted(entry))
    entry.apply(fill)
    actor.cache.add_order(entry)
    position = Position(INSTRUMENT, fill)
    actor.cache.add_position(position, OmsType.NETTING)
    return position


def _resting(actor, owner, side=NautilusOrderSide.SELL, reduce_only=True):
    stop = owner.order_factory.stop_market(
        INSTRUMENT.id, side, QTY, trigger_price=Price(59000, 0), reduce_only=reduce_only
    )
    stop = TestExecStubs.make_accepted_order(stop)
    actor.cache.add_order(stop)
    actor.cache.update_order(stop)  # indexes it as open
    return stop


def _rejected(order, reason):
    return OrderRejected(
        trader_id=order.trader_id,
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        account_id=TestEventStubs.order_accepted(order).account_id,
        reason=reason,
        event_id=UUID4(),
        ts_event=0,
        ts_init=0,
    )


def _check(actor, trigger=ReconcileTrigger.START):
    return asyncio.run(actor.check(trigger))


# AC-14: unresolved entries ----------------------------------------------------------------------


def _entry(actor, owner, reduce_only=False):
    order = owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY, reduce_only=reduce_only)
    actor.cache.add_order(order)
    return order


def test_an_entry_with_no_answer_blocks_entries_until_kraken_acknowledges_it():
    actor, owner, blocks, _ = _setup()
    order = _entry(actor, owner)

    actor.note_order_event(TestEventStubs.order_submitted(order))
    assert BlockReason.ORDER_UNRESOLVED in blocks.active()

    actor.note_order_event(TestEventStubs.order_accepted(order))
    assert BlockReason.ORDER_UNRESOLVED not in blocks.active()


def test_a_fill_or_a_rejection_from_kraken_also_answers_it():
    actor, owner, blocks, _ = _setup()
    filled, rejected = _entry(actor, owner), _entry(actor, owner)
    for order in (filled, rejected):
        actor.note_order_event(TestEventStubs.order_submitted(order))

    actor.note_order_event(TestEventStubs.order_filled(filled, INSTRUMENT))
    actor.note_order_event(_rejected(rejected, "postWouldExecute"))

    assert BlockReason.ORDER_UNRESOLVED not in blocks.active()


def test_a_reduce_only_order_never_blocks_entries():
    actor, owner, blocks, _ = _setup()
    exit_ = _entry(actor, owner, reduce_only=True)

    actor.note_order_event(TestEventStubs.order_submitted(exit_))

    assert BlockReason.ORDER_UNRESOLVED not in blocks.active()


def test_an_entry_the_in_flight_check_rejected_stays_blocked_until_the_next_clean_check():
    actor, owner, blocks, _ = _setup()
    order = _entry(actor, owner)
    actor.note_order_event(TestEventStubs.order_submitted(order))

    actor.note_order_event(_rejected(order, "UNKNOWN"))
    assert BlockReason.ORDER_UNRESOLVED in blocks.active()

    assert _check(actor, ReconcileTrigger.PERIODIC).outcome is ReconcileOutcome.CLEAN
    assert BlockReason.ORDER_UNRESOLVED not in blocks.active()


def test_entries_the_cache_loaded_in_flight_block_entries_from_the_start():
    actor, owner, blocks, _ = _setup(EMPTY)
    order = TestExecStubs.make_submitted_order(_entry(actor, owner))
    actor.cache.update_order(order)  # indexes it in flight

    async def start():
        actor.on_start()
        active = blocks.active()
        actor.on_stop()
        return active

    assert BlockReason.ORDER_UNRESOLVED in asyncio.run(start())


def test_a_mismatch_check_leaves_an_unresolved_entry_blocked():
    actor, owner, blocks, _ = _setup(FOREIGN_VIEW)
    order = _entry(actor, owner)
    actor.note_order_event(TestEventStubs.order_submitted(order))
    actor.note_order_event(_rejected(order, "UNKNOWN"))

    assert _check(actor).outcome is ReconcileOutcome.MISMATCH
    assert BlockReason.ORDER_UNRESOLVED in blocks.active()


# AC-15: the fresh signal time -------------------------------------------------------------------


def test_a_clean_start_check_tells_each_strategy_when_it_finished():
    actor, owner, _, _ = _setup()

    _check(actor)

    assert owner.after == [actor.snapshot_fields()["last_clean_check_ns"]]


def test_a_clean_check_that_lifts_no_reconciling_block_tells_nothing():
    actor, owner, blocks, _ = _setup()
    _check(actor)
    assert BlockReason.RECONCILING not in blocks.active()

    _check(actor, ReconcileTrigger.PERIODIC)

    assert len(owner.after) == 1


def test_a_clean_reconnect_check_moves_the_time_again():
    actor, owner, blocks, _ = _setup()
    _check(actor)
    blocks.add(BlockReason.RECONCILING)  # the stale feed went on

    _check(actor, ReconcileTrigger.RECONNECT)

    assert len(owner.after) == 2
    assert owner.after[1] >= owner.after[0]


def test_the_strategy_ignores_a_signal_older_than_the_check():
    _, owner, _, _ = _setup()
    assert owner.fresh_signal(5)  # before any check (a replay): always fresh

    owner.signals_after(100)

    assert not owner.fresh_signal(99)
    assert owner.fresh_signal(100)
    assert owner.fresh_signal(101)


# AC-16: resume ----------------------------------------------------------------------------------


def test_the_first_clean_check_hands_each_position_to_its_owner_with_its_stop():
    actor, owner, _, _ = _setup()
    position = _open_position(actor, owner)
    _resting(actor, owner, side=NautilusOrderSide.BUY)  # not a stop closing a long
    stop = _resting(actor, owner)

    _check(actor)

    assert owner.resumed == [(position.id, stop.client_order_id)]


def test_resume_runs_once_never_on_later_checks():
    actor, owner, blocks, _ = _setup()
    _open_position(actor, owner)

    _check(actor)
    blocks.add(BlockReason.RECONCILING)
    _check(actor, ReconcileTrigger.RECONNECT)
    _check(actor, ReconcileTrigger.PERIODIC)

    assert len(owner.resumed) == 1


def test_a_position_without_a_stop_is_handed_over_with_none():
    actor, owner, _, _ = _setup()
    position = _open_position(actor, owner)

    _check(actor)

    assert owner.resumed == [(position.id, None)]


def test_a_mismatch_start_check_resumes_nothing_and_the_next_clean_check_does():
    actor, owner, _, _ = _setup(FOREIGN_VIEW, FOREIGN_VIEW, EMPTY)
    _open_position(actor, owner)

    assert _check(actor).outcome is ReconcileOutcome.MISMATCH
    assert owner.resumed == []
    assert owner.after == []

    assert _check(actor, ReconcileTrigger.PERIODIC).outcome is ReconcileOutcome.CLEAN
    assert len(owner.resumed) == 1
    assert len(owner.after) == 1


def test_a_resume_that_raises_flattens_and_halts():
    actor, owner, _, journal = _setup(acting=True, fail=True)
    _open_position(actor, owner)

    assert _check(actor).outcome is ReconcileOutcome.CLEAN

    assert ("halt", HaltReason.RECONCILE_MISMATCH, True) in journal.calls


def test_resting_stop_is_the_reduce_only_stop_on_the_closing_side():
    actor, owner, _, _ = _setup()
    position = _open_position(actor, owner)
    assert resting_stop(actor.cache, position) is None
    _resting(actor, owner, reduce_only=False)
    assert resting_stop(actor.cache, position) is None

    stop = _resting(actor, owner)

    assert resting_stop(actor.cache, position).client_order_id == stop.client_order_id


def test_the_base_strategy_cannot_resume():
    _, owner, _, _ = _setup()

    with pytest.raises(NotImplementedError):
        KfsStrategy.resume_position(owner, None, None)


def test_decision_of_reads_the_decision_tag():
    _actor, owner, _, _ = _setup()
    decision = uuid.uuid7()
    tagged = owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY, tags=[f"{DECISION_TAG}{decision}"])
    bare = owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY)
    broken = owner.order_factory.market(INSTRUMENT.id, NautilusOrderSide.BUY, QTY, tags=[f"{DECISION_TAG}nope"])

    assert decision_of(tagged) == decision
    assert decision_of(bare) is None
    assert decision_of(broken) is None
    assert decision_of(None) is None
