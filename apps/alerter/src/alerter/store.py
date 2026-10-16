"""Episode writes off the delivery path (spec 0013 AC-15): a bounded queue drained into Postgres.

A push never waits for Postgres. Each changed episode is queued under its identity, so a newer
state replaces an older one still waiting. While Postgres is down the queue holds up to 1,000
episodes; past that the oldest info episode goes first, then the oldest of any tier. Nothing
is written before the stored episodes are loaded, so a restart can never insert a second open
row for a fingerprint the database already holds.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy.exc import SQLAlchemyError

from trading_core.alerts.engine import AlertEngine, EpisodeKey, key_of
from trading_core.alerts.model import Episode, Tier
from trading_core.recording.errors import RecordingError

log = logging.getLogger(__name__)

QUEUE_MAX: Final = 1000
RETRY_S: Final = 5.0
# Failed episodes older than this are not loaded for a retry (AC-14).
FAILED_MAX_AGE: Final = timedelta(hours=24)


class EpisodeStore(Protocol):
    """``AlertRepository``'s two calls (blocking; run in a thread)."""

    def save(self, episode: Episode) -> int: ...

    def load_active(self, failed_since: datetime) -> list[Episode]: ...


class StoreWriter:
    """Loads the stored episodes into the engine once, then writes every queued episode."""

    def __init__(
        self,
        store: EpisodeStore,
        engine: AlertEngine,
        clock: Callable[[], datetime],
        *,
        retry_s: float = RETRY_S,
    ) -> None:
        self._store = store
        self._engine = engine
        self._clock = clock
        self._retry_s = retry_s
        self._queue: OrderedDict[EpisodeKey, Episode] = OrderedDict()
        self._wake = asyncio.Event()
        self.loaded = False

    def put(self, episode: Episode) -> None:
        """Queue the latest state of ``episode``; never blocks."""
        key = key_of(episode)
        self._queue[key] = episode
        self._queue.move_to_end(key)
        while len(self._queue) > QUEUE_MAX:
            self._drop_one()
        self._wake.set()

    @property
    def pending(self) -> int:
        """How many episodes wait to be written."""
        return len(self._queue)

    async def run(self) -> None:
        """Load, then drain the queue forever, waiting ``retry_s`` after a database error."""
        while not self.loaded:
            if not await self.load_once():
                await asyncio.sleep(self._retry_s)
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                if not await self.flush_one():
                    await asyncio.sleep(self._retry_s)

    async def load_once(self) -> bool:
        """Load open and recent failed episodes into the engine; False when Postgres is unavailable."""
        now = self._clock()
        try:
            stored = await asyncio.to_thread(self._store.load_active, now - FAILED_MAX_AGE)
        except SQLAlchemyError as exc:
            log.warning("cannot load alerts yet", extra={"fields": {"code": "alerts_load_failed", "error": str(exc)}})
            return False
        current = {e.fingerprint: e for e in self._engine.open_episodes}
        # An open row whose fingerprint opened again in memory while Postgres was away is stale.
        stale = [
            e for e in stored if e.is_open and e.fingerprint in current and key_of(current[e.fingerprint]) != key_of(e)
        ]
        self._engine.load(e for e in stored if e not in stale)
        for episode in stale:
            self.put(replace(episode, resolved_at=now))
        self.loaded = True
        log.info(
            "alerts loaded",
            extra={"fields": {"code": "alerts_loaded", "episodes": len(stored), "closed_stale": len(stale)}},
        )
        return True

    async def flush_one(self) -> bool:
        """Write the oldest queued episode; False (kept queued) when Postgres is unavailable."""
        key, episode = next(iter(self._queue.items()))
        try:
            await asyncio.to_thread(self._store.save, episode)
        except SQLAlchemyError as exc:
            log.warning("cannot store alert yet", extra={"fields": {"code": "alert_store_failed", "error": str(exc)}})
            return False
        except RecordingError as exc:
            log.error(
                "alert refused by the store",
                extra={
                    "fields": {"code": "alert_store_refused", "fingerprint": episode.fingerprint, "error": str(exc)}
                },
            )
        # Only drop what was written: a newer state queued meanwhile stays.
        if self._queue.get(key) is episode:
            del self._queue[key]
        return True

    def _drop_one(self) -> None:
        victim = next((k for k, e in self._queue.items() if e.severity is Tier.INFO), next(iter(self._queue)))
        dropped = self._queue.pop(victim)
        log.warning(
            "alert queue full, dropping one",
            extra={"fields": {"code": "alert_queue_full", "fingerprint": dropped.fingerprint}},
        )
