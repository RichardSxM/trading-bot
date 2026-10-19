"""``lab study`` and ``lab holdout`` (spec 0015 AC-2 to AC-5): a strategy tested under a plan fixed in advance.

A study file is the lab's run file verbatim plus a ``[study]`` table. ``lab study`` validates it, loads
the packaged thresholds, and stores the study (its resolved plan and thresholds, each with a hash).
Every grid point then runs once over the development range: the file's ``from`` to the day before
``holdout_start``. Running it again with the same plan resumes, skipping grid points that already
completed; a different plan under the same name exits 64 before any run.

``lab holdout`` holds the study's advisory lock, needs every grid point completed, picks the final one
(best development daily Sharpe, ties to the lowest index), and runs it once from ``holdout_start`` to
the last complete UTC day of the catalog, unless that frozen version already spent its holdout. Then
the benchmarks (``buy_and_hold``, plus ``macd_baseline`` for an hour level study) run over the same
range. Every run writes its bundle beside its report, for ``scripts/push-bundles.sh``.

Only candle studies are built so far; a replay study is refused (exit 64) until Slice 3.
"""

import hashlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from lab.candle_backtest import backtest
from lab.candle_fetch import candle_tree
from lab.instruments import pinned_instrument
from lab.replay import PostgresStore
from lab.runfile import LoadedCandleRun, Overrides, RunFileError, candle_run_from_raw, read_toml
from lab.settings import LabSettings
from lab.strategies import registry_entry
from lab.study_rows import StudyContext
from trading_core.gate.thresholds import Thresholds
from trading_core.lab.candles import Month
from trading_core.lab.variants import CandleVariantSpec, expand_candle_variants
from trading_core.node.recording import instrument_row
from trading_core.recording import models, repository
from trading_core.recording.enums import RunStatus, StrategyKind, StudyLab, StudyRole
from trading_core.recording.rows import JsonValue, StrategyVersionRow, StudyRow, canonical_json
from trading_core.thresholds_file import packaged_thresholds

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 64
BENCHMARK_FILES: Final = {"buy_and_hold": "runs/buy_and_hold.toml", "macd_baseline": "runs/macd_baseline.toml"}
_DAY_NS: Final = 86_400 * 1_000_000_000


class StudySection(BaseModel):
    """The ``[study]`` table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    lab: Literal["candle", "replay"]
    entry_mode: Literal["taker", "maker"] | None = None
    seed: int
    first_in_sample_months: int | None = Field(default=None, ge=1)
    step_months: int | None = Field(default=None, ge=1)
    first_in_sample_days: int | None = Field(default=None, ge=1)
    step_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        candle = (self.first_in_sample_months, self.step_months)
        replay = (self.first_in_sample_days, self.step_days)
        if self.lab == "candle" and (self.entry_mode is not None or any(v is not None for v in replay)):
            raise ValueError("a candle study takes no entry_mode, first_in_sample_days, or step_days")
        if self.lab == "replay" and (self.entry_mode is None or any(v is not None for v in candle)):
            raise ValueError("a replay study needs entry_mode and takes no first_in_sample_months or step_months")
        return self

    def folds(self, thresholds: Thresholds) -> dict[str, int]:
        """The fold layout: the study's override, else the thresholds' default for its lab (AC-9)."""
        if self.lab == "candle":
            return {
                "first_in_sample_months": self.first_in_sample_months or thresholds.candle.first_in_sample_months,
                "step_months": self.step_months or thresholds.candle.step_months,
            }
        return {
            "first_in_sample_days": self.first_in_sample_days or thresholds.replay.first_in_sample_days,
            "step_days": self.step_days or thresholds.replay.step_days,
        }


@dataclass(frozen=True)
class LoadedStudy:
    """A validated study file: its ``[study]`` table, its run file, and the resolved plan."""

    section: StudySection
    run_file: Mapping[str, object]
    loaded: LoadedCandleRun
    variants: tuple[CandleVariantSpec, ...]
    thresholds: Thresholds
    plan: dict[str, JsonValue]

    @property
    def plan_hash(self) -> str:
        """sha256 of the plan's canonical JSON (the same function as ``version_hash``)."""
        return _sha(self.plan)


def _sha(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _plain(value: object) -> JsonValue:
    """TOML tables as canonical JSON values: dates as ISO strings."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, str | int | bool):
        return value
    raise RunFileError([f"{value!r}: only strings, integers, booleans, dates, lists, and tables are allowed"])


def development_range(start: date, thresholds: Thresholds) -> tuple[date, date]:
    """From the run file's ``from`` to the day before ``holdout_start`` (AC-3)."""
    return start, thresholds.holdout_start - timedelta(days=1)


def load_study(path: Path, *, test_strategies: bool = False) -> LoadedStudy:
    """Read and validate a study file; ``RunFileError`` lists every problem (exit 64)."""
    data, raw = read_toml(path)
    table = raw.get("study")
    run_file = {k: v for k, v in raw.items() if k != "study"}
    if not isinstance(table, Mapping):
        raise RunFileError([f"{path}: [study] is missing"])
    try:
        section = StudySection.model_validate(table)
    except ValidationError as exc:
        raise RunFileError(
            [f"study.{'.'.join(map(str, e['loc'])) or 'study'}: {e['msg']}" for e in exc.errors(include_url=False)]
        ) from None
    if section.lab != "candle":
        raise RunFileError(["study.lab: replay studies are not built yet; only candle studies run so far"])
    thresholds = packaged_thresholds()
    loaded = candle_run_from_raw(str(path), data, run_file, Overrides(), test_strategies=test_strategies)
    start, end = development_range(loaded.run.from_, thresholds)
    if start > end:
        raise RunFileError([f"from {start} is on or after holdout_start {thresholds.holdout_start}"])
    if loaded.run.to >= thresholds.holdout_start:
        raise RunFileError(
            [f"to {loaded.run.to} reaches into the holdout (from {thresholds.holdout_start}); the plan sets the ranges"]
        )
    variants = tuple(expand_candle_variants(loaded.run.slippage_bps, loaded.run.strategy.grid))
    plan: dict[str, JsonValue] = {
        "study": _plain(section.model_dump(exclude_none=True)),
        "run_file": _plain(run_file),
        "grid": [_plain(dict(v.grid_point)) for v in variants],
        "ranges": {
            "development": [start.isoformat(), end.isoformat()],
            "holdout_start": thresholds.holdout_start.isoformat(),
        },
        "folds": dict(section.folds(thresholds)),
    }
    return LoadedStudy(
        section, run_file, replace(loaded, run=loaded.run.model_copy(update={"to": end})), variants, thresholds, plan
    )


class StudyStore(PostgresStore):
    """The study's reads and writes over Postgres as ``lab_app`` (on top of the replay ``Store``)."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(sessionmaker(engine))
        self.engine = engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session, committed when the block ends without an error."""
        with self._sessions() as session:
            yield session
            session.commit()

    def lock(self, study_id: uuid.UUID) -> AbstractContextManager[None]:
        """The study's advisory lock."""
        return repository.study_lock(self.engine, study_id)

    def version_rows(self, ids: Mapping[str, uuid.UUID]) -> dict[str, StrategyVersionRow]:
        """Each variant's stored version."""
        with self.session() as session:
            return {name: repository.strategy_version_row(session, v) for name, v in ids.items()}


def _context(
    store: StudyStore,
    study: StudyRow,
    role: StudyRole,
    strategy: str,
    kind: StrategyKind,
    instrument_id: str,
    grid_points: Mapping[str, int | None],
    studied: tuple[str, StrategyKind] | None = None,
) -> Callable[[Mapping[str, uuid.UUID]], StudyContext]:
    """The study context of one batch; ``studied`` names the study's own strategy when ``strategy`` is a benchmark."""
    study_name, study_kind = studied or (strategy, kind)

    def build(version_ids: Mapping[str, uuid.UUID]) -> StudyContext:
        return StudyContext(
            study=study,
            role=role,
            strategy_name=strategy,
            strategy_kind=kind,
            study_strategy_name=study_name,
            study_strategy_kind=study_kind,
            instrument=instrument_row(pinned_instrument(instrument_id)),
            grid_points=grid_points,
            versions=store.version_rows(version_ids),
        )

    return build


def _stored_row(stored: models.Study) -> StudyRow:
    return StudyRow(
        id=stored.id,
        strategy_id=stored.strategy_id,
        name=stored.name,
        lab=StudyLab(stored.lab),
        code_hash=stored.code_hash,
        plan=stored.plan,  # type: ignore[arg-type]  # JSONB object
        plan_hash=stored.plan_hash,
        thresholds=stored.thresholds,  # type: ignore[arg-type]  # JSONB object
        thresholds_hash=stored.thresholds_hash,
    )


def _load_study(session: Session, name: str) -> StudyRow | None:
    """The study as a plain row, copied while the session is open (a commit expires the ORM object)."""
    stored = repository.study_by_name(session, name)
    return None if stored is None else _stored_row(stored)


def _refuse(command: str, problems: list[str]) -> int:
    for problem in problems:
        print(f"lab {command}: {problem}", file=sys.stderr)
    log.error("lab_bad_input", extra={"fields": {"problems": problems}})
    return EXIT_USAGE


def run_study(settings: LabSettings, study: LoadedStudy, store: StudyStore) -> int:
    """``lab study`` after validation: store the study, then run every grid point not yet completed (AC-2, AC-3)."""
    run = study.loaded.run
    entry = registry_entry(run.strategy.name, test_strategies=settings.lab_test_strategies)
    configs = {v.name: {**run.strategy.config, **v.grid_point} for v in study.variants}
    frozen = {name: entry.frozen(config) for name, config in configs.items()}
    if any(f is None for f in frozen.values()):
        return _refuse("study", [f"{run.strategy.name} has no trading_core.node module, so it cannot be studied"])
    code_hashes = {f.code_hash for f in frozen.values() if f is not None}
    with store.session() as session:
        stored = _load_study(session, study.section.name)
    if stored is not None and stored.plan_hash != study.plan_hash:
        return _refuse("study", [f"study {study.section.name!r} exists with another plan; give a new name"])
    if stored is not None and stored.code_hash not in code_hashes:
        return _refuse("study", [f"study {study.section.name!r}: the strategy's code changed since it started"])
    instrument = instrument_row(pinned_instrument(run.instrument_id))
    first = store.prepare(
        instrument,
        run.strategy.name,
        entry.kind,
        settings.git_commit,
        next(iter(configs.values())),
        next(iter(frozen.values())),
    )
    thresholds = study.thresholds.to_json()
    with store.session() as session:
        strategy_id = repository.strategy_version_row(session, first).strategy_id
        row = StudyRow(
            strategy_id=strategy_id,
            name=study.section.name,
            lab=StudyLab(study.section.lab),
            code_hash=code_hashes.pop(),
            plan=study.plan,
            plan_hash=study.plan_hash,
            thresholds=thresholds,  # type: ignore[arg-type]  # plain JSON from to_json
            thresholds_hash=_sha(thresholds),  # type: ignore[arg-type]
        )
        repository.record_study(session, row)
        stored = _load_study(session, study.section.name)
        assert stored is not None
        done = repository.completed_grid_points(session, stored.id)
    points = {v.name: i for i, v in enumerate(study.variants)}
    remaining = [v for v in study.variants if points[v.name] not in done]
    log.info(
        "lab_study_planned",
        extra={"fields": {"study": stored.name, "grid_points": len(points), "remaining": [v.name for v in remaining]}},
    )
    if not remaining:
        print(f"lab study: every grid point of {stored.name!r} has completed; run lab holdout {stored.name}")
        return EXIT_OK
    context = _context(store, stored, StudyRole.DEVELOPMENT, run.strategy.name, entry.kind, run.instrument_id, points)
    return backtest(settings, study.loaded, store, variants=remaining, study=context)


def last_complete_day(settings: LabSettings, start: date) -> date | None:
    """The last UTC day the candle catalog holds whole, from the newest month with a manifest."""
    tree = candle_tree(settings.lab_catalog_dir)
    month = Month.of_ns(time.time_ns())
    first = Month.of_ns(int(datetime(start.year, start.month, 1, tzinfo=UTC).timestamp()) * 1_000_000_000)
    while month >= first:
        manifest = tree.read_manifest(month)
        if manifest is not None and manifest.last_ts_ns is not None:
            return date(1970, 1, 1) + timedelta(days=manifest.last_ts_ns // _DAY_NS - 1)
        month = month.previous()
    return None


def _benchmark_run(path: str, base: LoadedCandleRun, start: date, end: date) -> LoadedCandleRun:
    """A committed benchmark run file with the study's fees and instrument, over ``start``..``end`` (AC-4)."""
    data, raw = read_toml(Path(path))
    merged = dict(raw) | {
        "instrument_id": base.run.instrument_id,
        "maker_fee": format(base.run.maker_fee, "f"),
        "taker_fee": format(base.run.taker_fee, "f"),
    }
    return candle_run_from_raw(path, data, merged, Overrides(from_=start, to=end))


def run_holdout(settings: LabSettings, name: str, store: StudyStore) -> int:
    """``lab holdout`` (AC-4, AC-5): the final grid point once over the holdout, then the benchmarks."""
    with store.session() as session:
        stored = _load_study(session, name)
    if stored is None:
        return _refuse("holdout", [f"no study called {name!r}"])
    with store.lock(stored.id):
        return _holdout_locked(settings, stored, store)


def _holdout_locked(settings: LabSettings, stored: StudyRow, store: StudyStore) -> int:
    plan: Mapping[str, object] = stored.plan
    run_file = plan["run_file"]
    grid = plan["grid"]
    assert isinstance(run_file, Mapping)
    assert isinstance(grid, list)
    thresholds = Thresholds.from_json(stored.thresholds)
    base = candle_run_from_raw(f"study:{stored.name}", canonical_json(run_file).encode(), run_file, Overrides())
    variants = expand_candle_variants(base.run.slippage_bps, base.run.strategy.grid)
    with store.session() as session:
        done = repository.completed_grid_points(session, stored.id)
        missing = [v.name for i, v in enumerate(variants) if i not in done]
        if missing:
            return _refuse("holdout", [f"grid points without a completed development run: {', '.join(missing)}"])
        final = repository.final_grid_point(session, stored.id)
        assert final is not None
        final_hash = done[final].frozen_hash
        spent = repository.holdout_spent(session, final_hash)
    if spent:
        return _refuse("holdout", [f"frozen version {final_hash[:12]} already spent its holdout"])
    variant = variants[final]
    entry = registry_entry(base.run.strategy.name, test_strategies=settings.lab_test_strategies)
    frozen = entry.frozen({**base.run.strategy.config, **variant.grid_point})
    if frozen is None or frozen.frozen_hash != final_hash:
        return _refuse(
            "holdout", ["the strategy's code or engine changed since its development runs; start a new study"]
        )
    end = last_complete_day(settings, thresholds.holdout_start)
    if end is None or end < thresholds.holdout_start:
        return _refuse(
            "holdout", [f"the catalog holds no complete day from {thresholds.holdout_start}; run lab fetch-candles"]
        )
    start = thresholds.holdout_start
    holdout = replace(base, run=base.run.model_copy(update={"from_": start, "to": end}))
    study_row = stored
    log.info(
        "lab_holdout_planned",
        extra={"fields": {"study": stored.name, "grid_point": final, "range": [str(start), str(end)]}},
    )
    context = _context(
        store,
        study_row,
        StudyRole.HOLDOUT,
        base.run.strategy.name,
        entry.kind,
        base.run.instrument_id,
        {variant.name: final},
    )
    code = backtest(settings, holdout, store, variants=[variant], study=context)
    if code != EXIT_OK:
        return code
    names = ["buy_and_hold"] + (["macd_baseline"] if entry.kind == StrategyKind.HOUR_LEVEL else [])
    with store.session() as session:
        have = set(
            session.execute(
                select(models.Strategy.name)
                .join(models.StrategyVersion, models.StrategyVersion.strategy_id == models.Strategy.id)
                .join(models.StrategyRun, models.StrategyRun.strategy_version_id == models.StrategyVersion.id)
                .join(models.StudyRun, models.StudyRun.run_id == models.StrategyRun.id)
                .where(
                    models.StudyRun.study_id == stored.id,
                    models.StudyRun.role == StudyRole.BENCHMARK,
                    models.StudyRun.status == RunStatus.COMPLETED,
                )
            ).scalars()
        )
    for name in names:
        if name in have:
            continue
        bench = _benchmark_run(BENCHMARK_FILES[name], base, start, end)
        bench_entry = registry_entry(name)
        bench_variant = CandleVariantSpec("base", bench.run.slippage_bps)
        context = _context(
            store,
            study_row,
            StudyRole.BENCHMARK,
            name,
            bench_entry.kind,
            bench.run.instrument_id,
            {bench_variant.name: None},
            studied=(base.run.strategy.name, entry.kind),
        )
        code = backtest(settings, bench, store, variants=[bench_variant], study=context)
        if code != EXIT_OK:
            return code
    return EXIT_OK
