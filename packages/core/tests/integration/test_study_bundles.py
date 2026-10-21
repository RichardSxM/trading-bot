"""covers: spec 0015 AC-2 (a study name is bound to its plan), AC-3 (study rows in the bundle), AC-5 (one
completed holdout per frozen hash), AC-7 (import is idempotent and refuses a contradiction)."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4, uuid7

import pytest
from bundle_factory import study, version
from bundle_factory import study_bundle as _study_bundle
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.bundle_codec import BundleFile, bundle_json
from trading_core.recording.enums import RunStatus, StudyRole
from trading_core.recording.errors import ForeignRunError, MissingParentError, RecordConflictError


def study_bundle(*args, **kwargs):
    """The factory's bundle with this database's instrument tick (the conftest's 0.5)."""
    return _study_bundle(*args, tick=Decimal("0.5"), **kwargs)


def _count(session: Session, model, **where) -> int:
    query = select(func.count()).select_from(model)
    for name, value in where.items():
        query = query.where(getattr(model, name) == value)
    return session.execute(query).scalar_one()


def _fresh(name: str):
    strategy_id = uuid7()
    return study(strategy_id, name), version(strategy_id, {"fast": 12, "name": name})


def test_importing_a_bundle_twice_adds_nothing_the_second_time(session: Session):
    the_study, the_version = _fresh(f"import-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version)

    assert repository.import_run_bundle(session, bundle) is True
    assert repository.import_run_bundle(session, bundle) is False
    session.commit()

    assert _count(session, models.StrategyRun, id=bundle.run.id) == 1
    assert _count(session, models.RunDailyEquity, run_id=bundle.run.id) == 3
    assert _count(session, models.RunMeasures, run_id=bundle.run.id) == 1
    assert _count(session, models.StudyRun, run_id=bundle.run.id) == 1
    assert _count(session, models.Fill, run_id=bundle.run.id) == 1
    stored = session.get(models.StrategyVersion, the_version.id)
    assert stored is not None
    assert stored.frozen_hash == the_version.frozen_hash
    # The laptop's strategy id is replaced by the VPS strategy of the same name.
    strategy = session.execute(select(models.Strategy).where(models.Strategy.name == "macd_test")).scalar_one()
    assert stored.strategy_id == strategy.id
    assert session.execute(select(models.Study.strategy_id).where(models.Study.id == the_study.id)).scalar_one() == (
        strategy.id
    )


def _in_parts(tmp_path, bundle):
    path = tmp_path / "run.bundle.json"
    path.write_text(bundle_json(bundle))
    file = BundleFile(path)
    return file.head(), file


def test_a_bundle_streamed_from_its_file_writes_the_same_rows(session: Session, tmp_path):
    the_study, the_version = _fresh(f"stream-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version)
    head, file = _in_parts(tmp_path, bundle)

    assert repository.import_run_bundle(session, head, file) is True
    assert repository.import_run_bundle(session, bundle) is False

    for model, count in (
        (models.RunResult, 1),
        (models.Decision, 1),
        (models.Order, 1),
        (models.Fill, 1),
        (models.Position, 1),
        (models.FundingPayment, 1),
        (models.RunDailyEquity, 3),
        (models.RunMeasures, 1),
        (models.StudyRun, 1),
    ):
        assert _count(session, model, run_id=bundle.run.id) == count, model
    order_id = session.execute(select(models.Order.id).where(models.Order.run_id == bundle.run.id)).scalar_one()
    assert _count(session, models.OrderEvent, order_id=order_id) == 1  # an event has no run id, only its order


def test_a_streamed_fill_of_an_order_the_file_lacks_is_refused(session: Session, tmp_path):
    the_study, the_version = _fresh(f"orphan-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version)
    head, file = _in_parts(tmp_path, replace(bundle, fills=[replace(bundle.fills[0], client_order_id="O-9")]))

    with pytest.raises(MissingParentError, match="O-9"):
        repository.import_run_bundle(session, head, file)


def test_a_streamed_row_of_another_run_is_refused(session: Session, tmp_path):
    the_study, the_version = _fresh(f"foreign-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version)
    head, file = _in_parts(tmp_path, replace(bundle, fills=[replace(bundle.fills[0], run_id=uuid7())]))

    with pytest.raises(ForeignRunError, match="fills row"):
        repository.import_run_bundle(session, head, file)


def test_a_changed_row_under_the_same_key_is_a_conflict(session: Session):
    the_study, the_version = _fresh(f"conflict-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version)
    repository.import_run_bundle(session, bundle)
    tampered = replace(bundle, results=[replace(bundle.results[0], return_after_costs=Decimal("0.5"))])

    with pytest.raises(RecordConflictError):
        repository.import_run_bundle(session, tampered)


def test_a_study_name_with_another_plan_is_a_conflict(session: Session):
    the_study, _ = _fresh(f"plan-{uuid4().hex[:8]}")
    repository.record_study(session, replace(the_study, strategy_id=_strategy(session)))
    other = replace(the_study, plan={"other": 1}, plan_hash="b" * 64)

    with pytest.raises(RecordConflictError):
        repository.record_study(session, replace(other, strategy_id=_strategy(session)))


def test_a_resumed_study_with_a_fresh_id_keeps_the_stored_id(session: Session):
    the_study, _ = _fresh(f"resume-{uuid4().hex[:8]}")
    first = repository.record_study(session, replace(the_study, strategy_id=_strategy(session)))

    again = repository.record_study(session, replace(the_study, id=uuid4(), strategy_id=_strategy(session)))

    assert again == first


def _strategy(session: Session):
    from trading_core.recording.enums import StrategyKind

    return repository._ensure_strategy(session, "macd_test", StrategyKind.HOUR_LEVEL)


def test_a_failed_trial_writes_its_run_and_study_row_without_results(session: Session):
    the_study, the_version = _fresh(f"trial-{uuid4().hex[:8]}")
    bundle = study_bundle(the_study, the_version, status=RunStatus.FAILED)

    repository.import_run_bundle(session, bundle)

    assert _count(session, models.StudyRun, run_id=bundle.run.id, status="failed") == 1
    assert _count(session, models.RunResult, run_id=bundle.run.id) == 0


def test_a_second_completed_holdout_of_the_same_frozen_hash_is_refused(session: Session):
    the_study, the_version = _fresh(f"holdout-{uuid4().hex[:8]}")
    first = study_bundle(the_study, the_version, role=StudyRole.HOLDOUT)
    second = study_bundle(the_study, the_version, role=StudyRole.HOLDOUT)
    repository.import_run_bundle(session, first)

    with pytest.raises(IntegrityError, match="uq_study_runs_frozen_hash"):
        repository.import_run_bundle(session, second)


def test_write_run_bundle_writes_the_study_parts(engine, trading_env):
    the_study, the_version = _fresh(f"write-{uuid4().hex[:8]}")
    with Session(engine) as session:
        strategy_id = _strategy(session)
        repository.upsert_instrument(session, study_bundle(the_study, the_version).instrument)
        session.execute(
            insert(models.StrategyVersion).values(
                id=the_version.id,
                strategy_id=strategy_id,
                git_commit=the_version.git_commit,
                config=dict(the_version.config),
                version_hash=the_version.version_hash,
                code_hash=the_version.code_hash,
                frozen_hash=the_version.frozen_hash,
            )
        )
        session.commit()
        bundle = study_bundle(replace(the_study, strategy_id=strategy_id), the_version)
        repository.write_run_bundle(session, bundle)

        assert _count(session, models.RunDailyEquity, run_id=bundle.run.id) == 3
        assert _count(session, models.StudyRun, study_id=the_study.id) == 1
