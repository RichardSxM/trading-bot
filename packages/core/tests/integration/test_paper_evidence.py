"""covers: spec 0015 AC-16 (which paper runs count, the chained record, insufficient without a gate pass),
AC-21 (paper progress lists a version in paper)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from trading_core.gate.paper import evaluate_paper
from trading_core.gate.verdict import GateVerdict
from trading_core.recording import repository
from trading_core.recording.enums import DecidedBy, PositionSide, PositionStatus, Stage, Verdict
from trading_core.recording.gate_queries import paper_evidence, paper_progress
from trading_core.recording.rows import PromotionRow

from .conftest import WORLD_FROZEN, World

NOW = datetime(2027, 3, 1, tzinfo=UTC)


def _close_a_trade(session: Session, world: World, realized: str) -> None:
    repository.upsert_position(
        session,
        world.position(
            status=PositionStatus.CLOSED,
            side=PositionSide.FLAT,
            quantity=Decimal(0),
            realized_pnl=Decimal(realized),
            closed_at=datetime(2027, 1, 1, tzinfo=UTC),
        ),
    )


def test_a_run_started_before_the_latest_pass_does_not_count(session: Session, world: World):
    # The paper guard (AC-14) means every paper run starts after some pass, so the world's run has one
    # (conftest.paper_pass). A newer pass, dated after the run started, leaves no run to count.
    repository.record_promotion(
        session,
        PromotionRow(
            strategy_version_id=world.version_id,
            from_stage=Stage.CANDIDATE,
            to_stage=Stage.PAPER,
            verdict=Verdict.PASS,
            thresholds={},
            decided_by=DecidedBy.MANUAL,
            notes="re promoted",
            decided_at=datetime(2027, 2, 1, tzinfo=UTC),  # after the world's run started (2027-01-15)
            frozen_hash=WORLD_FROZEN.frozen_hash,
        ),
        [],
    )
    _close_a_trade(session, world, "5")

    evidence = paper_evidence(session, WORLD_FROZEN.frozen_hash, NOW)

    assert evidence.run_ids == ()
    assert evaluate_paper(evidence.inputs).verdict == GateVerdict.INSUFFICIENT


def test_a_manual_pass_counts_later_runs_but_never_reaches_live(session: Session, world: World):
    passed_at = datetime(2026, 1, 1, tzinfo=UTC)  # before the world's paper run started
    repository.record_promotion(
        session,
        PromotionRow(
            strategy_version_id=world.version_id,
            from_stage=Stage.CANDIDATE,
            to_stage=Stage.PAPER,
            verdict=Verdict.PASS,
            thresholds={},
            decided_by=DecidedBy.MANUAL,
            notes="pre gate placeholder",
            decided_at=passed_at,
            frozen_hash=WORLD_FROZEN.frozen_hash,
        ),
        [],
    )
    _close_a_trade(session, world, "5")

    evidence = paper_evidence(session, WORLD_FROZEN.frozen_hash, NOW)
    result = evaluate_paper(evidence.inputs)

    assert world.run_id in evidence.run_ids
    assert evidence.inputs.gate_pass is False
    assert evidence.inputs.events  # the later trade counts
    reasons = {(r.check, r.code) for r in result.reasons}
    assert ("candidate_pass", "missing_run") in reasons
    assert ("paper_runs", "missing_run") in reasons  # the test world's run recorded no starting balance
    assert result.verdict != GateVerdict.PASS
    progress = {p.frozen_hash: p for p in paper_progress(session, NOW + timedelta(days=1))}
    assert WORLD_FROZEN.frozen_hash in progress
    assert progress[WORLD_FROZEN.frozen_hash].min_days == 30  # the floors the page shows (AC-21)
    assert progress[WORLD_FROZEN.frozen_hash].trade_floor in (20, 100)
