"""covers: spec 0015 AC-15 (manual promotion: terminal, note, typed name, never live), the stage it gives."""

import argparse
from datetime import UTC, datetime
from uuid import uuid4, uuid7

import pytest
from bundle_factory import (  # type: ignore[import-not-found]  # core's test helper, put on sys.path by conftest
    study,
    study_bundle,
    version,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gate.main import EXIT_USAGE, _promote
from gate.manual import ManualPromotionRefused, promote_manual, versions
from trading_core.recording import models, repository
from trading_core.recording.rows import StrategyVersionRow

NOW = datetime(2026, 10, 19, 16, tzinfo=UTC)


def _imported(gate_engine) -> tuple[StrategyVersionRow, str]:
    strategy_id = uuid7()
    the_version = version(strategy_id, {"manual": uuid7().hex})
    bundle = study_bundle(study(strategy_id, f"manual-{uuid4().hex[:8]}"), the_version)
    with sessionmaker(gate_engine)() as session:
        repository.import_run_bundle(session, bundle)
        session.commit()
    assert bundle.strategy_name is not None
    return the_version, bundle.strategy_name


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("tty", "note", "typed", "message"),
    [
        (False, "pre gate placeholder", "macd_test", "terminal"),
        (True, "  ", "macd_test", "--note"),
        (True, "pre gate placeholder", "macd", "does not match"),
    ],
)
def test_a_manual_promotion_is_refused_without_its_safeguards(gate_engine, tty, note, typed, message):
    the_version, _ = _imported(gate_engine)
    with Session(gate_engine) as session, pytest.raises(ManualPromotionRefused, match=message):
        promote_manual(session, the_version.id, note, is_tty=tty, confirm=lambda _: typed, clock=lambda: NOW)


@pytest.mark.postgres
def test_a_confirmed_manual_promotion_puts_the_version_in_paper(gate_engine):
    the_version, name = _imported(gate_engine)
    with Session(gate_engine) as session:
        promotion_id = promote_manual(
            session, the_version.id, "pre gate placeholder", is_tty=True, confirm=lambda _: name, clock=lambda: NOW
        )
        session.commit()
        row = session.get(models.Promotion, promotion_id)
        lines = {line.version_id: line for line in versions(session, name)}

    assert row is not None
    assert (row.decided_by, row.verdict, row.study_id, row.notes) == ("manual", "pass", None, "pre gate placeholder")
    assert lines[the_version.id].stage == "paper"


@pytest.mark.postgres
def test_the_decision_time_is_read_after_the_name_is_typed(gate_engine):
    the_version, name = _imported(gate_engine)
    typed: list[str] = []

    def clock():
        assert typed, "the clock was read before the prompt was answered"
        return NOW

    def confirm(_):
        typed.append(name)
        return name

    with Session(gate_engine) as session:
        promotion_id = promote_manual(session, the_version.id, "note", is_tty=True, confirm=confirm, clock=clock)
        session.rollback()
    assert promotion_id is not None


@pytest.mark.postgres
def test_an_unknown_version_is_refused(gate_engine):
    with Session(gate_engine) as session, pytest.raises(ManualPromotionRefused, match="no strategy version"):
        promote_manual(session, uuid7(), "note", is_tty=True, confirm=lambda _: "x", clock=lambda: NOW)


def test_the_live_step_is_refused_before_anything_is_read(capsys):
    args = argparse.Namespace(step="paper-live", version=uuid7(), note="note")

    assert _promote("postgresql+psycopg://nobody@nowhere/none", args) == EXIT_USAGE
    assert "only the gate can promote to live" in capsys.readouterr().err


@pytest.mark.postgres
def test_no_manual_promotion_row_ever_reaches_live(gate_engine):
    with Session(gate_engine) as session:
        manual_live = session.execute(
            select(models.Promotion.id).where(
                models.Promotion.decided_by == "manual", models.Promotion.to_stage == "live"
            )
        ).first()

    assert manual_live is None
