"""The journal export's passes over Postgres (spec 0024 AC-5 to AC-11, AC-14), one Amsterdam month at a time.

``journal_refusal`` reads every chunk and says why nothing may be exported, if anything.
``JournalCsv`` then yields ``journal.csv`` one chunk of text per month. ``journal_summary`` runs
the same refusal check in one pass that also sums the totals, then reads the balances and the
positions for the check panel and ``summary.csv``. Run each request's passes in one
``REPEATABLE READ`` transaction so they see the same rows (AC-11); the caller owns it.
"""

import io
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from trading_core.journal.bounds import JournalRange, month_chunks
from trading_core.journal.export import JournalRefusedError, RateCheck, write_header, write_rows
from trading_core.journal.summary import JournalSummary, JournalTotals, summarize
from trading_core.recording.enums import Env
from trading_core.recording.errors import UnsupportedInstrumentError
from trading_core.recording.queries import (
    JournalRow,
    balances_at,
    fx_rate_on,
    journal_check_positions,
    journal_rows,
    newest_fx_date,
)


def _chunk(
    session: Session, env: Env, journal_range: JournalRange, start: datetime, end: datetime
) -> Sequence[JournalRow]:
    return journal_rows(session, env, start, end, {journal_range.mode})


def _checked_pass(
    session: Session, env: Env, journal_range: JournalRange, feed: Callable[[Iterable[JournalRow]], None] | None = None
) -> JournalRefusedError | None:
    """Read every chunk through the rate check; ``feed`` gets each chunk while nothing has refused yet."""
    check = RateCheck(newest_fx_date(session), journal_range)
    for start, end in month_chunks(journal_range):
        try:
            rows = _chunk(session, env, journal_range, start, end)
        except UnsupportedInstrumentError as exc:
            return JournalRefusedError("inverse_instrument", str(exc))
        check.feed(rows)
        if feed is not None and check.refusal() is None:
            feed(rows)
    return check.refusal()


def journal_refusal(session: Session, env: Env, journal_range: JournalRange) -> JournalRefusedError | None:
    """Why the range may not be exported (a missing or unpublished rate, an inverse instrument), or None."""
    return _checked_pass(session, env, journal_range)


def journal_summary(session: Session, env: Env, journal_range: JournalRange) -> JournalSummary:
    """Totals, balances at both bounds, and the P&L check of the range (AC-7 to AC-9).

    Raises ``JournalRefusedError`` when the range may not be exported, exactly as ``journal_refusal`` says.
    """
    totals = JournalTotals()
    refusal = _checked_pass(session, env, journal_range, totals.feed)
    if refusal is not None:
        raise refusal
    modes = {journal_range.mode}
    bounds = [
        (balances_at(session, env, modes, at), fx_rate_on(session, at.date()))
        for at in (journal_range.start_utc, journal_range.end_utc)
    ]
    positions = journal_check_positions(session, env, modes, journal_range.start_utc, journal_range.end_utc)
    return summarize(journal_range, totals, bounds[0], bounds[1], positions)


class JournalCsv:
    """``journal.csv`` as text chunks: the header, then one chunk per month; ``rows`` counts as it goes."""

    def __init__(self, session: Session, env: Env, journal_range: JournalRange) -> None:
        self._session = session
        self._env = env
        self._range = journal_range
        self.rows = 0

    def __iter__(self) -> Iterator[str]:
        buffer = io.StringIO()
        write_header(buffer)
        yield buffer.getvalue()
        for start, end in month_chunks(self._range):
            buffer = io.StringIO()
            self.rows += write_rows(buffer, self._env, _chunk(self._session, self._env, self._range, start, end))
            text = buffer.getvalue()
            if text:
                yield text
