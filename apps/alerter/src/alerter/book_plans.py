"""``book_plan_stale``'s Postgres read (spec 0033 AC-7): a book run's start and its newest ``book_plans`` row."""

from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from trading_core.recording.enums import Env
from trading_core.recording.queries import BookPlanAges, book_plan_ages


class BookPlanStore(Protocol):
    """``BookPlanRepository``'s call (blocking; run in a thread)."""

    def ages(self, env: Env, run_id: UUID) -> BookPlanAges: ...


class BookPlanRepository:
    """``book_plans`` and ``strategy_runs`` as ``alerter_app``, read only."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def ages(self, env: Env, run_id: UUID) -> BookPlanAges:
        """When ``run_id`` started and when its newest book plan was rebalanced."""
        with self._sessions() as session:
            return book_plan_ages(session, env, run_id)
