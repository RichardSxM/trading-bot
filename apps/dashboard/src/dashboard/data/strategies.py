"""The Strategies pages (spec 0015 AC-18 to AC-21), read from Postgres as ``dashboard_app`` when requested."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from trading_core.recording.gate_queries import (
    PaperProgress,
    PromotionLine,
    StudyDetail,
    StudyRanking,
    paper_progress,
    promotion_log,
    study_detail,
    study_rankings,
)


@dataclass(frozen=True)
class StrategiesView:
    """The ranking, and every version in paper with its progress against the paper → live checks."""

    ranking: Sequence[StudyRanking]
    paper: Sequence[PaperProgress] = ()


def strategies_view(sessions: Callable[[], Session], now: datetime) -> StrategiesView:
    """Every study ranked, and paper progress computed now; the queries live in ``gate_queries``."""
    with sessions() as session:
        return StrategiesView(study_rankings(session), paper_progress(session, now))


def study_view(sessions: Callable[[], Session], study_id: UUID) -> StudyDetail | None:
    """One study in depth, or None when unknown."""
    with sessions() as session:
        return study_detail(session, study_id)


def promotions_view(sessions: Callable[[], Session], page: int) -> Sequence[PromotionLine]:
    """One page of the promotion log, newest first."""
    with sessions() as session:
        return promotion_log(session, page)
