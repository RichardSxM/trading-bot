"""The one Redis poller per dashboard process (spec 0004, Live poller).

While at least one SSE subscriber exists it reads the trader heartbeat and the newest control
commands (plus their results until each is final) every ``DASHBOARD_POLL_MS``, renders the top
bar partials and Overview's Activity (AC-20), and publishes each to every subscriber only when
its HTML changed. With no subscriber it idles. A Redis error renders every partial ``link lost``
and the next tick tries again.
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
    ActivityRow,
    AgeView,
    CommandView,
    Entry,
    activity_row,
    command_state,
    find_result,
    heartbeat_view,
)
from trading_core.control.messages import ControlMessage, Fields, field, to_fields

log = logging.getLogger(__name__)

AGE_EVENT: Final = "topbar-age"
COMMAND_EVENT: Final = "topbar-command"
ACTIVITY_EVENT: Final = "overview-activity"
ACTIVITY_SIZE: Final = 20
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
        render_activity: Callable[[Sequence[ActivityRow] | None], str],
        hide_after_s: float,
        read_timeout_s: float,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_heartbeat = read_heartbeat
        self._read_timeout_s = read_timeout_s
        self._render_age = render_age
        self._read_command = read_command
        self._read_results = read_results
        self._render_command = render_command
        self._render_activity = render_activity
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
        # Stream id to final result for the commands in view: no need to read those results again.
        self._finals: dict[str | bytes | None, Fields] = {}
        self._warned_invalid: set[tuple[object, ...]] = set()

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
        """Read Redis and render every partial without publishing (for a full page render).

        All reads share one ``read_timeout_s`` deadline, so a down or hung Redis renders ``link lost``
        within it instead of waiting out redis-py's retries on every read (AC-13).
        """
        try:
            raw, (command, rows) = await asyncio.wait_for(self._read_all(), timeout=self._read_timeout_s)
        except REDIS_ERRORS as exc:
            log.warning(
                "redis read failed", extra={"fields": {"code": "redis_read_failed", "error": type(exc).__name__}}
            )
            return self.link_lost()
        age = heartbeat_view(raw, self._clock_ns(), self._amber_s, self._red_s)
        if age.skewed:
            self._warn_skew()
        return {
            AGE_EVENT: self._render_age(age),
            COMMAND_EVENT: self._render_command(command),
            ACTIVITY_EVENT: self._render_activity(rows),
        }

    def link_lost(self) -> dict[str, str]:
        """Every partial in its ``link lost`` state, without reading Redis."""
        return {
            AGE_EVENT: self._render_age(LINK_LOST),
            COMMAND_EVENT: self._render_command(COMMAND_LINK_LOST),
            ACTIVITY_EVENT: self._render_activity(None),
        }

    async def _read_all(self) -> tuple[str | bytes | None, tuple[CommandView, list[ActivityRow]]]:
        return await self._read_heartbeat(), await self._commands()

    async def command_view(self) -> CommandView:
        """The latest command and its outcome; reads results only until one is final (AC-12)."""
        return (await self._commands())[0]

    async def _commands(self) -> tuple[CommandView, list[ActivityRow]]:
        """The top bar state and the Activity rows from one read of each stream (AC-12, AC-20).

        Results are read only while some command in view has no final outcome yet.
        """
        now = self._now()
        entries = [
            (stream_id, raw if raw is not None else {})  # a deleted entry parses as unknown
            for stream_id, raw in (await self._read_command() or ())
        ]
        open_ids = [field(command, "id") for stream_id, command in entries if stream_id not in self._finals]
        results = await self._read_results() if any(open_ids) else None
        finals: dict[str | bytes | None, Fields] = {}
        rows: list[ActivityRow] = []
        view = command_state(None, None, now, self._hide_after_s)
        for index, (stream_id, command) in enumerate(entries):
            result = self._finals.get(stream_id)
            if result is None:
                command_id = field(command, "id")
                result = None if command_id is None else find_result(results, command_id)
            row = activity_row(command, result, now)
            if index == 0:
                view = command_state(command, result, now, self._hide_after_s)
            if row.final and result is not None:
                finals[stream_id] = result
            if row.invalid or (index == 0 and view.invalid):
                self._warn_invalid((stream_id, None if result is None else tuple(result.items())))
            rows.append(row)
        self._finals = finals
        return view, rows

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
        if key not in self._warned_invalid:
            if len(self._warned_invalid) >= 4 * ACTIVITY_SIZE:  # bounded; an old entry may warn again
                self._warned_invalid.clear()
            self._warned_invalid.add(key)
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
