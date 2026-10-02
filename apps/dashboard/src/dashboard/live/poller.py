"""The one Redis poller per dashboard process (spec 0004, Live poller).

While at least one SSE subscriber exists it reads the trader heartbeat every
``DASHBOARD_POLL_MS``, renders the top bar partials, and publishes each to every subscriber
only when its HTML changed. With no subscriber it idles. A Redis error renders ``link lost``
and the next tick tries again. Milestone 2 adds the command state partial.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from redis.exceptions import RedisError

from dashboard.live.state import LINK_LOST, AgeView, heartbeat_view

log = logging.getLogger(__name__)

AGE_EVENT: Final = "topbar-age"
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
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_heartbeat = read_heartbeat
        self._render_age = render_age
        self._interval_s = interval_s
        self._amber_s = amber_s
        self._red_s = red_s
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._wake = asyncio.Event()
        self._cache: dict[str, str] = {}
        self._last_skew_warning: float | None = None

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
        return {AGE_EVENT: self._render_age(await self.age_view())}

    async def age_view(self) -> AgeView:
        """The data age right now; ``link lost`` when Redis fails or times out."""
        try:
            raw = await self._read_heartbeat()
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning(
                "redis read failed", extra={"fields": {"code": "redis_read_failed", "error": type(exc).__name__}}
            )
            return LINK_LOST
        view = heartbeat_view(raw, self._clock_ns(), self._amber_s, self._red_s)
        if view.skewed:
            self._warn_skew()
        return view

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
