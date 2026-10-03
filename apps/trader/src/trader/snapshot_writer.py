"""Writes the trader's heartbeat snapshot to Redis every second (spec 0004 AC-8).

The dashboard turns its ``ts_ns`` into the data age in the top bar. A Redis outage never
stops the trader: the writer logs and keeps trying, at most one warning a minute.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Final, Protocol

from redis.exceptions import RedisError

from trading_core.snapshot.envelope import encode

log = logging.getLogger(__name__)

# A dead trader's key disappears after this, so the dashboard shows `no heartbeat`.
HEARTBEAT_TTL_S: Final = 30
WRITE_INTERVAL_S: Final = 1.0
WARN_EVERY_S: Final = 60.0


class SnapshotClient(Protocol):
    """The one Redis call the writer needs (redis.asyncio.Redis satisfies it)."""

    def set(self, name: str, value: str, *, ex: int) -> Awaitable[object]: ...


async def write_heartbeats(
    client: SnapshotClient,
    key: str,
    read_state: Callable[[], Mapping[str, object] | None],
    interval_s: float = WRITE_INTERVAL_S,
    clock_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Write ``read_state()`` as a snapshot to ``key`` every ``interval_s`` until cancelled.

    A None state is not known yet, so nothing is written that round.
    """
    last_warning: float | None = None
    while True:
        state = read_state()
        try:
            if state is not None:
                await client.set(key, encode(clock_ns(), state), ex=HEARTBEAT_TTL_S)
        except (RedisError, OSError) as exc:
            now = monotonic()
            if last_warning is None or now - last_warning >= WARN_EVERY_S:
                last_warning = now
                log.warning(
                    "snapshot write failed",
                    extra={"fields": {"code": "snapshot_write_failed", "key": key, "error": type(exc).__name__}},
                )
        await asyncio.sleep(interval_s)


def log_writer_crash(task: asyncio.Task[None]) -> None:
    """Done callback: a writer that raised logs CRITICAL; the trader keeps trading."""
    if task.cancelled() or task.exception() is None:
        return
    log.critical(
        "snapshot writer crashed, the dashboard will show no heartbeat",
        exc_info=task.exception(),
        extra={"fields": {"code": "snapshot_writer_crashed"}},
    )
