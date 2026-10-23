"""covers: spec 0015 AC-2 (study file and plan), AC-3 (development runs and resume), AC-4 (benchmarks),
AC-5 (holdout once)."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from lab.candle_backtest import EXIT_MISSING
from lab.runfile import RunFileError
from lab.study import EXIT_FAILED, EXIT_OK, EXIT_USAGE, StudyStore, load_study, run_holdout, run_study
from trading_core.gate.verdict import evaluate_candidate
from trading_core.lab.candles import Month
from trading_core.recording import models
from trading_core.recording.bundle_codec import parse_bundle
from trading_core.recording.gate_queries import study_detail, study_evidence
from trading_core.recording.rows import RunBundle, StudyRunRow
from trading_core.secrets import POSTGRES_PASSWORD, read_secret
from trading_core.settings import Settings

from .candle_fixtures import synthetic_month
from .conftest import lab_settings

# Candle holdouts end at the catalog, not at this date; replay holdouts end before it (AC-24).
TODAY = date(2026, 10, 20)

STUDY = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2025-09-01
to = 2025-10-18
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"

[strategy]
name = "macd_baseline"
[strategy.config]
notional_usd = "10000"
bar_spec = "1-HOUR-LAST"
slow = 26
signal = 9
trend_ema = 50
stop_pct = "3"
[strategy.grid]
fast = [8, 12]

[study]
name = "NAME"
lab = "candle"
seed = 42
"""


def _write(tmp_path: Path, text: str, name: str = "s") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text)
    return path


def test_the_plan_holds_the_grid_ranges_and_default_folds(tmp_path):
    study = load_study(_write(tmp_path, STUDY.replace("NAME", "plan-check")))

    assert study.plan["grid"] == [{"fast": 8}, {"fast": 12}]
    assert study.plan["ranges"] == {"development": ["2025-09-01", "2025-10-18"], "holdout_start": "2025-10-19"}
    assert study.plan["folds"] == {"first_in_sample_months": 12, "step_months": 6}
    assert study.loaded.run.to.isoformat() == "2025-10-18"
    assert study.plan_hash == load_study(_write(tmp_path, STUDY.replace("NAME", "plan-check"), "again")).plan_hash


def test_a_comment_does_not_change_the_plan_but_a_value_does(tmp_path):
    base = load_study(_write(tmp_path, STUDY.replace("NAME", "h"))).plan_hash

    assert load_study(_write(tmp_path, "# note\n" + STUDY.replace("NAME", "h"), "c")).plan_hash == base
    assert (
        load_study(_write(tmp_path, STUDY.replace("NAME", "h").replace("seed = 42", "seed = 43"), "v")).plan_hash
        != base
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace('[study]\nname = "NAME"\nlab = "candle"\nseed = 42\n', ""), r"\[study\] is missing"),
        (lambda t: t.replace('lab = "candle"', 'lab = "candle"\nentry_mode = "maker"'), "no entry_mode"),
        (lambda t: t.replace("to = 2025-10-18", "to = 2025-10-19"), "reaches into the holdout"),
        (
            lambda t: t.replace("from = 2025-09-01", "from = 2025-10-19").replace("to = 2025-10-18", "to = 2025-10-20"),
            "on or after holdout_start",
        ),
        (lambda t: t.replace('name = "NAME"', 'name = "Bad Name"'), "study.name"),
    ],
)
def test_a_bad_study_file_is_refused(tmp_path, change, message):
    with pytest.raises(RunFileError, match=message):
        load_study(_write(tmp_path, change(STUDY).replace("NAME", "bad")))


def _catalog(root: Path) -> None:
    synthetic_month(root, month=Month(2025, 9), days=30)
    synthetic_month(root, month=Month(2025, 10), days=31)
    synthetic_month(root, month=Month(2025, 11), days=5)


def _bundles(reports: Path) -> list[RunBundle]:
    return [parse_bundle(p.read_text()) for p in sorted(reports.rglob("*.bundle.json"))]


def _study_run(bundle: RunBundle) -> StudyRunRow:
    assert bundle.study_run is not None
    return bundle.study_run


@pytest.mark.postgres
def test_a_candle_study_runs_resumes_and_spends_its_holdout_once(tmp_path, monkeypatch):
    """Runs in the lab service: ``docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres``."""
    catalog, reports = tmp_path / "catalog", tmp_path / "reports"
    _catalog(catalog)
    # This database persists, and a benchmark's config never changes: a fresh commit each run, so an
    # older run's benchmark version (same commit, older code hash) never conflicts.
    settings = lab_settings(catalog, reports).model_copy(update={"git_commit": uuid4().hex + uuid4().hex[:8]})
    name = f"study-{uuid4().hex[:8]}"
    # A frozen version spends its holdout forever, and this database persists: a fresh config each run.
    notional = str(9000 + uuid4().int % 1000)
    base = Settings(trading_env="demo", postgres_user="lab_app")
    store = StudyStore(create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=2))
    try:
        text = STUDY.replace("NAME", name).replace('notional_usd = "10000"', f'notional_usd = "{notional}"')
        study = load_study(_write(tmp_path, text))
        assert run_study(settings, study, store) == EXIT_OK
        development = _bundles(reports)
        assert len(development) == 2
        assert {_study_run(b).grid_point for b in development} == {0, 1}
        assert all(b.daily_equity and b.measures and b.results[0].ratio_kind == "sharpe_daily" for b in development)
        assert {b.study.plan_hash if b.study else None for b in development} == {study.plan_hash}

        # Resume: nothing left to run.
        assert run_study(settings, study, store) == EXIT_OK
        assert len(_bundles(reports)) == 2
        # The same name with another plan is refused before any run.
        other = load_study(_write(tmp_path, text.replace("seed = 42", "seed = 1"), "o"))
        assert run_study(settings, other, store) == EXIT_USAGE
        # The same plan with edited thresholds is refused too, with exit 64, not a conflict traceback.
        edited = replace(study, thresholds=replace(study.thresholds, dsr_min=study.thresholds.dsr_min + 1))
        assert run_study(settings, edited, store) == EXIT_USAGE

        # Stopped before its benchmarks: the holdout lands, no benchmark does.
        monkeypatch.setattr("lab.study.benchmark_names", lambda kind, studied: ())
        assert run_holdout(settings, name, store, today=TODAY) == EXIT_OK
        monkeypatch.undo()
        assert sorted(_study_run(b).role for b in _bundles(reports)) == ["development", "development", "holdout"]

        # The catalog end moves on. A benchmark failing during the resume exits 1 and reruns no holdout.
        synthetic_month(catalog, month=Month(2025, 11), days=12)
        monkeypatch.setattr("lab.study.backtest", lambda *args, **kwargs: EXIT_MISSING)
        assert run_holdout(settings, name, store, today=TODAY) == EXIT_FAILED
        monkeypatch.undo()

        # The resume runs only the missing benchmark, over the stored holdout range; a study of
        # macd_baseline never runs a macd_baseline benchmark (AC-4).
        assert run_holdout(settings, name, store, today=TODAY) == EXIT_OK
        roles = sorted((_study_run(b).role, b.strategy_name or "") for b in _bundles(reports))
        assert roles == [
            ("benchmark", "buy_and_hold"),
            ("development", "macd_baseline"),
            ("development", "macd_baseline"),
            ("holdout", "macd_baseline"),
        ]
        bench = next(b for b in _bundles(reports) if _study_run(b).role == "benchmark")
        assert bench.run.data_end_at is not None
        assert bench.run.data_end_at.date().isoformat() == "2025-11-06"  # the holdout's range, not the new end
        holdout = next(b for b in _bundles(reports) if _study_run(b).role == "holdout")
        assert holdout.run.data_start_at is not None
        assert holdout.run.data_end_at is not None
        assert holdout.version is not None
        assert holdout.run.data_start_at.date().isoformat() == "2025-10-19"
        assert holdout.run.data_end_at.date().isoformat() == "2025-11-06"  # through Nov 5, the last whole day
        assert holdout.version.frozen_hash == _study_run(holdout).frozen_hash  # the version travels with the run

        # AC-13: the lab previews the gate with the same inputs, as lab_app, and writes nothing.
        with store.session() as session:
            before = session.execute(select(func.count()).select_from(models.Promotion)).scalar_one()
            preview = evaluate_candidate(study_evidence(session, name).inputs)
            after = session.execute(select(func.count()).select_from(models.Promotion)).scalar_one()
            assert holdout.study is not None
            detail = study_detail(session, holdout.study.id)
        assert preview.verdict.value in {"pass", "fail", "insufficient"}
        assert preview.metrics["beats_macd_baseline_applicable"] is False
        assert "missing_benchmark" not in {r.code for r in preview.reasons}
        assert before == after
        # Two grid points, the holdout sharing one's frozen hash, and the benchmark left out (AC-19).
        assert detail is not None
        assert detail.trials == 2

        assert run_holdout(settings, name, store, today=TODAY) == EXIT_USAGE
        with store.session() as session:
            spent = session.execute(
                select(func.count())
                .select_from(models.StudyRun)
                .where(
                    models.StudyRun.frozen_hash == _study_run(holdout).frozen_hash, models.StudyRun.role == "holdout"
                )
            ).scalar_one()
        assert spent == 1
    finally:
        store.engine.dispose()


@pytest.mark.postgres
def test_holdout_of_an_unknown_study_is_refused(tmp_path):
    base = Settings(trading_env="demo", postgres_user="lab_app")
    store = StudyStore(create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=1))
    try:
        settings = lab_settings(tmp_path, tmp_path).model_copy(update={"git_commit": "f" * 40})
        assert run_holdout(settings, f"nope-{uuid4().hex[:6]}", store, today=TODAY) == EXIT_USAGE
    finally:
        store.engine.dispose()


def test_the_committed_study_file_is_valid():
    study = load_study(Path(__file__).resolve().parents[1] / "studies" / "macd_baseline.toml")

    assert study.section.name == "macd_baseline"
    assert len(study.variants) == 2
