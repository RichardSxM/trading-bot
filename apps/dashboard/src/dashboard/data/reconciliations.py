"""The Reconciliation page list (spec 0023 AC-19), read from Postgres as ``dashboard_app``.

The query itself lives in ``trading_core.recording.queries``.
"""

from collections.abc import Callable, Sequence

from sqlalchemy.orm import Session

from trading_core.recording.enums import Env
from trading_core.recording.queries import ReconciliationLine, reconciliations_page


def reconciliations_view(sessions: Callable[[], Session], env: str, page: int) -> Sequence[ReconciliationLine]:
    """Page ``page`` (from 1) of this env's reported checks, newest first, each with its findings."""
    with sessions() as session:
        return reconciliations_page(session, Env(env), page)
