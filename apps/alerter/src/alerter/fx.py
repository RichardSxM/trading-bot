"""The daily ECB rate fetch (spec 0024 AC-15): fills ``fx_rates`` from the ECB Data Portal.

At start and then every hour, whenever the newest stored rate is before today (Amsterdam), it
asks for the days after it, or from ``FX_HISTORY_FROM`` when the table is empty. A failed fetch
just waits for the next hour. A 404 is the API's answer for a period with no rates (a weekend,
a TARGET holiday, today before about 16:00 CET), so it writes and logs nothing.
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
    ) -> None:
        self._http = http
        self._store = store
        self._base_url = base_url
        self._history_from = history_from
        self._clock = clock

    async def step(self) -> None:
        """Fetch and store the missing days, if any; every failure is logged and left for the next hour."""
        today = amsterdam_today(self._clock())
        try:
            newest = await asyncio.to_thread(self._store.newest)
        except SQLAlchemyError as exc:
            _failed("database", error=type(exc).__name__)
            return
        start = self._history_from if newest is None else newest + timedelta(days=1)
        if start > today:
            return
        rows = await self._fetch(start, today)
        if not rows:
            return
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
            return
        except SQLAlchemyError as exc:
            _failed("database", error=type(exc).__name__)
            return
        log.info(
            "fx rates fetched",
            extra={"fields": {"code": "fx_fetched", "count": inserted, "newest": rows[-1].rate_date.isoformat()}},
        )

    async def _fetch(self, start: date, end: date) -> list[FxRateRow]:
        """The parsed rows for ``start`` to ``end``; empty on a 404 or any logged failure."""
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
        try:
            return parse_ecb_csv(response.text, start, end)
        except FxParseError as exc:
            _failed("parse", error=exc.reason)
            return []


def _failed(reason: str, **fields: object) -> None:
    log.warning("fx fetch failed", extra={"fields": {"code": "fx_fetch_failed", "reason": reason, **fields}})
