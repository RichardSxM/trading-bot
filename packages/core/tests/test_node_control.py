"""covers: spec 0005 AC-6 (pause in two layers, the key at start, not_implemented), AC-7 (trader_state),
AC-9 (entry orders cancelled on stop, stops and positions kept).

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image. The strategy tests use a
BacktestEngine: its Trader starts actors and strategies in the same order as the live node.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderStatus, OrderType, TradingState
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.control.messages import pause_command, stop_command, unpause_command
from trading_core.control.pause import PauseState, decode, encode
from trading_core.node.control import (
    ControlActor,
    PaperStatus,
    PauseController,
    load_pause,
    shutdown_counts,
    trader_state,
)
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource

NOW = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
KEY = "demo:trader:pause"
INSTRUMENT = TestInstrumentProvider.btcusdt_binance()


class FakeRiskEngine:
    def __init__(self):
        self.trading_state = TradingState.ACTIVE

    def set_trading_state(self, state):
        self.trading_state = state


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    async def set(self, name, value):
        if self.fail:
            raise RedisConnectionError("redis down")
        self.values[name] = value

    async def delete(self, *names):
        if self.fail:
            raise RedisConnectionError("redis down")
        for name in names:
            self.values.pop(name, None)


def _controller():
    gate, risk, redis = PauseGate(), FakeRiskEngine(), FakeRedis()
    return PauseController(gate, risk, redis, "demo", lambda: NOW), gate, risk, redis


def _codes(caplog):
    return [c for r in caplog.records if (c := getattr(r, "fields", {}).get("code"))]


# The controller -----------------------------------------------------------------------


def test_pause_sets_the_gate_reducing_and_the_key():
    controller, gate, risk, redis = _controller()
    msg = pause_command(NOW, 60, ControlSource.DEPLOY)

    outcome = asyncio.run(controller.execute(msg))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert gate.paused
    assert gate.state == PauseState(ControlSource.DEPLOY, NOW, msg.id)
    assert risk.trading_state == TradingState.REDUCING
    assert decode(redis.values[KEY]) == gate.state


def test_unpause_deletes_the_key_and_sets_active():
    controller, gate, risk, redis = _controller()
    asyncio.run(controller.execute(pause_command(NOW, 60, ControlSource.DASHBOARD)))

    outcome = asyncio.run(controller.execute(unpause_command(NOW, 60, ControlSource.DASHBOARD)))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert not gate.paused
    assert gate.state is None
    assert risk.trading_state == TradingState.ACTIVE
    assert KEY not in redis.values


@pytest.mark.parametrize("cmd", [ControlCmd.STOP, ControlCmd.RESUME, ControlCmd.FLATTEN])
def test_stop_resume_and_flatten_fail_not_implemented_with_one_warning(cmd, caplog):
    controller, gate, risk, _ = _controller()
    msg = stop_command(NOW, 60)
    msg = type(msg)(msg.id, cmd, msg.issued_at, msg.expires_at)

    with caplog.at_level(logging.WARNING):
        outcome = asyncio.run(controller.execute(msg))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "not_implemented")
    assert _codes(caplog) == ["control_not_implemented"]
    assert not gate.paused
    assert risk.trading_state == TradingState.ACTIVE


def test_a_pause_whose_key_cannot_be_written_fails_but_stays_paused(caplog):
    controller, gate, risk, redis = _controller()
    redis.fail = True

    with caplog.at_level(logging.ERROR):
        outcome = asyncio.run(controller.execute(pause_command(NOW, 60, ControlSource.DEPLOY)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "pause_key_write_failed")
    assert gate.paused
    assert risk.trading_state == TradingState.REDUCING
    assert "pause_key_write_failed" in _codes(caplog)


def test_an_unpause_whose_key_cannot_be_deleted_fails_and_stays_paused():
    controller, gate, risk, redis = _controller()
    asyncio.run(controller.execute(pause_command(NOW, 60, ControlSource.DEPLOY)))
    redis.fail = True

    outcome = asyncio.run(controller.execute(unpause_command(NOW, 60, ControlSource.DASHBOARD)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "pause_key_delete_failed")
    assert gate.paused
    assert risk.trading_state == TradingState.REDUCING


# The key at start ---------------------------------------------------------------------


def test_a_key_present_at_start_pauses_with_its_source():
    controller, gate, risk, _ = _controller()
    state = PauseState(ControlSource.DASHBOARD, NOW, uuid.uuid4())

    load_pause(controller, encode(state).encode())

    assert gate.state == state
    assert risk.trading_state == TradingState.REDUCING


def test_no_key_at_start_runs_active():
    controller, gate, risk, _ = _controller()

    load_pause(controller, None)

    assert not gate.paused
    assert risk.trading_state == TradingState.ACTIVE


def test_an_unreadable_key_at_start_pauses_in_memory(caplog):
    controller, gate, risk, _ = _controller()

    with caplog.at_level(logging.CRITICAL):
        load_pause(controller, b"garbage")

    assert gate.paused
    assert gate.state is None
    assert risk.trading_state == TradingState.REDUCING
    assert _codes(caplog) == ["pause_key_invalid"]


# The node: actor before strategies, the gate, trader_state, stop -----------------------


class Probe(KfsStrategy):
    """At start: a market entry, a resting limit entry, and a reduce only stop, as a feature 6 strategy might."""

    def __init__(self, config, gate, reduce_first=None):
        super().__init__(config, gate)
        self.reduce_first = reduce_first
        self.paused_at_start = None

    def on_start(self):
        self.paused_at_start = self._gate.paused
        if self.reduce_first is not None:
            # The second layer alone: REDUCING while the gate is open.
            self.reduce_first.set_trading_state(TradingState.REDUCING)
        factory = self.order_factory
        self.entry = factory.market(INSTRUMENT.id, OrderSide.BUY, Quantity.from_str("0.001"))
        self.stop_order = factory.stop_market(
            INSTRUMENT.id, OrderSide.SELL, Quantity.from_str("0.001"), Price.from_str("10000.00"), reduce_only=True
        )
        self.submit_order(self.entry)
        self.submit_order(self.stop_order)


def _engine(gate, pause_key_value, reduce_first=False):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("BINANCE"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(100_000, USD)],
        base_currency=USD,
    )
    engine.add_instrument(INSTRUMENT)
    controller = PauseController(gate, engine.kernel.risk_engine, FakeRedis(), "demo", lambda: NOW)

    async def consume():
        await asyncio.sleep(3600)

    engine.add_actor(ControlActor(controller, lambda: pause_key_value, consume))
    strategy = Probe(StrategyConfig(), gate, engine.kernel.risk_engine if reduce_first else None)
    engine.add_strategy(strategy)
    return engine, strategy


def _run(engine):
    async def run():
        engine.run()

    asyncio.run(run())


def test_a_paused_flat_trader_refuses_an_entry_and_accepts_a_reduce_only_stop(caplog):
    gate = PauseGate()
    engine, strategy = _engine(gate, encode(PauseState(ControlSource.DEPLOY, NOW, uuid.uuid4())))

    with caplog.at_level(logging.WARNING):
        _run(engine)

    # The control actor read the key before the strategy started.
    assert strategy.paused_at_start is True
    assert engine.kernel.risk_engine.trading_state == TradingState.REDUCING
    # The entry never reached the engine; the stop did.
    assert engine.cache.order(strategy.entry.client_order_id) is None
    assert engine.cache.order(strategy.stop_order.client_order_id) is not None
    assert "entry_denied_paused" in _codes(caplog)


def test_reducing_alone_lets_a_flat_entry_through_which_is_why_the_gate_exists():
    # Spec 0005 Consequences: REDUCING only denies orders that grow an existing position, so a
    # flat trader is held back by the KfsStrategy gate, not the risk engine. If NautilusTrader
    # ever denies here, this test tells us the second layer got stronger.
    engine, strategy = _engine(PauseGate(), None, reduce_first=True)

    _run(engine)

    entry = engine.cache.order(strategy.entry.client_order_id)
    assert engine.kernel.risk_engine.trading_state == TradingState.REDUCING
    assert entry is not None
    assert entry.status != OrderStatus.DENIED


def test_an_unpaused_trader_submits_both():
    engine, strategy = _engine(PauseGate(), None)

    _run(engine)

    assert strategy.paused_at_start is False
    assert engine.kernel.risk_engine.trading_state == TradingState.ACTIVE
    assert engine.cache.order(strategy.entry.client_order_id) is not None


class FakeOrder:
    def __init__(self, name, reduce_only=False, order_type=OrderType.LIMIT):
        self.client_order_id = name
        self.is_reduce_only = reduce_only
        self.order_type = order_type


class FakeCache:
    def __init__(self, open_orders=(), inflight=(), positions=0):
        self._open = list(open_orders)
        self._inflight = list(inflight)
        self._positions = [object()] * positions

    def orders_open(self):
        return self._open

    def orders_inflight(self):
        return self._inflight

    def positions_open(self):
        return self._positions


def _book():
    entry = FakeOrder("O-1")
    take_profit = FakeOrder("O-2", reduce_only=True)
    stop = FakeOrder("O-3", reduce_only=True, order_type=OrderType.STOP_MARKET)
    stop_limit = FakeOrder("O-4", reduce_only=True, order_type=OrderType.STOP_LIMIT)
    entry_stop = FakeOrder("O-5", order_type=OrderType.STOP_MARKET)  # a stop entry, not protective
    inflight = FakeOrder("O-6")
    # An order both open and in flight (a pending cancel) counts once.
    return FakeCache([entry, take_profit, stop, stop_limit, entry_stop], [inflight, entry], positions=2)


def test_trader_state_reports_the_pause_and_the_counts():
    gate = PauseGate()
    gate.pause(PauseState(ControlSource.DEPLOY, NOW, uuid.uuid4()))
    risk = FakeRiskEngine()
    risk.trading_state = TradingState.REDUCING

    assert trader_state(gate, risk, _book(), "a" * 40) == {
        "trading_state": "REDUCING",
        "paused": True,
        "pause_source": "deploy",
        "paused_at": "2026-10-03T09:30:00.000000Z",
        "open_positions": 2,
        "open_entry_orders": 3,
        "protective_stops": 2,
        "git_commit": "a" * 40,
        "entries_blocked": ["paused"],
        "recording_backlog": 0,
        "exiting": False,
        "position_opened_ns": None,
    }


def test_trader_state_carries_the_paper_status_when_the_paper_loop_runs():
    """covers: spec 0006 AC-16 (entries_blocked, recording_backlog, exiting, position_opened_ns)"""
    gate = PauseGate()
    gate.unpause()
    status = PaperStatus(
        entries_blocked=["stale_feed"], recording_backlog=3, exiting=True, position_opened_ns=1_700_000_000
    )

    state = trader_state(gate, FakeRiskEngine(), FakeCache(), "x", lambda: status)

    assert state is not None
    assert (state["entries_blocked"], state["recording_backlog"], state["exiting"], state["position_opened_ns"]) == (
        ["stale_feed"],
        3,
        True,
        1_700_000_000,
    )


def test_trader_state_without_the_paper_loop_reports_nothing_blocked_while_running():
    gate = PauseGate()
    gate.unpause()

    state = trader_state(gate, FakeRiskEngine(), FakeCache(), "x")

    assert state is not None
    assert (state["entries_blocked"], state["recording_backlog"], state["exiting"], state["position_opened_ns"]) == (
        [],
        0,
        False,
        None,
    )


def test_trader_state_while_running_has_no_pause_fields():
    gate = PauseGate()
    gate.unpause()
    state = trader_state(gate, FakeRiskEngine(), FakeCache(), "x")

    assert state is not None
    assert (state["trading_state"], state["paused"], state["pause_source"], state["paused_at"]) == (
        "ACTIVE",
        False,
        None,
        None,
    )


def test_trader_state_is_none_until_the_pause_key_was_read_at_start():
    gate, risk, redis = PauseGate(), FakeRiskEngine(), FakeRedis()
    controller = PauseController(gate, risk, redis, "demo", lambda: NOW)
    assert trader_state(gate, risk, FakeCache(), "x") is None

    load_pause(controller, None)

    assert trader_state(gate, risk, FakeCache(), "x") is not None


def test_shutdown_counts_take_what_is_left_after_the_strategies_stopped():
    gate = PauseGate()
    gate.note_cancelled(4)

    assert shutdown_counts(gate, _book()) == {
        "cancelled": 4,
        "unacknowledged_cancels": 3,
        "positions_kept": 2,
        "stops_kept": 2,
    }


def test_on_stop_pauses_in_memory_keeps_the_state_and_cancels_only_entry_orders():
    gate = PauseGate()
    state = PauseState(ControlSource.DASHBOARD, NOW, uuid.uuid4())
    gate.pause(state)
    engine, strategy = _engine(gate, encode(state))
    _run(engine)
    orders = [*engine.cache.orders_open(), *engine.cache.orders_inflight()]

    strategy.on_stop()

    assert gate.paused
    assert gate.state == state
    assert gate.cancelled == len([o for o in orders if not o.is_reduce_only])
