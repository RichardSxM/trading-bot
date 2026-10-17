"""The alerter's reader of ``{env}:control:alerts`` and its mute key (spec 0013 AC-12, AC-16, API surface).

``AlertCommands.run`` reads the stream in consumer group ``alerter`` (its own pending entries
first), and for each entry: decodes it (a bad one gets ``failed invalid``), answers an expired
one with ``expired``, otherwise runs it through ``execute`` and appends the outcome to
``{env}:control:alerts:results``, then acknowledges the entry. A test runs beside the reader (its
pushes can take minutes while Pushover fails), so a mute behind it is not kept waiting. A Redis
error is logged at most once a minute and the read retries every second.

``MuteKey`` keeps ``{env}:alerts:mute`` (``until`` and ``command_id``, with the mute's length as
its TTL), so a restarted alerter picks the mute up again.
"""

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final, Protocol

from redis.exceptions import RedisError, ResponseError
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT

from trading_core.control.alerts import (
    AlertCommand,
    AlertResult,
    alerts_results_stream,
    alerts_stream,
    command_from_fields,
    result_to_fields,
)
from trading_core.control.consumer import ControlRedis, stream_entries
from trading_core.control.messages import ControlFormatError, Fields, field, format_utc
from trading_core.recording.enums import AlertCmd, ControlOutcome
from trading_core.settings import TradingEnv

log = logging.getLogger(__name__)

GROUP: Final = "alerter"
CONSUMER: Final = "alerter"
BLOCK_MS: Final = 1000
READ_COUNT: Final = 10
RESULTS_MAXLEN: Final = 1000
RETRY_S: Final = 1.0
WARN_EVERY_S: Final = 60.0

Execute = Callable[[AlertCommand], Awaitable[AlertResult]]


class AlertCommands:
    """Reads ``{env}:control:alerts`` as group ``alerter`` and answers on ``{env}:control:alerts:results``."""

    def __init__(
        self,
        client: ControlRedis,
        env: TradingEnv,
        execute: Execute,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        retry_s: float = RETRY_S,
    ) -> None:
        self._client = client
        self._stream = alerts_stream(env)
        self._results = alerts_results_stream(env)
        self._execute = execute
        self._now = now
        self._monotonic = monotonic
        self._retry_s = retry_s
        self._last_warning: float | None = None
        self._group_ready = False
        self._pending = True
        # Tests still running beside the reader, by entry id, so a pending read skips them.
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

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
            try:
                await self._client.xgroup_create(self._stream, GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._group_ready = True
        start = "0" if self._pending else ">"
        streams: dict[KeyT, StreamIdT] = {self._stream: start}
        reply = await self._client.xreadgroup(
            GROUP, CONSUMER, streams, count=READ_COUNT, block=None if self._pending else BLOCK_MS
        )
        entries = [(i, f) for i, f in stream_entries(reply) if i not in self._running]
        if self._pending and not entries:
            self._pending = False
        for entry_id, fields in entries:
            await self._handle(entry_id, fields)
        return len(entries)

    async def drain(self) -> None:
        """Wait for the tests still running beside the reader."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def _handle(self, entry_id: str, fields: Fields) -> None:
        try:
            msg = command_from_fields(fields)
        except ControlFormatError as exc:
            log.warning(
                "alert command invalid",
                extra={"fields": {"code": "alert_command_invalid", "entry_id": entry_id, "error": str(exc)}},
            )
            command_id = _uuid_or_none(field(fields, "id"))
            if command_id is not None:
                await self._append(
                    AlertResult(command_id, ControlOutcome.FAILED, self._now(), _cmd_or_none(fields), "invalid")
                )
            await self._client.xack(self._stream, GROUP, entry_id)
            return
        now = self._now()
        if msg.expires_at <= now:
            await self._answer(entry_id, msg, AlertResult(msg.id, ControlOutcome.EXPIRED, now, msg.cmd))
        elif msg.cmd is AlertCmd.TEST:
            self._running.add(entry_id)
            task = asyncio.create_task(self._test(entry_id, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._answer(entry_id, msg, await self._run(msg))

    async def _test(self, entry_id: str, msg: AlertCommand) -> None:
        try:
            await self._answer(entry_id, msg, await self._run(msg))
        except (RedisError, OSError, TimeoutError) as exc:
            # Not acknowledged: the next pending read finds it again.
            self._redis_failed(exc)
        finally:
            self._running.discard(entry_id)

    async def _answer(self, entry_id: str, msg: AlertCommand, result: AlertResult) -> None:
        log.info(
            "alert command done",
            extra={
                "fields": {
                    "code": "alert_command_done",
                    "id": str(msg.id),
                    "cmd": msg.cmd.value,
                    "outcome": result.outcome.value,
                    "reason": result.reason,
                }
            },
        )
        await self._append(result)
        await self._client.xack(self._stream, GROUP, entry_id)

    async def _run(self, msg: AlertCommand) -> AlertResult:
        try:
            return await self._execute(msg)
        except RedisError, OSError, TimeoutError:
            raise
        except Exception:
            log.exception(
                "alert command failed",
                extra={"fields": {"code": "alert_command_failed", "id": str(msg.id), "cmd": msg.cmd.value}},
            )
            return AlertResult(msg.id, ControlOutcome.FAILED, self._now(), msg.cmd, "error")

    async def _append(self, result: AlertResult) -> None:
        fields: dict[FieldT, EncodableT] = {name: value for name, value in result_to_fields(result).items()}
        await self._client.xadd(self._results, fields, maxlen=RESULTS_MAXLEN, approximate=True)

    def _redis_failed(self, exc: BaseException) -> None:
        # The next read starts with the pending entries again, so nothing read is lost.
        self._pending = True
        now = self._monotonic()
        if self._last_warning is None or now - self._last_warning >= WARN_EVERY_S:
            self._last_warning = now
            log.warning(
                "alert command read failed",
                extra={"fields": {"code": "alert_command_read_failed", "error": type(exc).__name__}},
            )


class MuteRedis(Protocol):
    """The Redis calls the mute key needs (``redis.asyncio.Redis`` satisfies it)."""

    def get(self, name: str) -> Awaitable[bytes | str | None]: ...

    def set(self, name: str, value: str, ex: int | None = None) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[object]: ...


class MuteKey:
    """``{env}:alerts:mute``: written by the alerter on a mute, deleted on an unmute."""

    def __init__(self, client: MuteRedis, env: TradingEnv) -> None:
        self._client = client
        self.key = f"{env}:alerts:mute"

    async def set(self, until: datetime, command_id: uuid.UUID, now: datetime) -> None:
        """Store the mute until ``until``; the key expires then too."""
        ttl = max(1, math.ceil((until - now).total_seconds()))
        value = json.dumps({"until": format_utc(until), "command_id": str(command_id)})
        await self._client.set(self.key, value, ex=ttl)

    async def clear(self) -> None:
        """Delete the mute."""
        await self._client.delete(self.key)

    async def load(self) -> tuple[datetime, uuid.UUID] | None:
        """The stored mute's ``until`` and command id; None when there is none or it does not parse."""
        raw = await self._client.get(self.key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            until = datetime.fromisoformat(value["until"])
            command_id = uuid.UUID(value["command_id"])
        except ValueError, KeyError, TypeError:
            log.warning("bad mute key", extra={"fields": {"code": "alert_mute_malformed"}})
            return None
        if until.tzinfo is None:
            return None
        return until, command_id


def _uuid_or_none(text: str | None) -> uuid.UUID | None:
    if text is None:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _cmd_or_none(fields: Fields) -> AlertCmd | None:
    text = field(fields, "cmd")
    return AlertCmd(text) if text in {c.value for c in AlertCmd} else None
