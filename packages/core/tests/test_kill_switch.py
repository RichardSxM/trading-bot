"""covers: spec 0010 AC-1 (halt then flatten), AC-2 (flatten), AC-3 (executed when flat, flatten_timeout,
late fill re-flatten, a second stop), AC-4 (resume), AC-5 (the halt key), AC-6 (key write failure),
AC-7 (halt event, restart flatten).

Needs the ``node`` extra (NautilusTrader) for ``node.control``, so it runs in the trader image.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.model.enums import TradingState
from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.control import halt
from trading_core.control.halt import HaltReason, HaltSource
from trading_core.control.messages import command
from trading_core.node.control import KillSwitch, PauseController, control_execute
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource, RiskEventKind, Severity
from trading_core.recording.spool import WriteCall, WriteItem

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
HALT_KEY = "demo:trader:halt"


class FakeRiskEngine:
    def __init__(self):
        self.trading_state = TradingState.ACTIVE

    def set_trading_state(self, state):
        self.trading_state = state


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False
        self.sets = 0

    async def set(self, name, value):
        if self.fail:
            raise RedisConnectionError("redis down")
        self.sets += 1
        self.values[name] = value

    async def delete(self, *names):
        if self.fail or getattr(self, "fail_delete", False):
            raise RedisConnectionError("redis down")
        for name in names:
            self.values.pop(name, None)


class FakeOrder:
    def __init__(self, name, reduce_only=False):
        self.client_order_id = name
        self.is_reduce_only = reduce_only


class FakeCache:
    """Positions per strategy id; ``positions_open()`` without an id lists them all."""

    def __init__(self):
        self.positions = {}
        self.open = []
        self.inflight = []

    def positions_open(self, strategy_id=None):
        if strategy_id is None:
            return [p for ps in self.positions.values() for p in ps]
        return list(self.positions.get(strategy_id, []))

    def orders_open(self):
        return list(self.open)

    def orders_inflight(self):
        return list(self.inflight)


class FakeStrategy:
    """Closes its position when flattened, unless ``stuck``; records each flatten id."""

    def __init__(self, cache, name="S-1", stuck=False):
        self.id = name
        self.cache = cache
        self.stuck = stuck
        self.flattened = []

    def flatten(self, halt_id):
        self.flattened.append(halt_id)
        self.cache.open = [o for o in self.cache.open if o.is_reduce_only]
        if not self.stuck:
            self.cache.positions.pop(self.id, None)


class Clock:
    def __init__(self, step=timedelta(0)):
        self.now = NOW
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def _kill_switch(cache=None, clock=None, timeout_s=20, redis=None):
    gate, risk, redis = PauseGate(), FakeRiskEngine(), redis or FakeRedis()
    cache = cache or FakeCache()
    records: list[WriteItem] = []
    ks = KillSwitch(gate, risk, redis, "demo", clock or Clock(), cache, timeout_s, poll_s=0.001)
    return ks, gate, risk, redis, cache, records


def _stop(source=ControlSource.DASHBOARD):
    return command(ControlCmd.STOP, NOW, 60, source)


def _events(records):
    return [item.row for item in records if item.call is WriteCall.RECORD_RISK_EVENT]


def test_stop_when_flat_halts_writes_the_key_and_one_warning_halt_event():
    ks, gate, risk, redis, _, records = _kill_switch()
    ks.attach([], records.append, uuid.uuid4())
    msg = _stop()

    outcome = asyncio.run(ks.stop(msg))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert gate.halted
    assert risk.trading_state == TradingState.REDUCING
    state = halt.decode(redis.values[HALT_KEY])
    assert (state.reason, state.source, state.halt_id) == (HaltReason.MANUAL_STOP, HaltSource.DASHBOARD, msg.id)
    [event] = _events(records)
    assert (event.event_key, event.kind, event.severity) == (f"halt:{msg.id}", RiskEventKind.HALT, Severity.WARNING)
    assert event.details == {"reason": "manual_stop", "source": "dashboard"}


def test_stop_flattens_every_strategy_and_answers_executed_once_flat():
    cache = FakeCache()
    cache.positions = {"S-1": ["p1"], "S-2": ["p2"]}
    cache.open = [FakeOrder("entry"), FakeOrder("stop", reduce_only=True)]
    ks, gate, *_ = _kill_switch(cache)
    strategies = [FakeStrategy(cache, "S-1"), FakeStrategy(cache, "S-2")]
    ks.attach(strategies, None, None)
    msg = _stop(ControlSource.CLI)

    outcome = asyncio.run(ks.stop(msg))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert [s.flattened for s in strategies] == [[msg.id], [msg.id]]
    # A reduce only order does not count against flat.
    assert ks.is_flat()
    assert gate.halt_state.source is HaltSource.CLI


def test_a_position_that_appears_during_the_wait_is_flattened_again():
    cache = FakeCache()
    ks, *_ = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], None, None)
    cache.inflight = [FakeOrder("late-entry")]

    async def run():
        task = asyncio.create_task(ks.stop(_stop()))
        await asyncio.sleep(0.01)
        # The entry in flight fills after the halt: a position appears.
        cache.inflight = []
        cache.positions = {"S-1": ["late"]}
        return await task

    outcome = asyncio.run(run())

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert len(strategy.flattened) >= 2


def test_not_flat_after_the_timeout_fails_with_one_critical_event_and_keeps_halted(caplog):
    cache = FakeCache()
    cache.positions = {"S-1": ["p1"]}
    ks, gate, *_, records = _kill_switch(cache, clock=Clock(step=timedelta(seconds=1)), timeout_s=5)
    ks.attach([FakeStrategy(cache, stuck=True)], records.append, uuid.uuid4())
    msg = _stop()

    outcome = asyncio.run(ks.stop(msg))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "flatten_timeout")
    assert gate.halted
    assert not ks.flattening
    timeouts = [e for e in _events(records) if e.event_key == f"flatten_timeout:{msg.id}"]
    assert [(e.kind, e.severity) for e in timeouts] == [(RiskEventKind.ORDER_REJECTED, Severity.CRITICAL)]


def test_a_key_write_failure_still_halts_and_flattens_but_fails(caplog):
    cache = FakeCache()
    cache.positions = {"S-1": ["p1"]}
    ks, gate, risk, redis, cache, records = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], records.append, uuid.uuid4())
    redis.fail = True
    msg = _stop()

    with caplog.at_level(logging.ERROR):
        outcome = asyncio.run(ks.stop(msg))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "halt_key_write_failed")
    assert gate.halted
    assert risk.trading_state == TradingState.REDUCING
    assert strategy.flattened == [msg.id]
    keys = [(e.event_key, e.severity) for e in _events(records)]
    assert (f"halt_key_write_failed:{msg.id}", Severity.CRITICAL) in keys


def test_a_second_stop_keeps_the_first_halt_and_writes_no_second_event_but_flattens_again():
    cache = FakeCache()
    ks, gate, _, redis, cache, records = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], records.append, uuid.uuid4())
    first, second = _stop(), _stop()

    asyncio.run(ks.stop(first))
    cache.positions = {"S-1": ["p"]}
    outcome = asyncio.run(ks.stop(second))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert gate.halt_state.halt_id == first.id
    assert redis.sets == 1
    assert [e.event_key for e in _events(records)] == [f"halt:{first.id}"]
    assert strategy.flattened == [first.id, second.id]


def test_a_second_stop_retries_a_halt_key_that_failed_to_write():
    ks, gate, _, redis, *_ = _kill_switch()
    redis.fail = True
    asyncio.run(ks.stop(_stop()))
    redis.fail = False

    outcome = asyncio.run(ks.stop(_stop()))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert halt.decode(redis.values[HALT_KEY]) == gate.halt_state


def test_live_with_no_strategy_judges_flat_from_the_cache_and_only_logs(caplog):
    cache = FakeCache()
    cache.positions = {"external": ["p"]}
    ks, *_ = _kill_switch(cache, clock=Clock(step=timedelta(seconds=1)), timeout_s=5)

    with caplog.at_level(logging.CRITICAL):
        outcome = asyncio.run(ks.stop(_stop()))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "flatten_timeout")
    assert any(getattr(r, "fields", {}).get("code") == "order_rejected" for r in caplog.records)


def test_flatten_after_restart_flattens_only_when_halted():
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, *_ = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], None, None)

    async def run():
        ks.flatten_after_restart()
        assert strategy.flattened == []
        ks.load(halt.encode(halt.HaltState(HaltReason.MANUAL_STOP, HaltSource.DASHBOARD, NOW, uuid.uuid4())))
        ks.flatten_after_restart()
        await asyncio.sleep(0.01)

    asyncio.run(run())

    assert len(strategy.flattened) == 1
    assert ks.is_flat()


def test_control_execute_routes_stop_to_the_kill_switch_and_pause_to_the_controller():
    ks, gate, risk, redis, *_ = _kill_switch()
    execute = control_execute(PauseController(gate, risk, redis, "demo", lambda: NOW), ks)

    asyncio.run(execute(command(ControlCmd.PAUSE, NOW, 60, ControlSource.DEPLOY)))
    asyncio.run(execute(_stop()))

    assert gate.paused
    assert gate.halted
    assert HALT_KEY in redis.values


# Flatten and resume (spec 0010 AC-2, AC-4) -----------------------------------------------

PAUSE_KEY = "demo:trader:pause"


class Losses:
    def __init__(self, seeded=True, total_over_limit=False):
        self.seeded = seeded
        self.total_over_limit = total_over_limit


def _halted(**kwargs):
    parts = _kill_switch(**kwargs)
    ks, gate, _, redis, cache, records = parts
    ks.attach([FakeStrategy(cache)], records.append, uuid.uuid4())
    asyncio.run(ks.stop(_stop()))
    redis.values[PAUSE_KEY] = "{}"
    gate.pause_in_memory()
    return parts


def test_flatten_closes_without_halting_or_touching_the_pause():
    """covers: spec 0010 AC-2"""
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, gate, risk, redis, cache, _ = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], None, None)
    msg = command(ControlCmd.FLATTEN, NOW, 60)

    outcome = asyncio.run(ks.flatten(msg))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert strategy.flattened == [msg.id]
    assert not gate.halted
    assert not gate.paused
    assert risk.trading_state == TradingState.ACTIVE
    assert HALT_KEY not in redis.values


def test_flatten_that_cannot_get_flat_fails_with_flatten_timeout():
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, *_ = _kill_switch(cache, clock=Clock(step=timedelta(seconds=1)), timeout_s=5)
    ks.attach([FakeStrategy(cache, stuck=True)], None, None)

    outcome = asyncio.run(ks.flatten(command(ControlCmd.FLATTEN, NOW, 60)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "flatten_timeout")


def test_resume_deletes_both_keys_lifts_the_halt_and_pause_and_records_one_info_event():
    """covers: spec 0010 AC-4"""
    ks, gate, risk, redis, _, records = _halted()
    msg = command(ControlCmd.RESUME, NOW, 60)

    outcome = asyncio.run(ks.resume(msg))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert not gate.halted
    assert not gate.paused
    assert risk.trading_state == TradingState.ACTIVE
    assert HALT_KEY not in redis.values
    assert PAUSE_KEY not in redis.values
    resumes = [e for e in _events(records) if e.kind is RiskEventKind.RESUME]
    assert [(e.event_key, e.severity) for e in resumes] == [(f"resume:{msg.id}", Severity.INFO)]


def test_resume_while_a_position_is_open_fails_not_flat_and_changes_nothing():
    ks, gate, risk, redis, cache, _ = _halted()
    cache.positions = {"S-9": ["p"]}

    outcome = asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "not_flat")
    assert gate.halted
    assert risk.trading_state == TradingState.REDUCING
    assert HALT_KEY in redis.values


def test_resume_while_an_entry_order_is_still_open_fails_not_flat():
    """covers: spec 0010 AC-4 (an entry left after a flatten timeout could fill once the halt lifts)"""
    ks, gate, _, redis, cache, _ = _halted()
    cache.inflight = [FakeOrder("entry")]

    outcome = asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "not_flat")
    assert gate.halted
    assert HALT_KEY in redis.values


class TripOnDelete(FakeRedis):
    """A loss limit trips while resume's delete is on its way to Redis."""

    def __init__(self):
        super().__init__()
        self.kill_switch = None

    async def delete(self, *names):
        self.kill_switch.halt_automatic(HaltReason.TOTAL_LOSS_LIMIT)
        await super().delete(*names)


@pytest.mark.parametrize("halted_before", [True, False])
def test_a_halt_that_lands_during_resume_s_delete_is_kept_and_its_key_written_back(halted_before):
    """covers: spec 0010 AC-4, AC-10 (a limit tripped during resume is never lost)"""
    redis = TripOnDelete()
    ks, gate, risk, redis, cache, records = _kill_switch(redis=redis)
    redis.kill_switch = ks
    ks.attach([FakeStrategy(cache)], records.append, uuid.uuid4())
    if halted_before:
        asyncio.run(ks.stop(_stop()))

    async def run():
        outcome = await ks.resume(command(ControlCmd.RESUME, NOW, 60))
        await asyncio.sleep(0.01)  # let the automatic halt's task settle
        return outcome

    outcome = asyncio.run(run())

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "halted_during_resume")
    assert gate.halted
    assert risk.trading_state == TradingState.REDUCING
    assert halt.decode(redis.values[HALT_KEY]) == gate.halt_state
    assert not [e for e in _events(records) if e.kind is RiskEventKind.RESUME]


@pytest.mark.parametrize(
    ("losses", "reason"),
    [(Losses(seeded=False), "loss_unknown"), (Losses(total_over_limit=True), "total_loss_limit")],
)
def test_resume_is_refused_while_the_loss_limits_forbid_it(losses, reason):
    ks, gate, *_ = _halted()
    ks.watch_losses(losses)

    outcome = asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, reason)
    assert gate.halted


def test_resume_whose_delete_fails_stays_halted_and_paused():
    ks, gate, risk, redis, *_ = _halted()
    redis.fail_delete = True

    outcome = asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "halt_key_delete_failed")
    assert gate.halted
    assert gate.paused
    assert risk.trading_state == TradingState.REDUCING


def test_resume_when_not_halted_still_clears_a_pause():
    ks, gate, _, redis, *_ = _kill_switch()
    ks.watch_losses(Losses(seeded=False))
    redis.values[PAUSE_KEY] = "{}"
    gate.pause_in_memory()

    outcome = asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert not gate.paused
    assert PAUSE_KEY not in redis.values


def test_a_new_halt_after_resume_writes_the_key_again():
    ks, gate, _, redis, *_ = _halted()
    asyncio.run(ks.resume(command(ControlCmd.RESUME, NOW, 60)))

    asyncio.run(ks.stop(_stop()))

    assert halt.decode(redis.values[HALT_KEY]) == gate.halt_state


def test_resume_while_a_stop_is_still_waiting_for_flat_fails_not_flat():
    """covers: spec 0010 AC-4 (not_flat while a stop's flatten is still running)"""
    cache = FakeCache()
    cache.inflight = [FakeOrder("entry")]  # no position: only the running flatten says not flat
    ks, gate, _, redis, *_ = _kill_switch(cache)
    ks.attach([FakeStrategy(cache)], None, None)

    async def run():
        stop = asyncio.create_task(ks.stop(_stop()))
        await asyncio.sleep(0.01)
        refused = await ks.resume(command(ControlCmd.RESUME, NOW, 60))
        cache.inflight = []
        await stop
        return refused

    outcome = asyncio.run(run())

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "not_flat")
    assert gate.halted
    assert HALT_KEY in redis.values


class SlowRedis(FakeRedis):
    """``set`` waits until ``release`` is set, like a Redis that answers late."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def set(self, name, value):
        await self.release.wait()
        await super().set(name, value)


def test_a_slow_halt_key_write_never_delays_the_flatten():
    """covers: spec 0010 AC-1 (the key is written while the positions close)"""
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, _, _, redis, *_ = _kill_switch(cache, redis=SlowRedis())
    strategy = FakeStrategy(cache)
    ks.attach([strategy], None, None)
    msg = _stop()

    async def run():
        stop = asyncio.create_task(ks.stop(msg))
        await asyncio.sleep(0.01)
        flattened_before_the_write = list(strategy.flattened)
        redis.release.set()
        return flattened_before_the_write, await stop

    flattened, outcome = asyncio.run(run())

    assert flattened == [msg.id]
    assert outcome.outcome is ControlOutcome.EXECUTED
    assert HALT_KEY in redis.values


def _halt_automatic(ks, reason, settle_s=0.01):
    async def run():
        halt_id = ks.halt_automatic(reason)
        await asyncio.sleep(settle_s)  # the key, the event, and the flat wait run as a task
        return halt_id

    return asyncio.run(run())


def test_an_automatic_halt_writes_the_key_flattens_and_records_one_critical_event():
    """covers: spec 0010 AC-5, AC-7 (an automatic halt is critical, source trader), AC-10"""
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, gate, risk, redis, cache, records = _kill_switch(cache)
    strategy = FakeStrategy(cache)
    ks.attach([strategy], records.append, uuid.uuid4())

    halt_id = _halt_automatic(ks, HaltReason.TOTAL_LOSS_LIMIT)

    assert risk.trading_state == TradingState.REDUCING
    state = halt.decode(redis.values[HALT_KEY])
    assert (state.reason, state.source, state.halt_id) == (HaltReason.TOTAL_LOSS_LIMIT, HaltSource.TRADER, halt_id)
    assert gate.halt_state == state
    assert strategy.flattened[0] == halt_id
    assert cache.positions_open() == []
    [event] = _events(records)
    assert (event.event_key, event.kind, event.severity) == (f"halt:{halt_id}", RiskEventKind.HALT, Severity.CRITICAL)
    assert event.details == {"reason": "total_loss_limit", "source": "trader"}


def test_a_limit_that_trips_while_halted_keeps_the_first_halt_and_writes_no_second_event():
    """covers: spec 0010 AC-5 (already halted)"""
    ks, gate, _, redis, _, records = _kill_switch()
    strategy = FakeStrategy(FakeCache())
    ks.attach([strategy], records.append, uuid.uuid4())
    first = _stop()
    asyncio.run(ks.stop(first))
    key_before = redis.values[HALT_KEY]

    second_id = _halt_automatic(ks, HaltReason.DAILY_LOSS_LIMIT)

    assert gate.halt_state.halt_id == first.id
    assert gate.halt_state.reason is HaltReason.MANUAL_STOP
    assert redis.values[HALT_KEY] == key_before
    assert redis.sets == 1
    assert [e.event_key for e in _events(records)] == [f"halt:{first.id}"]
    assert strategy.flattened == [first.id, second_id]  # it still flattens again


def test_an_automatic_halt_that_cannot_get_flat_writes_flatten_timeout():
    """covers: spec 0010 AC-3 (an automatic halt's flat wait is its own task)"""
    cache = FakeCache()
    cache.positions = {"S-1": ["p"]}
    ks, gate, *_, records = _kill_switch(cache, clock=Clock(step=timedelta(seconds=1)), timeout_s=5)
    ks.attach([FakeStrategy(cache, stuck=True)], records.append, uuid.uuid4())

    halt_id = _halt_automatic(ks, HaltReason.DAILY_LOSS_LIMIT, settle_s=0.1)

    timeouts = [e for e in _events(records) if e.event_key == f"flatten_timeout:{halt_id}"]
    assert [(e.kind, e.severity) for e in timeouts] == [(RiskEventKind.ORDER_REJECTED, Severity.CRITICAL)]
    assert gate.halted
    assert not ks.flattening


def test_control_execute_routes_flatten_and_resume_to_the_kill_switch():
    ks, gate, risk, redis, *_ = _kill_switch()
    execute = control_execute(PauseController(gate, risk, redis, "demo", lambda: NOW), ks)

    flattened = asyncio.run(execute(command(ControlCmd.FLATTEN, NOW, 60)))
    resumed = asyncio.run(execute(command(ControlCmd.RESUME, NOW, 60)))

    assert (flattened.outcome, resumed.outcome) == (ControlOutcome.EXECUTED, ControlOutcome.EXECUTED)
