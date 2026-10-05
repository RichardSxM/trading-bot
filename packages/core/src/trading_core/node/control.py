"""The trader's control side in NautilusTrader (spec 0005 AC-6, AC-7, AC-9).

``ControlActor`` is registered before any strategy. At start it reads the pause key and pauses
when it is there, then runs the ``ControlConsumer`` on the node's event loop. ``PauseController``
executes ``pause`` and ``unpause``: it sets the ``PauseGate`` and the risk engine's trading state
(``REDUCING`` or ``ACTIVE``), and writes or deletes ``{env}:trader:pause``. ``stop``, ``resume``,
and ``flatten`` answer ``failed`` with ``not_implemented`` until feature 9.

``trader_state`` and ``shutdown_counts`` read the node's cache for the snapshot writer and the
``shutdown_graceful`` log line.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.enums import OrderType, TradingState
from nautilus_trader.model.orders.base import Order
from redis.exceptions import RedisError

from trading_core.control.consumer import Outcome
from trading_core.control.messages import ControlMessage, format_utc
from trading_core.control.pause import PauseFormatError, PauseState, decode, encode, pause_key, target_trading_state
from trading_core.node.strategy import PauseGate, entry_orders
from trading_core.recording.enums import ControlCmd, ControlOutcome
from trading_core.settings import TradingEnv

log = logging.getLogger(__name__)

PROTECTIVE_TYPES: Final = (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)


class RiskEngine(Protocol):
    """The risk engine calls the controller needs (NautilusTrader's ``RiskEngine``)."""

    @property
    def trading_state(self) -> TradingState: ...

    def set_trading_state(self, state: TradingState) -> None: ...


class PauseRedis(Protocol):
    """The pause key writes (redis.asyncio.Redis satisfies it)."""

    def set(self, name: str, value: str) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[object]: ...


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
        self._set_trading_state(state is not None)

    def pause_unknown(self) -> None:
        """Pause in memory when the pause key cannot be read at start (fail closed)."""
        self._gate.pause_in_memory()
        self._set_trading_state(paused=True)

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

    def _set_trading_state(self, paused: bool) -> None:
        self._risk_engine.set_trading_state(TradingState[target_trading_state(paused)])


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


class ControlActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Pauses at start when the key says so, then reads control commands until stopped."""

    def __init__(
        self,
        controller: PauseController,
        read_pause_key: Callable[[], str | bytes | None],
        consume: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(ActorConfig(component_id="ControlActor"))
        self._controller = controller
        self._read_pause_key = read_pause_key
        self._consume = consume
        self._task: asyncio.Task[None] | None = None

    def on_start(self) -> None:
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
        self._task = asyncio.get_running_loop().create_task(self._run())
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
        "control consumer crashed, pause and unpause no longer work",
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
            entries_blocked=["paused"] if gate.paused else [],
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
