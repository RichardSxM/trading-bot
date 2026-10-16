"""The trader's reader of the ``{env}:control`` stream (spec 0005 AC-6). No NautilusTrader import.

``ControlConsumer.run`` reads the stream in consumer group ``trader`` (its own pending entries
first), and for each entry: decodes it (a bad one gets ``failed``), answers a repeated ``id``
with ``duplicate``, an expired one with ``expired``, otherwise appends ``received``, runs the
command through ``execute``, and appends the final outcome to ``{env}:control:results``. It
records each command in Postgres through a ``CommandRecorder`` and acknowledges the entry last.

A Postgres error is logged and never blocks a command or its result. A Redis error is logged at
most once a minute and the read retries every second; the trading state stays as it was.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from redis.exceptions import RedisError, ResponseError
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT

from trading_core.control.messages import (
    ControlFormatError,
    ControlMessage,
    ControlResult,
    Fields,
    control_stream,
    field,
    from_fields,
    result_to_fields,
    results_stream,
)
from trading_core.recording.enums import ControlOutcome
from trading_core.settings import TradingEnv

log = logging.getLogger(__name__)

GROUP: Final = "trader"
CONSUMER: Final = "trader"
# Above the 1 s blocking read, so a slow reply is not cut short (the client's socket_timeout).
BLOCK_MS: Final = 1000
READ_COUNT: Final = 10
RESULTS_MAXLEN: Final = 1000
RETRY_S: Final = 1.0
WARN_EVERY_S: Final = 60.0
# Ids this process finished, kept so a re-read entry gets its result again, not `duplicate`.
FINISHED_KEPT: Final = 1000


@dataclass(frozen=True)
class Outcome:
    """What executing a command did: ``executed`` or ``failed`` with a ``reason``."""

    outcome: ControlOutcome
    reason: str | None = None


Execute = Callable[[ControlMessage], Awaitable[Outcome]]


class ControlRedis(Protocol):
    """The Redis calls the consumer needs (redis.asyncio.Redis satisfies it)."""

    def xgroup_create(self, name: str, groupname: str, id: str, mkstream: bool) -> Awaitable[object]: ...

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> Awaitable[object]: ...

    def xadd(
        self,
        name: KeyT,
        fields: dict[FieldT, EncodableT],
        id: StreamIdT = "*",
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> Awaitable[object]: ...

    def xack(self, name: str, groupname: str, *ids: str) -> Awaitable[object]: ...


class CommandRecorder(Protocol):
    """Where the trader records control commands (Postgres ``control_commands`` in the node)."""

    async def known(self, command_id: uuid.UUID) -> bool:
        """True when ``command_id`` was recorded before."""
        ...

    async def received(self, msg: ControlMessage, at: datetime) -> None:
        """Record ``msg`` as received at ``at``."""
        ...

    async def finished(self, result: ControlResult) -> None:
        """Move the recorded command to its final outcome."""
        ...


class ControlConsumer:
    """Reads ``{env}:control`` as group ``trader`` and answers on ``{env}:control:results``."""

    def __init__(
        self,
        client: ControlRedis,
        env: TradingEnv,
        execute: Execute,
        recorder: CommandRecorder,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        retry_s: float = RETRY_S,
    ) -> None:
        self._client = client
        self._stream = control_stream(env)
        self._results = results_stream(env)
        self._execute = execute
        self._recorder = recorder
        self._now = now
        self._monotonic = monotonic
        self._retry_s = retry_s
        self._finished: dict[uuid.UUID, ControlResult] = {}
        self._last_warning: float | None = None
        self._group_ready = False
        self._pending = True

    async def run(self) -> None:
        """Read and handle entries until cancelled."""
        while True:
            try:
                await self.step()
            except (RedisError, OSError, TimeoutError) as exc:
                self._redis_failed(exc)
                await asyncio.sleep(self._retry_s)

    async def step(self) -> int:
        """One read: the pending entries while any are left, then new ones. Returns how many it handled."""
        if not self._group_ready:
            await self._create_group()
        start = "0" if self._pending else ">"
        streams: dict[KeyT, StreamIdT] = {self._stream: start}
        reply = await self._client.xreadgroup(
            GROUP, CONSUMER, streams, count=READ_COUNT, block=None if self._pending else BLOCK_MS
        )
        entries = stream_entries(reply)
        if self._pending and not entries:
            self._pending = False
        for entry_id, fields in entries:
            await self._handle(entry_id, fields)
        return len(entries)

    async def _create_group(self) -> None:
        try:
            await self._client.xgroup_create(self._stream, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def _handle(self, entry_id: str, fields: Fields) -> None:
        try:
            msg = from_fields(fields)
        except ControlFormatError as exc:
            await self._invalid(entry_id, fields, exc)
            return
        done = self._finished.get(msg.id)
        if done is not None:
            # Handled by this process but not acknowledged (Redis failed): answer the same again.
            await self._append(done)
        elif await self._known(msg.id):
            await self._append(ControlResult(msg.id, ControlOutcome.DUPLICATE, self._now()))
        else:
            await self._new(msg)
        await self._client.xack(self._stream, GROUP, entry_id)

    async def _new(self, msg: ControlMessage) -> None:
        received_at = self._now()
        await self._record(self._recorder.received(msg, received_at), msg.id)
        if msg.expires_at <= received_at:
            await self._finish(ControlResult(msg.id, ControlOutcome.EXPIRED, received_at))
            return
        await self._append(ControlResult(msg.id, ControlOutcome.RECEIVED, received_at))
        try:
            outcome = await self._execute(msg)
        except RedisError, OSError, TimeoutError:
            raise
        except Exception:
            # Fails closed: the executor changes the trading state before anything that can fail.
            log.exception(
                "control command failed",
                extra={"fields": {"code": "control_execute_failed", "id": str(msg.id), "cmd": msg.cmd.value}},
            )
            outcome = Outcome(ControlOutcome.FAILED, "error")
        await self._finish(ControlResult(msg.id, outcome.outcome, self._now(), outcome.reason))

    async def _finish(self, result: ControlResult) -> None:
        self._remember(result)
        await self._append(result)
        await self._record(self._recorder.finished(result), result.id)

    async def _invalid(self, entry_id: str, fields: Fields, exc: ControlFormatError) -> None:
        log.warning(
            "control entry invalid",
            extra={"fields": {"code": "control_entry_invalid", "entry_id": entry_id, "error": str(exc)}},
        )
        command_id = _uuid_or_none(field(fields, "id"))
        if command_id is not None:
            await self._append(ControlResult(command_id, ControlOutcome.FAILED, self._now(), "invalid"))
        await self._client.xack(self._stream, GROUP, entry_id)

    async def _known(self, command_id: uuid.UUID) -> bool:
        try:
            return await self._recorder.known(command_id)
        except Exception as exc:
            _record_failed(exc, command_id)
            return False

    async def _record(self, write: Awaitable[None], command_id: uuid.UUID) -> None:
        try:
            await write
        except Exception as exc:
            _record_failed(exc, command_id)

    async def _append(self, result: ControlResult) -> None:
        fields: dict[FieldT, EncodableT] = {name: value for name, value in result_to_fields(result).items()}
        await self._client.xadd(self._results, fields, maxlen=RESULTS_MAXLEN, approximate=True)

    def _remember(self, result: ControlResult) -> None:
        self._finished[result.id] = result
        while len(self._finished) > FINISHED_KEPT:
            del self._finished[next(iter(self._finished))]

    def _redis_failed(self, exc: BaseException) -> None:
        # The next read starts with the pending entries again, so nothing read is lost.
        self._pending = True
        now = self._monotonic()
        if self._last_warning is None or now - self._last_warning >= WARN_EVERY_S:
            self._last_warning = now
            log.warning(
                "control read failed",
                extra={"fields": {"code": "control_read_failed", "error": type(exc).__name__}},
            )


def _record_failed(exc: BaseException, command_id: uuid.UUID) -> None:
    log.error(
        "control command not recorded",
        extra={"fields": {"code": "control_record_failed", "id": str(command_id), "error": type(exc).__name__}},
    )


def _uuid_or_none(text: str | None) -> uuid.UUID | None:
    if text is None:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, str):
        return value
    raise RedisError(f"unexpected stream reply value {type(value).__name__}")


def stream_entries(reply: object) -> list[tuple[str, Fields]]:
    """The ``(entry id, fields)`` pairs of an XREADGROUP reply (RESP2 list or RESP3 dict, one stream)."""
    if reply is None:
        return []
    streams: Sequence[object]
    if isinstance(reply, Mapping):
        streams = [[name, entries] for name, entries in reply.items()]
    elif isinstance(reply, list):
        streams = reply
    else:
        raise RedisError(f"unexpected XREADGROUP reply {type(reply).__name__}")
    found: list[tuple[str, Fields]] = []
    for stream in streams:
        if not isinstance(stream, list | tuple) or len(stream) != 2:
            raise RedisError("unexpected XREADGROUP stream shape")
        entries = stream[1]
        if not isinstance(entries, list):
            raise RedisError("unexpected XREADGROUP entries shape")
        for entry in entries:
            if not isinstance(entry, list | tuple) or len(entry) != 2:
                raise RedisError("unexpected XREADGROUP entry shape")
            entry_id, fields = entry
            # A pending entry deleted from the stream (trimmed) comes back with no fields.
            if not isinstance(fields, Mapping):
                found.append((_text(entry_id), {}))
                continue
            found.append((_text(entry_id), fields))
    return found
