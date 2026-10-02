"""The one Redis poller per dashboard process (spec 0004, Live poller).

While at least one SSE subscriber exists it reads the trader heartbeat and the latest control
command (plus its results until one is final) every ``DASHBOARD_POLL_MS``, renders the top
bar partials, and publishes each to every subscriber only when its HTML changed. With no
subscriber it idles. A Redis error renders both partials ``link lost`` and the next tick
tries again.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Final, Protocol

from redis.exceptions import RedisError

from dashboard.live.state import (
    COMMAND_LINK_LOST,
    LINK_LOST,
    AgeView,
    CommandView,
    Entry,
    command_state,
    find_result,
    heartbeat_view,
)
from trading_core.control.messages import ControlMessage, Fields, field, to_fields

log = logging.getLogger(__name__)

AGE_EVENT: Final = "topbar-age"
COMMAND_EVENT: Final = "topbar-command"
REDIS_ERRORS: Final = (RedisError, OSError, TimeoutError)
QUEUE_SIZE: Final = 8
RESTART_AFTER_S: Final = 1.0
SKEW_WARN_EVERY_S: Final = 60.0

Event = tuple[str, str]


class Poller:
    """Reads live values, renders them, and fans changes out to subscriber queues."""

    def __init__(
        self,
        read_heartbeat: Callable[[], Awaitable[str | bytes | None]],
        render_age: Callable[[AgeView], str],
        interval_s: float,
        amber_s: float,
        red_s: float,
        *,
        read_command: Callable[[], Awaitable[Sequence[Entry] | None]],
        read_results: Callable[[], Awaitable[Sequence[Entry] | None]],
        render_command: Callable[[CommandView], str],
        hide_after_s: float,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_heartbeat = read_heartbeat
        self._render_age = render_age
        self._read_command = read_command
        self._read_results = read_results
        self._render_command = render_command
        self._hide_after_s = hide_after_s
        self._interval_s = interval_s
        self._amber_s = amber_s
        self._red_s = red_s
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._wake = asyncio.Event()
        self._cache: dict[str, str] = {}
        self._last_skew_warning: float | None = None
        # The stream id of a command whose result is final, and that result: no need to read again.
        self._final: tuple[str | bytes | None, Fields] | None = None
        self._warned_invalid: tuple[object, ...] | None = None

    @property
    def cached(self) -> dict[str, str]:
        """The latest rendered partial per event name."""
        return dict(self._cache)

    def subscribe(self) -> asyncio.Queue[Event]:
        """A new bounded queue that receives every changed partial; wakes an idle poller."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        self._wake.set()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Stop sending to ``queue``."""
        self._subscribers.discard(queue)

    async def render_now(self) -> dict[str, str]:
        """Read Redis and render every partial without publishing (for a full page render)."""
        try:
            raw = await self._read_heartbeat()
            command = await self.command_view()
        except REDIS_ERRORS as exc:
            log.warning(
                "redis read failed", extra={"fields": {"code": "redis_read_failed", "error": type(exc).__name__}}
            )
            return {AGE_EVENT: self._render_age(LINK_LOST), COMMAND_EVENT: self._render_command(COMMAND_LINK_LOST)}
        age = heartbeat_view(raw, self._clock_ns(), self._amber_s, self._red_s)
        if age.skewed:
            self._warn_skew()
        return {AGE_EVENT: self._render_age(age), COMMAND_EVENT: self._render_command(command)}

    async def command_view(self) -> CommandView:
        """The latest command and its outcome; reads results only until one is final (AC-12)."""
        latest = await self._read_command()
        if not latest:
            return command_state(None, None, self._now(), self._hide_after_s)
        stream_id, raw_command = latest[0]
        command: Fields = raw_command if raw_command is not None else {}  # a deleted entry parses as unknown
        if self._final is not None and self._final[0] == stream_id:
            result: Fields | None = self._final[1]
        else:
            command_id = field(command, "id")
            result = None if command_id is None else find_result(await self._read_results(), command_id)
        view = command_state(command, result, self._now(), self._hide_after_s)
        if view.final and result is not None:
            self._final = (stream_id, result)
        if view.invalid:
            self._warn_invalid((stream_id, None if result is None else tuple(result.items())))
        return view

    def note_sent(self, msg: ControlMessage) -> None:
        """Show a command the dashboard just appended at once, in the cache and to every subscriber."""
        view = command_state(to_fields(msg), None, self._now(), self._hide_after_s)
        html = self._render_command(view)
        if self._cache.get(COMMAND_EVENT) != html:
            self._cache[COMMAND_EVENT] = html
            self._publish((COMMAND_EVENT, html))

    async def tick(self) -> None:
        """One poll: render each partial and publish the ones whose HTML changed."""
        for name, html in (await self.render_now()).items():
            if self._cache.get(name) != html:
                self._cache[name] = html
                self._publish((name, html))

    async def run(self) -> None:
        """Poll while anyone listens, idle otherwise; runs until cancelled."""
        while True:
            if not self._subscribers:
                self._wake.clear()
                await self._wake.wait()
            await self.tick()
            await asyncio.sleep(self._interval_s)

    def _publish(self, event: Event) -> None:
        for queue in self._subscribers:
            if queue.full():  # a slow browser loses the oldest update, never blocks the others
                queue.get_nowait()
            queue.put_nowait(event)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock_ns() / 1_000_000_000, tz=UTC)

    def _warn_invalid(self, key: tuple[object, ...]) -> None:
        if key != self._warned_invalid:
            self._warned_invalid = key
            log.warning(
                "control entry does not parse, showing unknown result",
                extra={"fields": {"code": "control_result_invalid"}},
            )

    def _warn_skew(self) -> None:
        now = self._monotonic()
        if self._last_skew_warning is None or now - self._last_skew_warning >= SKEW_WARN_EVERY_S:
            self._last_skew_warning = now
            log.warning("heartbeat is from the future, showing 0s", extra={"fields": {"code": "clock_skew"}})


class Runnable(Protocol):
    """Anything with a long running ``run`` coroutine (the ``Poller``)."""

    async def run(self) -> None: ...


async def supervise(poller: Runnable, restart_after_s: float = RESTART_AFTER_S) -> None:
    """Run ``poller`` forever; a crash logs ERROR ``poller_crashed`` and restarts it after a pause."""
    while True:
        try:
            await poller.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poller crashed, restarting", extra={"fields": {"code": "poller_crashed"}})
            await asyncio.sleep(restart_after_s)
