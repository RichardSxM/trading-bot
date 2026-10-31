"""The daily ECB rate fetch (spec 0024 AC-15): fills ``fx_rates`` from the ECB Data Portal.

At start and then every hour, whenever the newest stored rate is before today (Amsterdam), it
asks for the days after it, or from ``FX_HISTORY_FROM`` when the table is empty. A failed fetch
just waits for the next hour. A 404 is the API's answer for a period with no rates (a weekend,
a TARGET holiday, today before about 16:00 CET), so it writes and logs nothing. Each step then
judges ``fx_rates_stale`` (AC-17) from the newest date it knows.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Final, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading_core.alerts.checks import fx_stale_check
from trading_core.alerts.model import CheckResult
from trading_core.fx.ecb import FxParseError, ecb_url, parse_ecb_csv
from trading_core.recording import repository
from trading_core.recording.errors import RecordConflictError
from trading_core.recording.queries import newest_fx_date
from trading_core.recording.rows import FxRateRow

log = logging.getLogger(__name__)

FX_S: Final = 3600.0
TIMEOUT_S: Final = 20.0
AMSTERDAM: Final = ZoneInfo("Europe/Amsterdam")


class FxStore(Protocol):
    """``FxRepository``'s calls (blocking; run in a thread)."""

    def newest(self) -> date | None: ...

    def write(self, rows: Sequence[FxRateRow]) -> int: ...


class FxRepository:
    """``fx_rates`` as ``alerter_app``: the newest date, and idempotent inserts."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def newest(self) -> date | None:
        """The newest stored EUR/USD rate date."""
        with self._sessions() as session:
            return newest_fx_date(session)

    def write(self, rows: Sequence[FxRateRow]) -> int:
        """Insert ``rows`` in one transaction; returns how many were new. A conflict writes none."""
        with self._sessions() as session, session.begin():
            return repository.write_fx_rates(session, rows)


def amsterdam_today(now: datetime) -> date:
    """Today's calendar date in Europe/Amsterdam."""
    return now.astimezone(AMSTERDAM).date()


class FxFetcher:
    """One fetch step; the alerter runs it every ``FX_S`` seconds."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        store: FxStore,
        base_url: str,
        history_from: date,
        clock: Callable[[], datetime],
        stale_days: int = 6,
    ) -> None:
        self._http = http
        self._store = store
        self._base_url = base_url
        self._history_from = history_from
        self._clock = clock
        self._stale_days = stale_days

    async def step(self) -> CheckResult | None:
        """Fetch and store the missing days, if any; every failure is logged and left for the next hour.

        Returns the ``fx_rates_stale`` check of the newest date after the fetch, or None when the
        newest date could not be read (``postgres_unreachable`` covers that).
        """
        now = self._clock()
        today = amsterdam_today(now)
        try:
            newest = await asyncio.to_thread(self._store.newest)
        except SQLAlchemyError as exc:
            _failed("database", error=type(exc).__name__)
            return None
        newest = await self._fill(newest, today)
        return fx_stale_check(newest, today, self._stale_days, now)

    async def _fill(self, newest: date | None, today: date) -> date | None:
        """Store the days after ``newest`` up to ``today``; returns the newest date stored afterwards."""
        start = self._history_from if newest is None else newest + timedelta(days=1)
        if start > today:
            return newest
        rows = await self._fetch(start, today)
        if not rows:
            return newest
        try:
            inserted = await asyncio.to_thread(self._store.write, rows)
        except RecordConflictError as exc:
            log.error(
                "fx rate conflict, stored rate kept",
                extra={
                    "fields": {
                        "code": "fx_rate_conflict",
                        "rate_date": str(exc.key.get("rate_date")),
                        "stored_rate": str(exc.stored.get("rate")),
                        "fetched_rate": str(exc.incoming.get("rate")),
                    }
                },
            )
            return newest
        except SQLAlchemyError as exc:
            _failed("database", error=type(exc).__name__)
            return newest
        log.info(
            "fx rates fetched",
            extra={"fields": {"code": "fx_fetched", "count": inserted, "newest": rows[-1].rate_date.isoformat()}},
        )
        return rows[-1].rate_date

    async def _fetch(self, start: date, end: date) -> list[FxRateRow]:
        """The parsed rows for ``start`` to ``end``; empty on a 404, an empty 200, or any logged failure."""
        try:
            response = await self._http.get(ecb_url(self._base_url, start, end), timeout=TIMEOUT_S)
        except httpx.HTTPError as exc:
            _failed("http", error=type(exc).__name__)
            return []
        if response.status_code == httpx.codes.NOT_FOUND:
            return []
        if response.status_code != httpx.codes.OK:
            _failed("status", status=response.status_code)
            return []
        if not response.text.strip():  # the ECB also answers a period with no observations with an empty 200
            return []
        try:
            return parse_ecb_csv(response.text, start, end)
        except FxParseError as exc:
            _failed("parse", error=exc.reason)
            return []


def _failed(reason: str, **fields: object) -> None:
    log.warning("fx fetch failed", extra={"fields": {"code": "fx_fetch_failed", "reason": reason, **fields}})
