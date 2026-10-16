"""The trader's heartbeat in Redis (spec 0013 AC-1, *Value sourcing*)."""

import logging
from collections.abc import Awaitable
from typing import Protocol

from trading_core.snapshot.envelope import SnapshotFormatError, decode

log = logging.getLogger(__name__)


class SnapshotReader(Protocol):
    """The one Redis call the heartbeat read needs (``redis.asyncio.Redis`` satisfies it)."""

    def get(self, name: str) -> Awaitable[bytes | str | None]: ...


async def heartbeat_ts(redis: SnapshotReader, key: str) -> int | None:
    """The heartbeat's ``ts_ns``, or ``None`` when the key is missing or not a valid envelope.

    A Redis error propagates: while Redis is unreachable the trader's state is unknown, so the
    caller raises no ``trader_down`` (AC-1).
    """
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        return decode(raw).ts_ns
    except SnapshotFormatError as exc:
        log.warning("bad heartbeat snapshot", extra={"fields": {"code": "heartbeat_malformed", "error": str(exc)}})
        return None
