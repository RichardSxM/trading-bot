"""Container log lines into observations (spec 0013 Build plan 2.1, *Replay safety*).

Every 5 s the reader asks Docker for each service's lines since its cursor. The cursor (the last
Docker timestamp read) lives in Redis under ``{env}:alerts:cursor:<service>`` for 7 days, so a
restart picks up where it left off; without one the read starts 5 min back. A line at or before
the cursor is dropped (lines sharing a nanosecond arrive in the same response, so none is lost),
and a set of recently seen line keys (service, Docker timestamp, line hash) kept 30 min drops the
overlap after a lost cursor, so no line is ever observed twice.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from redis.exceptions import RedisError

from alerter.sources.docker_api import (
    Container,
    DockerApi,
    DockerApiError,
    ns_to_datetime,
    ns_to_rfc3339,
    ns_to_since,
    rfc3339_ns,
)
from trading_core.alerts.model import Observation

log = logging.getLogger(__name__)

CURSOR_TTL_S: Final = 7 * 24 * 3600
LOOKBACK_NS: Final = 5 * 60 * 1_000_000_000
REPLAY_KEEP_NS: Final = 30 * 60 * 1_000_000_000
# One shot services and the alerter's own plumbing are never read.
SKIPPED: Final = frozenset({"migrate", "tools", "lab", "dashboard-credentials", "docker-proxy", "alerter"})
_HEADER: Final = 8


class CursorStore(Protocol):
    """The two Redis calls the cursors need (``redis.asyncio.Redis`` satisfies it)."""

    def get(self, name: str) -> Awaitable[bytes | str | None]: ...

    def set(self, name: str, value: str, ex: int | None = None) -> Awaitable[object]: ...


def demux(raw: bytes) -> list[str]:
    """The text lines of a logs response.

    A container without a TTY (all of ours) answers in frames: one byte for the stream, three
    zero bytes, a 4 byte big endian length, then the payload. Anything else is read as plain text.
    """
    if not _framed(raw):
        return raw.decode(errors="replace").splitlines()
    chunks = []
    at = 0
    while at + _HEADER <= len(raw):
        size = int.from_bytes(raw[at + 4 : at + _HEADER], "big")
        chunks.append(raw[at + _HEADER : at + _HEADER + size])
        at += _HEADER + size
    return b"".join(chunks).decode(errors="replace").splitlines()


def _framed(raw: bytes) -> bool:
    return len(raw) >= _HEADER and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00"


@dataclass(frozen=True)
class Line:
    """One timestamped log line."""

    ts_ns: int
    text: str

    def key(self, service: str) -> tuple[str, int, str]:
        """The replay key: service, Docker time, and the line's hash."""
        return (service, self.ts_ns, hashlib.sha256(self.text.encode()).hexdigest())


def split_line(raw: str) -> Line | None:
    """``<RFC 3339 time> <text>`` into a ``Line``; None when the line has no Docker timestamp."""
    stamp, _, text = raw.partition(" ")
    try:
        return Line(rfc3339_ns(stamp), text)
    except ValueError:
        return None


def observe(service: str, line: Line) -> Observation:
    """A line as an ``Observation``: a JSON object gives ``code``, ``level``, and its fields;
    any other line (a NautilusTrader engine line, a traceback) keeps only its text."""
    at = ns_to_datetime(line.ts_ns)
    source = f"log:{service}"
    try:
        body = json.loads(line.text)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return Observation(source, service, at, code=None, level=None, text=line.text)
    code, level = body.get("code"), body.get("level")
    return Observation(
        source,
        service,
        at,
        code=code if isinstance(code, str) else None,
        level=level if isinstance(level, str) else None,
        fields=body,
    )


class LogReader:
    """Reads every project container's new lines; ``ok_at`` says when a full pass last succeeded."""

    def __init__(
        self,
        api: DockerApi,
        cursors: CursorStore,
        env: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._api = api
        self._cursors = cursors
        self._env = env
        self._clock = clock
        self._positions: dict[str, int] = {}
        # Services whose position moved on but whose Redis cursor write failed; retried next pass.
        self._unsaved: set[str] = set()
        self._seen: dict[tuple[str, int, str], int] = {}
        self.ok_at: datetime | None = None

    def cursor_key(self, service: str) -> str:
        """The Redis key of a service's cursor."""
        return f"{self._env}:alerts:cursor:{service}"

    async def poll(self, containers: Iterable[Container]) -> list[Observation]:
        """New lines of every readable container, oldest first per service (AC-3).

        Every line read is returned, even when another container's read or a cursor write failed
        in the same pass, so a critical line is never lost to someone else's error. A failed pass
        leaves ``ok_at`` as it was, so the watchdog stops pinging once it goes stale (AC-2).
        """
        now_ns = _ns(self._clock())
        found: list[Observation] = []
        failed = False
        for container in containers:
            if container.service in SKIPPED:
                continue
            error: DockerApiError | RedisError | None
            try:
                lines, error = await self._read(container, now_ns)
                found += lines
            except (DockerApiError, RedisError) as exc:
                error = exc
            if error is not None:
                log.warning(
                    "log read failed",
                    extra={
                        "fields": {"code": "alerter_log_read_failed", "service": container.service, "error": str(error)}
                    },
                )
                failed = True
        self._forget(now_ns)
        if not failed:
            self.ok_at = self._clock()
        return found

    async def _read(self, container: Container, now_ns: int) -> tuple[list[Observation], RedisError | None]:
        """A container's new lines, and the cursor write's error when it failed.

        Raises a Docker or Redis error only before anything is marked seen, so the next pass
        reads the same lines again.
        """
        service = container.service
        cursor = await self._position(service, now_ns)
        raw = await self._api.logs(container.id, ns_to_since(cursor))
        first = service not in self._positions
        found = []
        latest = cursor
        for text in demux(raw):
            line = split_line(text)
            if line is None or line.ts_ns <= cursor:
                continue
            key = line.key(service)
            if key in self._seen:
                continue
            self._seen[key] = now_ns
            latest = max(latest, line.ts_ns)
            found.append(observe(service, line))
        self._positions[service] = latest
        if latest == cursor and not first and service not in self._unsaved:
            return found, None
        # The position in memory has moved on; the Redis copy only matters after a restart, so a
        # failed write keeps the lines and is tried again on the next pass.
        try:
            await self._cursors.set(self.cursor_key(service), ns_to_rfc3339(latest), ex=CURSOR_TTL_S)
        except RedisError as exc:
            self._unsaved.add(service)
            return found, exc
        self._unsaved.discard(service)
        return found, None

    async def _position(self, service: str, now_ns: int) -> int:
        known = self._positions.get(service)
        if known is not None:
            return known
        stored = await self._cursors.get(self.cursor_key(service))
        text = stored.decode() if isinstance(stored, bytes) else stored
        try:
            return rfc3339_ns(text) if text else now_ns - LOOKBACK_NS
        except ValueError:
            return now_ns - LOOKBACK_NS

    def _forget(self, now_ns: int) -> None:
        cutoff = now_ns - REPLAY_KEEP_NS
        for key in [k for k, seen in self._seen.items() if seen < cutoff]:
            del self._seen[key]


def _ns(at: datetime) -> int:
    return int(at.timestamp()) * 1_000_000_000 + at.microsecond * 1000
