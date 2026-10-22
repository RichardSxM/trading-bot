"""``gate evaluate <study>`` (spec 0015 AC-8): one promotion per call, with its evidence and folds.

Holds the study's advisory lock, reads the study through ``gate_queries``, runs the pure gate, and
appends a ``candidate → paper`` promotion decided by ``gate``. The evaluation time comes from the
caller, never from inside ``trading_core.gate``, and is read once the lock is held, so an evaluation
that waited never gets an older time than the one that committed first.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from trading_core.gate.checks import MetricValue
from trading_core.gate.paper import evaluate_paper
from trading_core.gate.verdict import GateResult, evaluate_candidate
from trading_core.recording import models, repository
from trading_core.recording.enums import DecidedBy, Stage, Verdict
from trading_core.recording.gate_queries import UnknownStudyError, paper_evidence, study_evidence
from trading_core.recording.rows import JsonValue, PromotionFoldRow, PromotionRow


class NothingToEvaluateError(Exception):
    """The study has no run of a frozen version of its strategy, so there is no version to decide on."""


@dataclass(frozen=True)
class Evaluation:
    """What one ``gate evaluate`` wrote."""

    promotion_id: UUID
    result: GateResult


def _json(metrics: dict[str, MetricValue]) -> dict[str, JsonValue]:
    return dict(metrics)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _decided_elsewhere(session: Session, frozen_hash: str, study_id: UUID) -> bool:
    """Whether a promotion of ``frozen_hash`` came from another study or by hand."""
    return (
        session.execute(
            select(models.Promotion.id)
            .where(
                models.Promotion.frozen_hash == frozen_hash,
                models.Promotion.study_id.is_distinct_from(study_id),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def evaluate_candidate_step(
    engine: Engine, name: str, *, gate_commit: str, clock: Callable[[], datetime]
) -> Evaluation:
    """Evaluate the study called ``name`` and append its promotion; raises ``UnknownStudyError`` or
    ``NothingToEvaluateError``."""
    with Session(engine) as session:
        study_id = session.execute(select(models.Study.id).where(models.Study.name == name)).scalar_one_or_none()
    if study_id is None:
        raise UnknownStudyError(name)
    with repository.study_lock(engine, study_id), Session(engine) as session:
        now = clock()
        evidence = study_evidence(session, name)
        if evidence.final_version_id is None or evidence.final_frozen_hash is None:
            raise NothingToEvaluateError(f"study {name!r} has no run of a frozen version of its strategy yet")
        if not evidence.inputs.completed_holdouts() and _decided_elsewhere(
            session, evidence.final_frozen_hash, evidence.study.id
        ):
            # Its insufficient row would become the hash's latest decision and take away a pass
            # that another study (or a manual promotion) gave the same version.
            raise NothingToEvaluateError(
                f"study {name!r} has no completed holdout, and its version already has a decision "
                "from another study or by hand; run lab holdout first"
            )
        result = evaluate_candidate(evidence.inputs)
        row = PromotionRow(
            strategy_version_id=evidence.final_version_id,
            from_stage=Stage.CANDIDATE,
            to_stage=Stage.PAPER,
            verdict=Verdict(result.verdict.value),
            thresholds=evidence.study.thresholds,  # type: ignore[arg-type]  # JSONB object
            decided_by=DecidedBy.GATE,
            decided_at=now,
            frozen_hash=evidence.final_frozen_hash,
            study_id=evidence.study.id,
            thresholds_hash=evidence.study.thresholds_hash,
            reasons=[r.to_json() for r in result.reasons],
            metrics=_json(result.metrics),
            gate_commit=gate_commit,
        )
        folds = [
            PromotionFoldRow(
                promotion_id=row.id,
                fold=f.fold,
                is_start=date.fromisoformat(f.is_start),
                is_end=date.fromisoformat(f.is_end),
                oos_start=date.fromisoformat(f.oos_start),
                oos_end=date.fromisoformat(f.oos_end),
                chosen_grid_point=f.chosen_grid_point,
                is_sharpe=_decimal(f.is_sharpe),
                oos_sharpe=_decimal(f.oos_sharpe),
                oos_return=Decimal(f.oos_return),
                oos_trades=f.oos_trades,
                oos_net_pnl=Decimal(f.oos_net_pnl),
            )
            for f in result.folds
        ]
        repository.record_promotion(session, row, evidence.run_ids, folds)
        session.commit()
    return Evaluation(row.id, result)


def evaluate_paper_step(engine: Engine, frozen_hash: str, *, gate_commit: str, now: datetime) -> Evaluation:
    """Evaluate the paper record of ``frozen_hash`` and append a ``paper → live`` promotion (AC-16).

    Raises ``UnknownFrozenHashError`` when no version carries the hash. Evidence: the paper runs it read
    and the study's holdout run.
    """
    with Session(engine) as session:
        evidence = paper_evidence(session, frozen_hash, now)
        result = evaluate_paper(evidence.inputs)
        row = PromotionRow(
            strategy_version_id=evidence.version_id,
            from_stage=Stage.PAPER,
            to_stage=Stage.LIVE,
            verdict=Verdict(result.verdict.value),
            thresholds=evidence.thresholds,  # type: ignore[arg-type]  # JSON object
            decided_by=DecidedBy.GATE,
            decided_at=now,
            frozen_hash=frozen_hash,
            study_id=evidence.study_id,
            thresholds_hash=None,
            reasons=[r.to_json() for r in result.reasons],
            metrics=_json(result.metrics),
            gate_commit=gate_commit,
        )
        repository.record_promotion(session, row, evidence.run_ids)
        session.commit()
    return Evaluation(row.id, result)
