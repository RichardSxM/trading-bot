"""The journal export's two passes over Postgres (spec 0024 AC-5, AC-6, AC-14), one Amsterdam month at a time.

``journal_refusal`` reads every chunk and says why nothing may be exported, if anything.
``journal_csv`` then yields ``journal.csv`` one chunk of text per month. Run both in one
``REPEATABLE READ`` transaction so they see the same rows (AC-11); the caller owns it.
"""

import io
from collections.abc import Iterator, Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from trading_core.journal.bounds import JournalRange, month_chunks
from trading_core.journal.export import JournalRefusedError, RateCheck, write_header, write_rows
from trading_core.recording.enums import Env
from trading_core.recording.errors import UnsupportedInstrumentError
from trading_core.recording.queries import JournalRow, journal_rows, newest_fx_date


def _chunk(
    session: Session, env: Env, journal_range: JournalRange, start: datetime, end: datetime
) -> Sequence[JournalRow]:
    return journal_rows(session, env, start, end, {journal_range.mode})


def journal_refusal(session: Session, env: Env, journal_range: JournalRange) -> JournalRefusedError | None:
    """Why the range may not be exported (a missing or unpublished rate, an inverse instrument), or None."""
    check = RateCheck(newest_fx_date(session), journal_range)
    for start, end in month_chunks(journal_range):
        try:
            check.feed(_chunk(session, env, journal_range, start, end))
        except UnsupportedInstrumentError as exc:
            return JournalRefusedError("inverse_instrument", str(exc))
    return check.refusal()


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
