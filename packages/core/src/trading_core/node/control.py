"""The trader's control side in NautilusTrader (spec 0005 AC-6, AC-7, AC-9, spec 0010).

``ControlActor`` is registered before any strategy. At start it reads the halt and pause keys
and applies them once, then runs the ``ControlConsumer`` on the node's event loop.
``PauseController`` executes ``pause`` and ``unpause``: it sets the ``PauseGate`` and writes or
deletes ``{env}:trader:pause``. ``KillSwitch`` executes ``stop``: it halts in memory, then writes
``{env}:trader:halt`` while it cancels entries and closes positions through every strategy's
``flatten``, and answers once the trader is flat (or after its timeout).

The risk engine's trading state has one owner, ``apply_trading_state``: ``REDUCING`` while the
trader is paused or halted, else ``ACTIVE``. So no pause path can lift a halt's ``REDUCING``.

``trader_state`` and ``shutdown_counts`` read the node's cache for the snapshot writer and the
``shutdown_graceful`` log line.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.enums import OrderType, TradingState
from nautilus_trader.model.orders.base import Order
from redis.exceptions import RedisError

from trading_core.control import halt
from trading_core.control.consumer import Execute, Outcome
from trading_core.control.halt import HaltFormatError, HaltReason, HaltSource, HaltState, halt_key
from trading_core.control.messages import ControlMessage, format_utc
from trading_core.control.pause import PauseFormatError, PauseState, decode, encode, pause_key, target_trading_state
from trading_core.node.strategy import KfsStrategy, PauseGate, entry_orders
from trading_core.recording.enums import ControlCmd, ControlOutcome, Env, RiskEventKind, Severity
from trading_core.recording.rows import JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.settings import TradingEnv
from trading_core.strategy.blocks import EntryBlocks, entries_blocked

log = logging.getLogger(__name__)

PROTECTIVE_TYPES: Final = (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
# How often the kill switch looks at the cache while it waits for flat (spec 0010 AC-3).
FLAT_POLL_S: Final = 0.25
# A trader that starts halted flattens again this long after start (spec 0010 AC-7).
RESTART_FLATTEN_DELAY: Final = timedelta(seconds=5)
_RESTART_FLATTEN_ALERT: Final = "kill_switch_restart_flatten"
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}

type Record = Callable[[WriteItem], None]


class RiskEngine(Protocol):
    """The risk engine calls the controller needs (NautilusTrader's ``RiskEngine``)."""

    @property
    def trading_state(self) -> TradingState: ...

    def set_trading_state(self, state: TradingState) -> None: ...


class PauseRedis(Protocol):
    """The pause and halt key writes (redis.asyncio.Redis satisfies it)."""

    def set(self, name: str, value: str) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[object]: ...


def apply_trading_state(gate: PauseGate, risk_engine: RiskEngine) -> None:
    """Set the risk engine from both flags: ``REDUCING`` while paused or halted, else ``ACTIVE`` (spec 0010)."""
    risk_engine.set_trading_state(TradingState[target_trading_state(gate.paused, gate.halted)])


class PauseController:
    """Executes control commands against the gate, the risk engine, and the pause key."""

    def __init__(
        self,
        gate: PauseGate,
        risk_engine: RiskEngine,
        client: PauseRedis,
        env: TradingEnv,
        now: Callable[[], datetime],
    ) -> None:
        self._gate = gate
        self._risk_engine = risk_engine
        self._client = client
        self._key = pause_key(env)
        self._now = now

    def apply(self, state: PauseState | None) -> None:
        """Pause (``state`` given) or run (None): the gate first, then the risk engine."""
        if state is None:
            self._gate.unpause()
        else:
            self._gate.pause(state)
        apply_trading_state(self._gate, self._risk_engine)

    @property
    def halted(self) -> bool:
        """True while the trader is halted (the gate's flag)."""
        return self._gate.halted

    def pause_unknown(self) -> None:
        """Pause in memory when the pause key cannot be read at start (fail closed)."""
        self._gate.pause_in_memory()
        apply_trading_state(self._gate, self._risk_engine)

    async def execute(self, msg: ControlMessage) -> Outcome:
        """Run one command; trading state changes come before the key write, so a failure stays safe."""
        if msg.cmd is ControlCmd.PAUSE:
            return await self._pause(msg)
        if msg.cmd is ControlCmd.UNPAUSE:
            return await self._unpause()
        log.warning(
            "control command not implemented",
            extra={"fields": {"code": "control_not_implemented", "cmd": msg.cmd.value, "id": str(msg.id)}},
        )
        return Outcome(ControlOutcome.FAILED, "not_implemented")

    async def _pause(self, msg: ControlMessage) -> Outcome:
        state = PauseState(msg.source, self._now(), msg.id)
        self.apply(state)
        try:
            await self._client.set(self._key, encode(state))
        except (RedisError, OSError, TimeoutError) as exc:
            # Paused in memory; a restart before the next pause would come back running.
            log.error(
                "pause key not written",
                extra={"fields": {"code": "pause_key_write_failed", "error": type(exc).__name__}},
            )
            return Outcome(ControlOutcome.FAILED, "pause_key_write_failed")
        log.info(
            "trader paused",
            extra={"fields": {"code": "trader_paused", "source": msg.source.value, "id": str(msg.id)}},
        )
        return Outcome(ControlOutcome.EXECUTED)

    async def _unpause(self) -> Outcome:
        try:
            await self._client.delete(self._key)
        except (RedisError, OSError, TimeoutError) as exc:
            # Stays paused: the key would pause the trader again on its next start.
            log.error(
                "pause key not deleted",
                extra={"fields": {"code": "pause_key_delete_failed", "error": type(exc).__name__}},
            )
            return Outcome(ControlOutcome.FAILED, "pause_key_delete_failed")
        self.apply(None)
        log.info("trader unpaused", extra={"fields": {"code": "trader_unpaused"}})
        return Outcome(ControlOutcome.EXECUTED)


def load_pause(controller: PauseController, raw: str | bytes | None) -> None:
    """Apply the pause key value found at start: absent runs, present pauses, unreadable pauses."""
    if raw is None:
        controller.apply(None)
        return
    try:
        state = decode(raw)
    except PauseFormatError as exc:
        log.critical(
            "pause key unreadable, starting paused",
            extra={"fields": {"code": "pause_key_invalid", "error": str(exc)}},
        )
        controller.pause_unknown()
        return
    controller.apply(state)
    log.info(
        "trader starts paused",
        extra={"fields": {"code": "trader_starts_paused", "source": state.source.value}},
    )


class LossStatus(Protocol):
    """What ``resume`` needs from the loss limits (spec 0010 AC-4): seeded yet, and the total under its limit."""

    @property
    def seeded(self) -> bool: ...

    @property
    def total_over_limit(self) -> bool: ...


class KillSwitch:
    """Executes ``stop``, ``flatten``, and ``resume`` (spec 0010 AC-1 to AC-7).

    ``stop`` halts, cancels entries, flattens, and waits for flat; ``flatten`` does the same without
    the halt; ``resume`` lifts the halt and any pause, unless the trader is not flat or a loss limit
    still forbids it.

    The halt is applied in memory before any I/O, so a Redis failure can only leave the trader
    more halted, never less. ``attach`` hands it the strategies and the recording callable once
    they exist; with none (live) flat is judged from the cache alone and events are only logged.
    """

    def __init__(
        self,
        gate: PauseGate,
        risk_engine: RiskEngine,
        client: PauseRedis,
        env: TradingEnv,
        now: Callable[[], datetime],
        cache: Cache,
        flat_timeout_s: float,
        poll_s: float = FLAT_POLL_S,
    ) -> None:
        self._gate = gate
        self._risk_engine = risk_engine
        self._client = client
        self._env = Env(env)
        self._key = halt_key(env)
        self._pause_key = pause_key(env)
        self._now = now
        self._cache = cache
        self._flat_timeout = timedelta(seconds=flat_timeout_s)
        self._poll_s = poll_s
        self._strategies: Sequence[KfsStrategy] = ()
        self._losses: LossStatus | None = None
        self._record: Record | None = None
        self._run_id: uuid.UUID | None = None
        self._key_written = False
        self._flattening = 0
        self._tasks: set[asyncio.Task[object]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, strategies: Sequence[KfsStrategy], record: Record | None, run_id: uuid.UUID | None) -> None:
        """Hand over the strategies to flatten and where events go, once they exist (``main``)."""
        self._strategies = tuple(strategies)
        self._record = record
        self._run_id = run_id

    def watch_losses(self, losses: LossStatus) -> None:
        """Let ``resume`` read the loss limits (the ``LossLimitActor``); without it nothing is refused for them."""
        self._losses = losses

    @property
    def flattening(self) -> bool:
        """True while a stop, flatten, or automatic halt waits for flat."""
        return self._flattening > 0

    def load(self, raw: str | bytes | None) -> None:
        """Apply the halt key value found at start, before the pause key (the caller applies the state).

        Absent runs; present halts; unreadable halts in memory with reason ``unknown`` (fail closed).
        """
        if raw is None:
            return
        try:
            state = halt.decode(raw)
        except HaltFormatError as exc:
            log.critical(
                "halt key unreadable, starting halted",
                extra={"fields": {"code": "halt_key_invalid", "error": str(exc)}},
            )
            self.halt_unknown()
            return
        self._gate.halt(state)
        self._key_written = True
        log.warning(
            "trader starts halted",
            extra={
                "fields": {"code": "trader_starts_halted", "reason": state.reason.value, "halt_id": str(state.halt_id)}
            },
        )

    def halt_unknown(self) -> None:
        """Halt in memory with reason ``unknown``: the halt key could not be read at start."""
        self._gate.halt(HaltState(HaltReason.UNKNOWN, HaltSource.TRADER, self._now(), uuid.uuid7()))

    def is_flat(self) -> bool:
        """No open position, and no entry order (not reduce only) open, in flight, or pending cancel."""
        if self._cache.positions_open():
            return False
        return not entry_orders([*self._cache.orders_open(), *self._cache.orders_inflight()])

    async def stop(self, msg: ControlMessage) -> Outcome:
        """Halt (``manual_stop``), write the key while flattening, then wait for flat."""
        state = HaltState(HaltReason.MANUAL_STOP, HaltSource(msg.source.value), self._now(), msg.id)
        key_ok = await self._halt(state, flatten_id=msg.id)
        flat = await self._wait_flat(msg.id, self._now())
        if not key_ok:
            return Outcome(ControlOutcome.FAILED, "halt_key_write_failed")
        if not flat:
            return Outcome(ControlOutcome.FAILED, "flatten_timeout")
        return Outcome(ControlOutcome.EXECUTED)

    async def flatten(self, msg: ControlMessage) -> Outcome:
        """Cancel entries and close positions without halting or touching the pause, then wait for flat."""
        self._flatten_all(msg.id)
        if await self._wait_flat(msg.id, self._now()):
            return Outcome(ControlOutcome.EXECUTED)
        return Outcome(ControlOutcome.FAILED, "flatten_timeout")

    async def resume(self, msg: ControlMessage) -> Outcome:
        """Lift the halt and any pause in one ``DEL``, unless a refusal applies (spec 0010 AC-4).

        Not halted: it still clears any pause. Halted: refused while not flat, while the loss
        figures are unknown, or while the total loss is at or over its limit. Entry blocks stay.
        """
        state = self._gate.halt_state
        if state is not None:
            refusal = self._resume_refusal()
            if refusal is not None:
                log.warning(
                    "resume refused",
                    extra={"fields": {"code": "resume_refused", "reason": refusal, "id": str(msg.id)}},
                )
                return Outcome(ControlOutcome.FAILED, refusal)
        try:
            await self._client.delete(self._key, self._pause_key)
        except (RedisError, OSError, TimeoutError) as exc:
            # Nothing changed: the keys would halt and pause the trader again on its next start.
            log.error(
                "halt and pause keys not deleted",
                extra={"fields": {"code": "halt_key_delete_failed", "error": type(exc).__name__}},
            )
            return Outcome(ControlOutcome.FAILED, "halt_key_delete_failed")
        self._gate.unhalt()
        self._gate.unpause()
        self._key_written = False
        apply_trading_state(self._gate, self._risk_engine)
        self._event(
            f"resume:{msg.id}",
            RiskEventKind.RESUME,
            Severity.INFO,
            "trader resumed" if state is not None else "trader resumed (was not halted)",
            {"source": msg.source.value, "halt_reason": None if state is None else state.reason.value},
        )
        return Outcome(ControlOutcome.EXECUTED)

    def _resume_refusal(self) -> str | None:
        if self.flattening or self._cache.positions_open():
            return "not_flat"
        if self._losses is None:
            return None
        if not self._losses.seeded:
            return "loss_unknown"
        if self._losses.total_over_limit:
            return "total_loss_limit"
        return None

    def flatten_after_restart(self) -> None:
        """Flatten again once the strategies run, for a trader that started halted (spec 0010 AC-7)."""
        if not self._gate.halted:
            return
        flatten_id = uuid.uuid7()
        log.info(
            "flattening after a halted start", extra={"fields": {"code": "restart_flatten", "id": str(flatten_id)}}
        )
        self._flatten_all(flatten_id)
        self._spawn(self._wait_flat(flatten_id, self._now()))

    def halt_automatic(self, reason: HaltReason) -> uuid.UUID:
        """Halt for a loss limit (spec 0010 AC-9, AC-10): in memory now, then key, event, and flat wait as a task.

        Returns the new ``halt_id`` (a UUID7). There is no command to answer, so a timeout only
        writes its ``flatten_timeout`` event.
        """
        state = HaltState(reason, HaltSource.TRADER, self._now(), uuid.uuid7())
        newly = self._apply_halt(state)
        self._spawn(self._halt_automatic(state, newly))
        return state.halt_id

    async def _halt_automatic(self, state: HaltState, newly: bool) -> None:
        await self._after_halt(state, newly, flatten_id=state.halt_id)
        await self._wait_flat(state.halt_id, self._now())

    def _apply_halt(self, state: HaltState) -> bool:
        """Halt in memory and set the trading state, before any I/O; True when newly halted."""
        newly = self._gate.halt(state)
        apply_trading_state(self._gate, self._risk_engine)
        return newly

    async def _halt(self, state: HaltState, flatten_id: uuid.UUID) -> bool:
        """Halt in memory, then write the key while flattening; False when the key could not be written."""
        return await self._after_halt(state, self._apply_halt(state), flatten_id)

    async def _after_halt(self, state: HaltState, newly: bool, flatten_id: uuid.UUID) -> bool:
        current = self._gate.halt_state or state
        write = None if self._key_written else asyncio.ensure_future(self._write_key(current))
        self._flatten_all(flatten_id)  # before the write runs: a slow Redis never delays the close
        if newly:
            self._event(
                f"halt:{state.halt_id}",
                RiskEventKind.HALT,
                Severity.WARNING if state.reason is HaltReason.MANUAL_STOP else Severity.CRITICAL,
                f"trader halted: {state.reason.value}",
                {"reason": state.reason.value, "source": state.source.value},
            )
        if write is None:
            return True
        if await write:
            return True
        self._event(
            f"halt_key_write_failed:{current.halt_id}",
            RiskEventKind.HALT,
            Severity.CRITICAL,
            "halt key not written; a restart would come back running",
            {"reason": current.reason.value, "source": current.source.value},
        )
        return False

    async def _write_key(self, state: HaltState) -> bool:
        try:
            await self._client.set(self._key, halt.encode(state))
        except (RedisError, OSError, TimeoutError) as exc:
            log.error(
                "halt key not written",
                extra={"fields": {"code": "halt_key_write_failed", "error": type(exc).__name__}},
            )
            return False
        self._key_written = True
        return True

    def _flatten_all(self, flatten_id: uuid.UUID) -> None:
        for strategy in self._strategies:
            strategy.flatten(flatten_id)

    async def _wait_flat(self, flatten_id: uuid.UUID, started: datetime) -> bool:
        """Poll the cache until flat, re-flattening as positions appear; False after the timeout."""
        self._flattening += 1
        try:
            while not self.is_flat():
                if self._now() - started >= self._flat_timeout:
                    self._event(
                        f"flatten_timeout:{flatten_id}",
                        RiskEventKind.ORDER_REJECTED,
                        Severity.CRITICAL,
                        f"not flat {self._flat_timeout.total_seconds():g} s after the flatten started; exits retry",
                        {
                            "open_positions": len(self._cache.positions_open()),
                            "open_entry_orders": len(
                                entry_orders([*self._cache.orders_open(), *self._cache.orders_inflight()])
                            ),
                        },
                    )
                    return False
                await asyncio.sleep(self._poll_s)
                for strategy in self._strategies:
                    if self._cache.positions_open(strategy_id=strategy.id):
                        strategy.flatten(flatten_id)
            return True
        finally:
            self._flattening -= 1

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """The node's event loop, where the flat waits of automatic halts run (``ControlActor.on_start``)."""
        self._loop = loop

    def _spawn(self, work: Coroutine[object, object, object]) -> None:
        loop = self._loop or asyncio.get_running_loop()

        def start() -> None:
            task = loop.create_task(work)
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

        loop.call_soon_threadsafe(start)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.critical(
                "kill switch flat wait crashed",
                exc_info=task.exception(),
                extra={"fields": {"code": "kill_switch_task_crashed"}},
            )

    def _event(self, key: str, kind: RiskEventKind, severity: Severity, message: str, details: JsonObject) -> None:
        log.log(_LOG_LEVELS[severity], message, extra={"fields": {"code": kind.value, "event_key": key}})
        if self._record is None:
            return  # live: no run to record under yet (spec 0010 AC-7)
        row = RiskEventRow(
            env=self._env,
            event_key=key,
            kind=kind,
            severity=severity,
            message=message,
            details=details,
            run_id=self._run_id,
            occurred_ns=int(self._now().timestamp() * 1_000_000_000),
        )
        self._record(WriteItem(WriteCall.RECORD_RISK_EVENT, row))


def control_execute(pause: PauseController, kill_switch: KillSwitch) -> Execute:
    """The consumer's ``execute``: ``stop``, ``flatten``, and ``resume`` to the kill switch, the rest to the pause."""

    async def execute(msg: ControlMessage) -> Outcome:
        if msg.cmd is ControlCmd.STOP:
            return await kill_switch.stop(msg)
        if msg.cmd is ControlCmd.FLATTEN:
            return await kill_switch.flatten(msg)
        if msg.cmd is ControlCmd.RESUME:
            return await kill_switch.resume(msg)
        return await pause.execute(msg)

    return execute


class ControlActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Halts and pauses at start when the keys say so, then reads control commands until stopped.

    The halt key is read first, then the pause key, and the trading state is applied once. A
    trader that starts halted flattens again 5 s later, once its strategies run (spec 0010 AC-7).
    """

    def __init__(
        self,
        controller: PauseController,
        kill_switch: KillSwitch,
        read_pause_key: Callable[[], str | bytes | None],
        read_halt_key: Callable[[], str | bytes | None],
        consume: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(ActorConfig(component_id="ControlActor"))
        self._controller = controller
        self._kill_switch = kill_switch
        self._read_pause_key = read_pause_key
        self._read_halt_key = read_halt_key
        self._consume = consume
        self._task: asyncio.Task[None] | None = None

    def on_start(self) -> None:
        try:
            raw_halt = self._read_halt_key()
        except (RedisError, OSError, TimeoutError) as exc:
            log.critical(
                "halt key unreadable, starting halted",
                extra={"fields": {"code": "halt_key_invalid", "error": type(exc).__name__}},
            )
            self._kill_switch.halt_unknown()
        else:
            self._kill_switch.load(raw_halt)
        try:
            raw = self._read_pause_key()
        except (RedisError, OSError, TimeoutError) as exc:
            log.critical(
                "pause key unreadable, starting paused",
                extra={"fields": {"code": "pause_key_unreadable", "error": type(exc).__name__}},
            )
            self._controller.pause_unknown()
        else:
            load_pause(self._controller, raw)
        loop = asyncio.get_running_loop()
        self._kill_switch.bind_loop(loop)
        if self._controller.halted:
            # Through the loop: the flat wait it starts is a task on the node's event loop.
            self.clock.set_time_alert(
                _RESTART_FLATTEN_ALERT,
                self.clock.utc_now() + RESTART_FLATTEN_DELAY,
                lambda _event: loop.call_soon_threadsafe(self._kill_switch.flatten_after_restart),
            )
        self._task = loop.create_task(self._run())
        self._task.add_done_callback(_consumer_ended)

    def on_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        await self._consume()


def _consumer_ended(task: asyncio.Task[None]) -> None:
    if task.cancelled() or task.exception() is None:
        return
    log.critical(
        "control consumer crashed, control commands no longer work",
        exc_info=task.exception(),
        extra={"fields": {"code": "control_consumer_crashed"}},
    )


def protective_stops(orders: Iterable[Order]) -> int:
    """How many of ``orders`` are reduce only stop orders, each counted once."""
    seen = {order.client_order_id for order in orders if order.is_reduce_only and order.order_type in PROTECTIVE_TYPES}
    return len(seen)


@dataclass(frozen=True)
class PaperStatus:
    """The paper loop's part of ``trader_state`` (spec 0006 AC-16)."""

    entries_blocked: list[str]
    recording_backlog: int
    exiting: bool
    position_opened_ns: int | None


def trader_state(
    gate: PauseGate,
    risk_engine: RiskEngine,
    cache: Cache,
    git_commit: str,
    paper: Callable[[], PaperStatus] | None = None,
) -> Mapping[str, object] | None:
    """The ``trader_state`` snapshot data (AC-7) from the gate, the risk engine, and the cache.

    None until the pause key was read at start, so the trader never reports running before it knows.
    With ``paper`` (demo) it adds the paper loop's status; without one the same fields hold only
    the pause, so the dashboard reads one shape in every env (spec 0006 AC-16).
    """
    if not gate.loaded:
        return None
    orders = [*cache.orders_open(), *cache.orders_inflight()]
    state = gate.state
    status = (
        paper()
        if paper is not None
        else PaperStatus(
            entries_blocked=entries_blocked(EntryBlocks(), gate.paused, gate.halted),
            recording_backlog=0,
            exiting=False,
            position_opened_ns=None,
        )
    )
    return {
        "trading_state": TradingState(risk_engine.trading_state).name,
        "paused": gate.paused,
        "pause_source": state.source.value if state is not None else None,
        "paused_at": format_utc(state.paused_at) if state is not None else None,
        "open_positions": len(cache.positions_open()),
        "open_entry_orders": len(entry_orders(orders)),
        "protective_stops": protective_stops(orders),
        "git_commit": git_commit,
        "entries_blocked": status.entries_blocked,
        "recording_backlog": status.recording_backlog,
        "exiting": status.exiting,
        "position_opened_ns": status.position_opened_ns,
    }


def shutdown_counts(gate: PauseGate, cache: Cache) -> Mapping[str, int]:
    """The ``shutdown_graceful`` counts, taken after the strategies stopped (AC-9)."""
    orders = [*cache.orders_open(), *cache.orders_inflight()]
    return {
        "cancelled": gate.cancelled,
        "unacknowledged_cancels": len(entry_orders(orders)),
        "positions_kept": len(cache.positions_open()),
        "stops_kept": protective_stops(orders),
    }
