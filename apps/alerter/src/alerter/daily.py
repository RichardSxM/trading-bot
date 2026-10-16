"""The daily note's loop step (spec 0013 AC-18), run every minute.

From ``ALERTS_DAILY_AT`` in ``ALERTS_DAILY_TZ`` until the end of that local day, the step sends the
note unless its ``alerts`` row (``daily:<local date>``) already exists; so a start after 08:00
catches up once. The row is written before the push goes out, so a crash in between can't send a
second note. The note needs Postgres for its numbers and its marker: while Postgres is
unavailable the step waits and tries again next minute.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime

from alerter.delivery import Outcome
from alerter.store import StoreWriter
from trading_core.alerts import daily
from trading_core.alerts.engine import AlertEngine
from trading_core.alerts.model import Push

log = logging.getLogger(__name__)


class DailyNote:
    """Sends one daily note per local day."""

    def __init__(
        self,
        *,
        engine: AlertEngine,
        store: StoreWriter,
        deliver: Callable[[Push], Awaitable[Outcome]],
        backup: Callable[[], Awaitable[str]],
        at: str,
        tz: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._engine = engine
        self._store = store
        self._deliver = deliver
        self._backup = backup
        self._at = at
        self._tz = tz
        self._clock = clock
        self._done: date | None = None

    async def step(self) -> None:
        """Send today's note when it is due and not yet sent; otherwise nothing."""
        now = self._clock()
        day = daily.due(now, self._at, self._tz)
        if day is None or day == self._done:
            return
        sent = await self._store.daily_sent(daily.fingerprint(day))
        if sent is None:
            return
        if sent:
            self._done = day
            return
        figures = await self._store.daily_figures(now)
        if figures is None:
            return
        figures = replace(figures, backup=await self._backup())
        decision = self._engine.daily_note(day, figures, now)
        if not all([await self._store.write_now(episode) for episode in decision.store]):
            return
        self._done = day
        log.info("daily note", extra={"fields": {"code": "daily_note", "day": day.isoformat()}})
        for push in decision.pushes:
            await self._deliver(push)
