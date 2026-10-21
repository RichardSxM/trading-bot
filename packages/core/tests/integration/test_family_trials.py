"""covers: spec 0018 AC-19 (N and the trial Sharpes span every study of the same strategy kind and lab).

Other tests in the session may commit studies to the same database, so each check is the change in the
family's counts after this test's own imports (rolled back at the end).
"""

import math
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4, uuid7

from bundle_factory import study, version
from bundle_factory import study_bundle as _study_bundle
from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import RunStatus, StrategyKind, StudyLab, StudyRole
from trading_core.recording.gate_queries import family_trials, study_evidence


def study_bundle(*args, **kwargs):
    """The factory's bundle with this database's instrument tick (the conftest's 0.5)."""
    return _study_bundle(*args, tick=Decimal("0.5"), **kwargs)


def _import_study(session, hashes, *, kind=StrategyKind.HOUR_LEVEL, lab=StudyLab.CANDLE, failed=()):
    """A study of a fresh strategy with one development run per hash index (``failed`` ones fail) and a
    holdout of the last; returns its name."""
    name = f"fam-{uuid4().hex[:8]}"
    strategy = f"cand_{uuid4().hex[:8]}"
    strategy_id = uuid7()
    the_study = replace(study(strategy_id, name), lab=lab)
    points = [version(strategy_id, {"point": i, "study": name}) for i in range(hashes)]
    bundles = [
        study_bundle(
            the_study,
            v,
            strategy=strategy,
            grid_point=i,
            status=RunStatus.FAILED if i in failed else RunStatus.COMPLETED,
        )
        for i, v in enumerate(points)
    ]
    holdout = study_bundle(the_study, points[-1], strategy=strategy, role=StudyRole.HOLDOUT, grid_point=hashes - 1)
    bundles.append(holdout)
    bench_version = version(uuid7(), {"bench": name})
    bench = study_bundle(the_study, bench_version, strategy="buy_and_hold", role=StudyRole.BENCHMARK)
    for bundle in [*bundles, bench]:
        bundle = replace(bundle, study_strategy_name=strategy, study_strategy_kind=kind)
        if bundle.study_run.role != StudyRole.BENCHMARK:
            bundle = replace(bundle, strategy_kind=kind)
        assert repository.import_run_bundle(session, bundle)
    return name


def test_two_candle_studies_of_the_kind_count_their_distinct_hashes_and_other_families_do_not(session: Session):
    before_n, before_pool = family_trials(session, StrategyKind.HOUR_LEVEL, StudyLab.CANDLE)

    first = _import_study(session, 4)
    _import_study(session, 2)
    # Neither a scalping replay study nor an hour level replay study is in the hour level candle family.
    _import_study(session, 3, kind=StrategyKind.SCALPING, lab=StudyLab.REPLAY)
    _import_study(session, 3, lab=StudyLab.REPLAY)

    n, pool = family_trials(session, StrategyKind.HOUR_LEVEL, StudyLab.CANDLE)
    assert n - before_n == 6  # the benchmarks never count
    assert len(pool) - len(before_pool) == 6
    # Every factory run has an annual ratio of 1.5: six more per day Sharpes of 1.5 / sqrt(365).
    per_day = 1.5 / math.sqrt(365)
    assert sum(math.isclose(x, per_day) for x in pool) - sum(math.isclose(x, per_day) for x in before_pool) == 6

    evidence = study_evidence(session, first)
    assert evidence.inputs.trial_count == n
    assert evidence.inputs.trial_sharpes == pool


def test_a_failed_run_counts_in_n_but_gives_no_trial_sharpe(session: Session):
    before_n, before_pool = family_trials(session, StrategyKind.HOUR_LEVEL, StudyLab.CANDLE)
    _import_study(session, 3, failed={0})
    n, pool = family_trials(session, StrategyKind.HOUR_LEVEL, StudyLab.CANDLE)
    assert (n - before_n, len(pool) - len(before_pool)) == (3, 2)
