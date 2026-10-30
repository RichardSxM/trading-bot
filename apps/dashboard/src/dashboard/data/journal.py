"""The journal export's Postgres session (spec 0024 AC-11, AC-14), as ``dashboard_app``.

Each export opens its own session from the factory, never a request dependency, because the
stream outlives the request handler. The session runs one ``REPEATABLE READ`` read only
transaction: the refusal pass and the streaming pass see the same snapshot.
"""

from collections.abc import Callable, Iterator
from typing import Protocol

from sqlalchemy.orm import Session

from trading_core.journal.bounds import JournalRange
from trading_core.recording.enums import Env
from trading_core.recording.journal_export import JournalCsv, journal_refusal

SNAPSHOT_OPTIONS = {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}


class JournalExport(Protocol):
    """An export that passed its checks: ``chunks()`` yields ``journal.csv``, ``rows`` counts what it wrote."""

    @property
    def rows(self) -> int: ...

    def chunks(self) -> Iterator[str]: ...


class PostgresJournalExport:
    """The export over its own open session; streaming closes the session at the end or on an error."""

    def __init__(self, session: Session, csv: JournalCsv) -> None:
        self._session = session
        self._csv = csv

    @property
    def rows(self) -> int:
        """Rows written so far."""
        return self._csv.rows

    def chunks(self) -> Iterator[str]:
        """``journal.csv`` one month at a time; a database error here aborts the response."""
        try:
            yield from self._csv
        finally:
            self._session.close()


def open_journal(sessions: Callable[[], Session], env: str, journal_range: JournalRange) -> JournalExport:
    """Open the snapshot and run the refusal pass.

    Raises ``JournalRefusedError`` (or a database error) before any byte is sent; the session is then closed.
    """
    session = sessions()
    try:
        session.connection(execution_options=SNAPSHOT_OPTIONS)
        refusal = journal_refusal(session, Env(env), journal_range)
    except BaseException:
        session.close()
        raise
    if refusal is not None:
        session.close()
        raise refusal
    return PostgresJournalExport(session, JournalCsv(session, Env(env), journal_range))
