"""covers: spec 0015 AC-7 (import refuses a non lab file whole, loads the rest, and adds nothing twice),
AC-8 (one promotion per evaluation, evidence on every study run, the same metrics every time), AC-12 (the
gate cross checks its evidence), AC-22 (gate_app may not update a promotion)."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4, uuid7

import pytest
from bundle_factory import (  # type: ignore[import-not-found]  # core's test helper, put on sys.path by conftest
    study,
    study_bundle,
    version,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from gate.evaluate import evaluate_candidate_step
from gate.importer import BundleRefused, check_bundle, import_folder
from trading_core.recording import models
from trading_core.recording.bundle_codec import bundle_json
from trading_core.recording.enums import Env, RunMode, StudyRole
from trading_core.recording.gate_queries import UnknownStudyError

NOW = datetime(2026, 10, 19, 15, tzinfo=UTC)


def _fresh(name: str):
    strategy_id = uuid7()
    return study(strategy_id, name), version(strategy_id, {"fast": 12, "name": name})


def test_a_lab_bundle_passes_the_check():
    check_bundle(study_bundle(*_fresh("ok")))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: replace(b, run=replace(b.run, env=Env.LIVE)), "not demo"),
        (lambda b: replace(b, run=replace(b.run, mode=RunMode.PAPER)), "not backtest"),
        (lambda b: replace(b, run=replace(b.run, trader_id="TRADER-001")), "not BACKTESTER-LAB"),
        (lambda b: replace(b, version=replace(b.version, frozen_hash=None)), "frozen_hash"),
        (lambda b: replace(b, version=None), "frozen_hash"),
    ],
)
def test_anything_but_a_frozen_demo_lab_run_is_refused(change, message):
    with pytest.raises(BundleRefused, match=message):
        check_bundle(change(study_bundle(*_fresh("bad"))))


def _write(folder: Path, name: str, bundle) -> None:
    path = folder / "batch" / f"{name}.bundle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bundle_json(bundle))


@pytest.mark.postgres
def test_import_loads_good_files_refuses_bad_ones_and_repeats_nothing(tmp_path, gate_engine):
    """Runs in the gate service: ``docker compose run --rm -T --entrypoint pytest gate -q -m postgres``."""
    good = study_bundle(*_fresh(f"imp-{uuid4().hex[:8]}"))
    _write(tmp_path, "good", good)
    _write(tmp_path, "paper", replace(good, run=replace(good.run, id=uuid7(), mode=RunMode.PAPER)))
    (tmp_path / "batch" / "junk.bundle.json").write_text("{")
    sessions = sessionmaker(gate_engine)

    first = import_folder(sessions, tmp_path)
    again = import_folder(sessions, tmp_path)

    assert (first.loaded, sorted(first.refused)) == (
        ["batch/good.bundle.json"],
        ["batch/junk.bundle.json", "batch/paper.bundle.json"],
    )
    assert (again.loaded, again.skipped) == ([], ["batch/good.bundle.json"])
    with sessions() as session:
        assert (
            session.execute(
                select(func.count()).select_from(models.StrategyRun).where(models.StrategyRun.id == good.run.id)
            ).scalar_one()
            == 1
        )


# The packaged thresholds' holdout_start.
HOLDOUT_START = date(2025, 10, 19)


def _study_files(folder: Path, name: str, *, holdout_point: int = 1, development_start: date | None = None) -> None:
    """Two grid points with equal results (so point 0 wins), a holdout, and both benchmarks.

    By default the holdout runs point 1 and development runs from the factory's 2026 days, after
    ``holdout_start``, which the gate must call inconsistent.
    """
    strategy_id = uuid7()
    the_study = study(strategy_id, name)
    points = [version(strategy_id, {"fast": f, "study": name}) for f in (8, 12)]
    dev = {} if development_start is None else {"start": development_start}
    for i, v in enumerate(points):
        _write(folder, f"dev{i}", study_bundle(the_study, v, grid_point=i, **dev))
    held = {} if development_start is None else {"start": HOLDOUT_START}
    _write(
        folder,
        "holdout",
        study_bundle(the_study, points[holdout_point], role=StudyRole.HOLDOUT, grid_point=holdout_point, **held),
    )
    for bench in ("buy_and_hold", "macd_baseline"):
        bench_id = uuid7()
        _write(
            folder,
            bench,
            study_bundle(
                the_study,
                version(bench_id, {"b": bench, "s": name}),
                strategy=bench,
                role=StudyRole.BENCHMARK,
                **held,
            ),
        )


@pytest.mark.postgres
def test_evaluate_appends_one_promotion_with_evidence_and_the_same_metrics_each_time(tmp_path, gate_engine):
    name = f"eval-{uuid4().hex[:8]}"
    _study_files(tmp_path, name)
    assert not import_folder(sessionmaker(gate_engine), tmp_path).refused

    first = evaluate_candidate_step(gate_engine, name, gate_commit="g" * 40, now=NOW)
    second = evaluate_candidate_step(gate_engine, name, gate_commit="g" * 40, now=NOW)

    assert first.promotion_id != second.promotion_id
    assert first.result.metrics == second.result.metrics
    # Three days and one trade: too few for the floor, so never a pass.
    assert first.result.verdict.value != "pass"
    assert ("trade_floor", "too_few_trades") in {(r.check, r.code) for r in first.result.reasons}
    with Session(gate_engine) as session:
        row = session.get(models.Promotion, first.promotion_id)
        assert row is not None
        assert (row.decided_by, row.from_stage) == ("gate", "candidate")
        assert row.frozen_hash is not None
        assert row.study_id is not None
        evidence = session.execute(
            select(func.count())
            .select_from(models.PromotionEvidence)
            .where(models.PromotionEvidence.promotion_id == first.promotion_id)
        ).scalar_one()
        assert evidence == 5
        with pytest.raises(ProgrammingError, match="permission denied"):
            session.execute(text("UPDATE promotions SET notes = 'x' WHERE id = :id"), {"id": first.promotion_id})


@pytest.mark.postgres
def test_evaluate_refuses_an_unknown_study(gate_engine):
    with pytest.raises(UnknownStudyError):
        evaluate_candidate_step(gate_engine, f"none-{uuid4().hex[:8]}", gate_commit="g", now=NOW)


def _inconsistent(result) -> set[str]:
    return {r.check for r in result.reasons if r.code == "inconsistent_evidence"}


@pytest.mark.postgres
def test_evaluate_cross_checks_the_holdout_against_the_development_evidence(tmp_path, gate_engine):
    good, bad = f"good-{uuid4().hex[:8]}", f"bad-{uuid4().hex[:8]}"
    _study_files(tmp_path / "good", good, holdout_point=0, development_start=date(2025, 10, 1))
    _study_files(tmp_path / "bad", bad)
    assert not import_folder(sessionmaker(gate_engine), tmp_path).refused

    consistent = evaluate_candidate_step(gate_engine, good, gate_commit="g" * 40, now=NOW)
    broken = evaluate_candidate_step(gate_engine, bad, gate_commit="g" * 40, now=NOW)

    assert _inconsistent(consistent.result) == set()
    assert _inconsistent(broken.result) == {"holdout_grid_point", "development_days"}
    assert broken.result.verdict.value != "pass"
