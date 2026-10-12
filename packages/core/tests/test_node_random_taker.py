"""covers: spec 0010 AC-1 (the kill switch flattens the RandomTaker with tagged orders, and only those),
AC-2 (it trades again once flat), AC-15 (it refuses entries while halted).

Runs ``RandomTaker`` in a BacktestEngine on the synthetic quotes of ``test_node_placeholder``.
Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_placeholder import (
    HALT_ID,
    INSTRUMENT,
    INSTRUMENT_ID,
    KILL_SWITCH_TAGS,
    START,
    Prices,
    _decisions,
    _ns,
    _quotes,
    _risk_events,
)

from trading_core.control.halt import HaltReason, HaltSource, HaltState
from trading_core.node.random_taker import Phase, RandomTaker, RandomTakerConfig
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env, Intent, OrderOrigin, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import OrderRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks

# One entry a minute, held 30 s: the first round trip runs 12:01:00 to 12:01:30.
RISING = Prices(3, lambda t: 85000 + int(t * 10))


class _FlattenAt(RandomTaker):
    """Calls ``flatten(HALT_ID)`` once, ``at`` seconds after START."""

    at = 70

    def on_start(self):
        super().on_start()
        at_ns = _ns(START + timedelta(seconds=type(self).at))
        self.clock.set_time_alert_ns("test_flatten", at_ns, lambda _: self.flatten(HALT_ID))


def _run(prices, strategy_cls=RandomTaker, gate=None):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(1000, USD)],
        base_currency=USD,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_quotes(prices))
    run_id = uuid.uuid7()
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    engine.add_actor(
        RecordingActor(env=Env.DEMO, run_id=run_id, instrument_id=INSTRUMENT_ID, record=records.append, blocks=blocks)
    )
    config = RandomTakerConfig(
        env="demo",
        run_id=str(run_id),
        instrument_id=str(INSTRUMENT_ID),
        seed=1,
        max_order_notional_usd=Decimal(250),
        max_leverage=Decimal(2),
        notional_usd=Decimal(100),
    )
    strategy = strategy_cls(config, gate or PauseGate(), blocks, records.append)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _exits(engine):
    return [o for o in engine.cache.orders() if o.order_type == OrderType.MARKET and o.is_reduce_only]


def _order_rows(rows, order):
    return {
        (r.origin, r.decision_id)
        for r in rows
        if isinstance(r, OrderRow) and r.client_order_id == str(order.client_order_id)
    }


def test_flatten_while_holding_closes_at_market_with_the_kill_switch_tags():
    _FlattenAt.at = 70  # 12:01:10, inside the first 30 s hold
    engine, strategy, rows = _run(RISING, strategy_cls=_FlattenAt)

    first_exit, second_exit = _exits(engine)
    assert first_exit.tags == KILL_SWITCH_TAGS
    assert _order_rows(rows, first_exit) == {(OrderOrigin.KILL_SWITCH, None)}
    # Once flat it trades again (AC-2), and the 12:02 round trip's exit is its own.
    second_decision = [d for d in _decisions(rows) if d.intent == Intent.EXIT][-1]
    assert second_exit.tags == [f"{DECISION_TAG}{second_decision.id}"]
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_flatten_while_flat_leaves_the_next_round_trip_its_own_tags():
    """A stop or flatten while flat, or the restart flatten, must not tag later exits."""
    _FlattenAt.at = 30  # 12:00:30, flat: before the first boundary
    engine, _, rows = _run(RISING, strategy_cls=_FlattenAt)

    exit_decisions = [d for d in _decisions(rows) if d.intent == Intent.EXIT]
    first_exit = _exits(engine)[0]
    assert first_exit.tags == [f"{DECISION_TAG}{exit_decisions[0].id}"]
    assert _order_rows(rows, first_exit) == {(OrderOrigin.STRATEGY, exit_decisions[0].id)}


def test_a_halted_gate_refuses_every_entry_as_halted_and_places_nothing():
    gate = PauseGate()
    gate.halt(HaltState(HaltReason.DAILY_LOSS_LIMIT, HaltSource.TRADER, START, HALT_ID))
    engine, strategy, rows = _run(RISING, gate=gate)

    decisions = _decisions(rows)
    assert decisions
    assert {(d.intent, d.risk_verdict, d.risk_reason) for d in decisions} == {
        (Intent.ENTER, RiskVerdict.REJECTED, "halted")
    }
    events = _risk_events(rows)
    assert {(e.kind, e.severity) for e in events} == {(RiskEventKind.LIMIT_HIT, Severity.INFO)}
    assert len(events) == len(decisions)
    assert engine.cache.orders() == []
    assert strategy.phase == Phase.FLAT
