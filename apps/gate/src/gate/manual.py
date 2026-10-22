"""``gate versions`` and ``gate promote --manual`` (spec 0015 AC-15).

A manual promotion is only ever ``candidate → paper``: it needs a live terminal, a note, and the
strategy's name typed back, and it is always logged. The repository refuses any manual promotion to
live (``ManualLivePromotionError``), and a CHECK backs that up. A version promoted only by hand has no
study, so it can reach paper but never live.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import DecidedBy, Stage, Verdict
from trading_core.recording.gate_queries import stage_of
from trading_core.recording.rows import PromotionRow


class ManualPromotionRefused(Exception):
    """Why ``gate promote --manual`` did nothing."""


@dataclass(frozen=True)
class VersionLine:
    """One row of ``gate versions``."""

    version_id: UUID
    strategy: str
    git_commit: str
    frozen_hash: str | None
    stage: str
    created_at: datetime


def versions(session: Session, strategy: str | None = None) -> list[VersionLine]:
    """Every strategy version (of one strategy, when named), newest first, with its derived stage."""
    query = select(models.StrategyVersion, models.Strategy.name).join(
        models.Strategy, models.Strategy.id == models.StrategyVersion.strategy_id
    )
    if strategy is not None:
        query = query.where(models.Strategy.name == strategy)
    rows = session.execute(query.order_by(models.StrategyVersion.created_at.desc())).all()
    return [
        VersionLine(
            version_id=v.id,
            strategy=name,
            git_commit=v.git_commit,
            frozen_hash=v.frozen_hash,
            stage="unfrozen" if v.frozen_hash is None else stage_of(session, v.frozen_hash),
            created_at=v.created_at,
        )
        for v, name in rows
    ]


def promote_manual(
    session: Session,
    version_id: UUID,
    note: str,
    *,
    is_tty: bool,
    confirm: Callable[[str], str],
    clock: Callable[[], datetime],
) -> UUID:
    """Append a manual ``candidate → paper`` pass for the version; returns the promotion id.

    ``confirm`` asks for the strategy's name (the prompt text is passed in); ``clock`` is read once the
    name is typed, so a slow answer never backdates the row. Raises
    ``ManualPromotionRefused`` without a terminal, with an empty note, for an unknown or unfrozen
    version, or when the typed name does not match.
    """
    if not is_tty:
        raise ManualPromotionRefused("a manual promotion needs a terminal (run with -it, not -T)")
    if not note.strip():
        raise ManualPromotionRefused("--note must say why")
    version = session.get(models.StrategyVersion, version_id)
    if version is None:
        raise ManualPromotionRefused(f"no strategy version {version_id}")
    if version.frozen_hash is None:
        raise ManualPromotionRefused(f"version {version_id} has no frozen hash; the paper guard could never match it")
    name = session.execute(select(models.Strategy.name).where(models.Strategy.id == version.strategy_id)).scalar_one()
    typed = confirm(
        f"Promote {name} version {version_id} (frozen {version.frozen_hash[:12]}) to paper by hand? "
        f"Type the strategy name to confirm: "
    )
    if typed.strip() != name:
        raise ManualPromotionRefused("the typed name does not match; nothing was written")
    row = PromotionRow(
        strategy_version_id=version.id,
        from_stage=Stage.CANDIDATE,
        to_stage=Stage.PAPER,
        verdict=Verdict.PASS,
        thresholds={},
        decided_by=DecidedBy.MANUAL,
        notes=note.strip(),
        decided_at=clock(),
        frozen_hash=version.frozen_hash,
    )
    repository.record_promotion(session, row, [])
    return row.id
