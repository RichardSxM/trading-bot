"""The trader's heartbeat and entry blocks in Redis (spec 0013 AC-1, AC-6, *Value sourcing*)."""

import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Protocol

from trading_core.snapshot.envelope import SnapshotFormatError, decode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraderBlocks:
    """The entry block part of ``trader_state``: when it was written, the reasons, and their since."""

    ts_ns: int
    blocked: tuple[str, ...]
    since_ns: Mapping[str, int]


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


async def trader_blocks(redis: SnapshotReader, key: str) -> TraderBlocks | None:
    """``entries_blocked`` and ``entries_blocked_since`` of ``trader_state``; None when missing or malformed.

    Bad entries inside the fields are skipped. A Redis error propagates.
    """
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        snapshot = decode(raw)
    except SnapshotFormatError as exc:
        log.warning(
            "bad trader_state snapshot", extra={"fields": {"code": "trader_state_malformed", "error": str(exc)}}
        )
        return None
    blocked = snapshot.data.get("entries_blocked")
    since = snapshot.data.get("entries_blocked_since")
    reasons = tuple(r for r in blocked if isinstance(r, str)) if isinstance(blocked, list) else ()
    since_ns = (
        {r: ns for r, ns in since.items() if isinstance(r, str) and isinstance(ns, int)}
        if isinstance(since, dict)
        else {}
    )
    return TraderBlocks(snapshot.ts_ns, reasons, since_ns)
