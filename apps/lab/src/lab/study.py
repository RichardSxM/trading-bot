"""``lab study`` and ``lab holdout`` (spec 0015 AC-2 to AC-5): a strategy tested under a plan fixed in advance.

A study file is the lab's run file verbatim plus a ``[study]`` table. ``lab study`` validates it, loads
the packaged thresholds, and stores the study (its resolved plan and thresholds, each with a hash).
Every grid point then runs once over the development range: the file's ``from`` to the day before
``holdout_start``. Running it again with the same plan resumes, skipping grid points that already
completed; a different plan under the same name exits 64 before any run.

``lab holdout`` holds the study's advisory lock, needs every grid point completed, picks the final one
(best development daily Sharpe, ties to the lowest index), and runs it once from ``holdout_start`` to
the last complete UTC day of the catalog, unless that frozen version already spent its holdout. Then
the benchmarks (``buy_and_hold``, plus ``macd_baseline`` for an hour level study, minus the studied
strategy itself) run over the same range as candle runs. A candle study of a scalping strategy is a
screen and never holds out, unless its plan has ``candle_gate`` true (spec 0017 AC-20). When this study
already ran its holdout, a
rerun only fills in the benchmarks still missing, over the stored holdout range. Every run writes its
bundle beside its report, for ``scripts/push-bundles.sh``.

A replay study (AC-24) replays free Tardis days only: every 1st of the month from ``from`` to the day
before ``holdout_start`` for development, and every 1st from ``holdout_start`` to the latest one before
the current UTC day for the holdout. A planned day without a manifest exits 2 before anything is
written; a day gapped in every hour is dropped when the plan is made. Each grid point runs as its own
``replay()`` call with one ``base`` variant, so each gets its own strategy version and frozen hash.
"""

import hashlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from lab.candle_backtest import backtest
from lab.candle_fetch import candle_tree
from lab.catalog import SourceTree
from lab.instruments import pinned_instrument
from lab.replay import PostgresStore, plan_days, replay
from lab.runfile import (
    LoadedCandleRun,
    LoadedRun,
    Overrides,
    RunFileError,
    candle_run_from_raw,
    read_toml,
    replay_run_from_raw,
)
from lab.settings import LabSettings
from lab.strategies import ConfigValue, StrategyConfigError, StrategyEntry, registry_entry
from lab.study_rows import StudyContext
from trading_core.gate.checks import benchmark_names
from trading_core.gate.thresholds import Thresholds
from trading_core.lab.candles import Month
from trading_core.lab.errors import MissingDaysError, StudyDaysError
from trading_core.lab.screen import is_candle_gate
from trading_core.lab.study_days import development_days, dropped_days, holdout_days, import_command
from trading_core.lab.variants import (
    BASE,
    MAX_REPLAY_GRID_VARIANTS,
    TAKER,
    CandleVariantSpec,
    GridValue,
    VariantSpec,
    expand_candle_variants,
    expand_grid,
)
from trading_core.node.recording import instrument_row
from trading_core.recording import models, repository
from trading_core.recording.enums import StrategyKind, StudyLab, StudyRole
from trading_core.recording.rows import JsonObject, JsonValue, StrategyVersionRow, StudyRow, canonical_json
from trading_core.thresholds_file import packaged_thresholds

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_MISSING_DAYS: Final = 2
EXIT_USAGE: Final = 64
BENCHMARK_FILES: Final = {"buy_and_hold": "runs/buy_and_hold.toml", "macd_baseline": "runs/macd_baseline.toml"}
_DAY_NS: Final = 86_400 * 1_000_000_000
# Where a replay study looks for its days when the caller names no catalog (the settings default).
CATALOG_DIR: Final[Path] = LabSettings.model_fields["lab_catalog_dir"].default

# A grid point: its name (``base``, or ``grid-1``, ``grid-2``, …) and the settings it adds.
type GridPoint = tuple[str, dict[str, GridValue]]
# What a benchmark run takes from the study's run file, whichever lab: its coins (sorted), maker fee, taker fee.
type Market = tuple[tuple[str, ...], Decimal, Decimal]


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
    # A candle scalping study judged by the gate (spec 0017 AC-20). None, never False, by default, so a
    # plan without it dumps (``exclude_none``) and hashes as before.
    candle_gate: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        candle = (self.first_in_sample_months, self.step_months)
        replay = (self.first_in_sample_days, self.step_days)
        if self.lab == "candle" and (self.entry_mode is not None or any(v is not None for v in replay)):
            raise ValueError("a candle study takes no entry_mode, first_in_sample_days, or step_days")
        if self.lab == "replay" and (self.entry_mode is None or any(v is not None for v in candle)):
            raise ValueError("a replay study needs entry_mode and takes no first_in_sample_months or step_months")
        if self.lab == "replay" and self.candle_gate is not None:
            raise ValueError("a replay study takes no candle_gate")
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
    """A validated study file: its ``[study]`` table, its run file, its grid points, and the resolved plan.

    ``variants`` are a candle study's batch variants (empty for replay); ``dropped`` are a replay
    study's development days gapped in every hour.
    """

    section: StudySection
    run_file: Mapping[str, object]
    loaded: LoadedCandleRun | LoadedRun
    points: tuple[GridPoint, ...]
    thresholds: Thresholds
    plan: dict[str, JsonValue]
    variants: tuple[CandleVariantSpec, ...] = ()
    dropped: tuple[date, ...] = ()

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


def load_study(path: Path, *, catalog_dir: Path = CATALOG_DIR, test_strategies: bool = False) -> LoadedStudy:
    """Read and validate a study file; ``RunFileError`` lists every problem (exit 64).

    A replay study also checks its days in ``catalog_dir``: ``MissingDaysError`` (exit 2) when a
    planned day has no manifest, so nothing is written.
    """
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
    thresholds = packaged_thresholds()
    if section.lab == "replay":
        return _load_replay_study(str(path), data, section, run_file, thresholds, catalog_dir, test_strategies)
    loaded = candle_run_from_raw(str(path), data, run_file, Overrides(), test_strategies=test_strategies)
    start, end = development_range(loaded.run.from_, thresholds)
    if start > end:
        raise RunFileError([f"from {start} is on or after holdout_start {thresholds.holdout_start}"])
    if loaded.run.to >= thresholds.holdout_start:
        raise RunFileError(
            [f"to {loaded.run.to} reaches into the holdout (from {thresholds.holdout_start}); the plan sets the ranges"]
        )
    variants = tuple(expand_candle_variants(loaded.run.slippage_bps, loaded.run.strategy.grid))
    points = tuple((v.name, dict(v.grid_point)) for v in variants)
    return LoadedStudy(
        section,
        run_file,
        replace(loaded, run=loaded.run.model_copy(update={"to": end})),
        points,
        thresholds,
        _plan(section, run_file, points, (start, end), thresholds),
        variants=variants,
    )


def _plan(
    section: StudySection,
    run_file: Mapping[str, object],
    points: tuple[GridPoint, ...],
    development: tuple[date, date],
    thresholds: Thresholds,
    days: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """The resolved plan ``plan_hash`` covers; ``days`` is a replay study's ``development`` and ``dropped`` lists."""
    plan: dict[str, JsonValue] = {
        "study": _plain(section.model_dump(exclude_none=True)),
        "run_file": _plain(run_file),
        "grid": [_plain(point) for _, point in points],
        "ranges": {
            "development": [development[0].isoformat(), development[1].isoformat()],
            "holdout_start": thresholds.holdout_start.isoformat(),
        },
        "folds": dict(section.folds(thresholds)),
    }
    if days is not None:
        plan["days"] = days
    return plan


def replay_points(loaded: LoadedRun) -> tuple[GridPoint, ...]:
    """A replay study's grid points: ``base`` alone without a grid, else the grid capped at 4 (AC-2)."""
    grid = loaded.run.strategy.grid
    return tuple(expand_grid(grid, MAX_REPLAY_GRID_VARIANTS)) if grid else ((BASE, {}),)


def _replay_problems(loaded: LoadedRun, entry: StrategyEntry, section: StudySection) -> list[str]:
    """The replay study rules of AC-2: Tardis only, and ``[study]`` alone sets the entry mode."""
    run = loaded.run
    problems: list[str] = []
    if run.source != "tardis":
        problems.append('source: a replay study uses free Tardis days only; set source = "tardis"')
    problems += [
        f"{table}: entry_mode is set by [study] entry_mode, not here"
        for table, values in (("strategy.config", run.strategy.config), ("strategy.grid", run.strategy.grid))
        if "entry_mode" in values
    ]
    if entry.default_entry_mode is None and section.entry_mode != TAKER:
        problems.append(f'study.entry_mode: {run.strategy.name} has no entry_mode setting, so only "taker" is allowed')
    return problems


def _load_replay_study(
    path: str,
    data: bytes,
    section: StudySection,
    run_file: Mapping[str, object],
    thresholds: Thresholds,
    catalog_dir: Path,
    test_strategies: bool,
) -> LoadedStudy:
    """A replay study: the AC-2 and AC-24 checks, then its days in the Tardis catalog, before any write."""
    loaded = replay_run_from_raw(path, data, run_file, Overrides(), test_strategies=test_strategies, grid_allowed=True)
    run = loaded.run
    entry = registry_entry(run.strategy.name, test_strategies=test_strategies)
    holdout_start = thresholds.holdout_start
    folds = section.folds(thresholds)
    needed = folds["first_in_sample_days"] + folds["step_days"]
    problems = _replay_problems(loaded, entry, section)
    if run.to >= holdout_start:
        problems.append(f"to {run.to} reaches into the holdout (from {holdout_start}); the plan sets the ranges")
    days: list[date] = []
    try:
        days = development_days(run.from_, holdout_start)
    except StudyDaysError as exc:
        problems.append(str(exc))
    else:
        if len(days) < needed:
            problems.append(f"from {run.from_} gives {len(days)} development days; the folds need at least {needed}")
    if problems:
        raise RunFileError(problems)
    tree = SourceTree(catalog_dir / "tardis", run.instrument_id)
    missing = tree.missing_manifests(days)
    if missing:
        raise MissingDaysError(missing)
    dropped = dropped_days(days, plan_days(tree, days).skipped)
    if len(days) - len(dropped) < needed:
        raise RunFileError(
            [
                f"{len(dropped)} development days have gaps in every hour ({', '.join(map(str, dropped))}), "
                f"which leaves {len(days) - len(dropped)}; the folds need at least {needed}"
            ]
        )
    end = holdout_start - timedelta(days=1)
    points = replay_points(loaded)
    day_lists: dict[str, JsonValue] = {
        "development": [d.isoformat() for d in days],
        "dropped": [d.isoformat() for d in dropped],
    }
    return LoadedStudy(
        section,
        run_file,
        replace(loaded, run=loaded.run.model_copy(update={"to": end})),
        points,
        thresholds,
        _plan(section, run_file, points, (run.from_, end), thresholds, day_lists),
        dropped=tuple(dropped),
    )


def missing_days_exit(command: str, days: list[date]) -> int:
    """Print every missing day and the import command that fetches them (AC-24); returns exit 2."""
    for day in days:
        print(f"lab {command}: no manifest for {day} in catalog-lab/tardis", file=sys.stderr)
    print(f"lab {command}: fetch them with: {import_command(days)}", file=sys.stderr)
    log.error("lab_days_missing", extra={"fields": {"days": [d.isoformat() for d in days]}})
    return EXIT_MISSING_DAYS


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
    if study.section.candle_gate and not entry.candle_gate:
        return _refuse("study", [f"study.candle_gate: {run.strategy.name} may not take the candle gate path"])
    configs = {
        name: _point_config(study.loaded, entry, point, study.section.entry_mode) for name, point in study.points
    }
    frozen = {name: entry.frozen(config) for name, config in configs.items()}
    if any(f is None for f in frozen.values()):
        return _refuse("study", [f"{run.strategy.name} has no trading_core.node module, so it cannot be studied"])
    code_hashes = {f.code_hash for f in frozen.values() if f is not None}
    with store.session() as session:
        stored = _load_study(session, study.section.name)
    thresholds = study.thresholds.to_json()
    if stored is not None and stored.plan_hash != study.plan_hash:
        return _refuse("study", [f"study {study.section.name!r} exists with another plan; give a new name"])
    if stored is not None and stored.thresholds_hash != _sha(thresholds):  # type: ignore[arg-type]
        why = "was planned with other thresholds; restore thresholds.toml or give a new name"
        return _refuse("study", [f"study {study.section.name!r} {why}"])
    if stored is not None and stored.code_hash not in code_hashes:
        return _refuse("study", [f"study {study.section.name!r}: the strategy's code changed since it started"])
    first = store.prepare(
        [instrument_row(pinned_instrument(coin)) for coin in _coins(study.loaded)],
        run.strategy.name,
        entry.kind,
        settings.git_commit,
        next(iter(configs.values())),
        next(iter(frozen.values())),
    )
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
    points = {name: i for i, (name, _) in enumerate(study.points)}
    remaining = [(name, point) for name, point in study.points if points[name] not in done]
    log.info(
        "lab_study_planned",
        extra={"fields": {"study": stored.name, "grid_points": len(points), "remaining": [n for n, _ in remaining]}},
    )
    if not remaining:
        print(f"lab study: every grid point of {stored.name!r} has completed; run lab holdout {stored.name}")
        return EXIT_OK
    if isinstance(study.loaded, LoadedRun):
        assert study.section.entry_mode is not None  # StudySection requires it for replay
        return _replay_points(
            settings,
            study.loaded,
            stored,
            store,
            entry,
            study.section.entry_mode,
            [(points[name], point) for name, point in remaining],
            StudyRole.DEVELOPMENT,
            study.dropped,
        )
    coin = study.loaded.run.instruments[0]  # the bundle's instrument: the first coin (spec 0022 AC-12)
    context = _context(store, stored, StudyRole.DEVELOPMENT, run.strategy.name, entry.kind, coin, points)
    variants = [v for v in study.variants if points[v.name] not in done]
    return backtest(settings, study.loaded, store, variants=variants, study=context)


def _version_config(
    entry: StrategyEntry, config: dict[str, ConfigValue], entry_mode: str | None
) -> dict[str, ConfigValue]:
    """A grid point's version config, as ``replay()`` records it for the point's runs (spec 0015 AC-25).

    It takes the study's entry mode when the strategy has the setting; a candle study has none.
    """
    if entry_mode is None or entry.default_entry_mode is None:
        return config
    return {**config, "entry_mode": entry_mode}


def _point_config(
    loaded: LoadedCandleRun | LoadedRun, entry: StrategyEntry, point: Mapping[str, GridValue], entry_mode: str | None
) -> JsonObject:
    """A grid point's version config as the lab records it: a candle run's own (with ``instrument_ids``
    for a multi coin run, spec 0022 AC-3), else ``_version_config`` with the study's entry mode."""
    if isinstance(loaded, LoadedCandleRun):
        return loaded.run.version_config(point)
    config: JsonObject = {**_version_config(entry, {**loaded.run.strategy.config, **point}, entry_mode)}
    return config


def _coins(loaded: LoadedCandleRun | LoadedRun) -> tuple[str, ...]:
    """The run's coins in sorted order: a replay run has one."""
    return loaded.run.instruments if isinstance(loaded, LoadedCandleRun) else (loaded.run.instrument_id,)


def _with_point(loaded: LoadedRun, point: Mapping[str, GridValue]) -> LoadedRun:
    """The replay run of one grid point: ``[strategy.config]`` plus the point, and no grid (AC-2)."""
    strategy = loaded.run.strategy.model_copy(update={"config": {**loaded.run.strategy.config, **point}, "grid": {}})
    return replace(loaded, run=loaded.run.model_copy(update={"strategy": strategy}))


def _replay_points(
    settings: LabSettings,
    loaded: LoadedRun,
    stored: StudyRow,
    store: StudyStore,
    entry: StrategyEntry,
    entry_mode: str,
    points: list[tuple[int, dict[str, GridValue]]],
    role: StudyRole,
    dropped: tuple[date, ...],
) -> int:
    """Each grid point as its own ``replay()`` call with one ``base`` variant (AC-2, AC-3).

    The study's entry mode becomes the variant's, and stays unset for a strategy with no
    ``entry_mode`` setting. A failed point fails only itself (exit 1 at the end); an interrupt or
    missing data stops at once with the replay's own exit code.
    """
    run = loaded.run
    variant = VariantSpec(BASE, run.latency_ms, True, None if entry.default_entry_mode is None else entry_mode)
    failed = False
    for index, point in points:
        context = _context(store, stored, role, run.strategy.name, entry.kind, run.instrument_id, {BASE: index})
        code = replay(
            settings, _with_point(loaded, point), store, variants=[variant], study=context, dropped_days=dropped
        )
        if code == EXIT_FAILED:
            failed = True
        elif code != EXIT_OK:
            return code
    return EXIT_FAILED if failed else EXIT_OK


def last_complete_day(settings: LabSettings, instrument_ids: Sequence[str], start: date) -> date | None:
    """The last UTC day every coin's candle tree holds whole: per coin from its newest month with a
    manifest, then the earliest over the coins (spec 0022 AC-11); None when a coin has none."""
    days = [_coin_last_day(settings, coin, start) for coin in instrument_ids]
    return None if not days or any(d is None for d in days) else min(d for d in days if d is not None)


def _coin_last_day(settings: LabSettings, instrument_id: str, start: date) -> date | None:
    tree = candle_tree(settings.lab_catalog_dir, instrument_id)
    month = Month.of_ns(time.time_ns())
    first = Month.of_ns(int(datetime(start.year, start.month, 1, tzinfo=UTC).timestamp()) * 1_000_000_000)
    while month >= first:
        manifest = tree.read_manifest(month)
        if manifest is not None and manifest.last_ts_ns is not None:
            return date(1970, 1, 1) + timedelta(days=manifest.last_ts_ns // _DAY_NS - 1)
        month = month.previous()
    return None


def _benchmark_run(path: str, market: Market, start: date, end: date) -> LoadedCandleRun:
    """A committed benchmark run file with the study's coins and fees, over ``start``..``end`` (AC-4;
    spec 0022 AC-11: ``instrument_ids`` in place of the file's ``instrument_id`` for several coins)."""
    coins, maker_fee, taker_fee = market
    data, raw = read_toml(Path(path))
    coin_key: dict[str, object] = {"instrument_ids": list(coins)} if len(coins) > 1 else {"instrument_id": coins[0]}
    merged = (
        {k: v for k, v in raw.items() if k not in ("instrument_id", "instrument_ids")}
        | coin_key
        | {
            "maker_fee": format(maker_fee, "f"),
            "taker_fee": format(taker_fee, "f"),
        }
    )
    return candle_run_from_raw(path, data, merged, Overrides(from_=start, to=end))


def run_holdout(settings: LabSettings, name: str, store: StudyStore, *, today: date) -> int:
    """``lab holdout`` (AC-4, AC-5): the final grid point once over the holdout, then the benchmarks.

    ``today`` is the current UTC date (the CLI passes it); a replay study's holdout days end before it (AC-24).
    """
    with store.session() as session:
        stored = _load_study(session, name)
    if stored is None:
        return _refuse("holdout", [f"no study called {name!r}"])
    screen = stored.lab == StudyLab.CANDLE and _studied_kind(stored, settings) == StrategyKind.SCALPING
    if screen and not is_candle_gate(stored.plan):
        return _refuse("holdout", ["a candle screen never holds out"])
    with store.lock(stored.id):
        return _holdout_locked(settings, stored, store, today)


def _studied_kind(stored: StudyRow, settings: LabSettings) -> StrategyKind | None:
    """The registry kind of the study's strategy, read from its stored plan (None when not registered)."""
    run_file = stored.plan.get("run_file")
    strategy = run_file.get("strategy") if isinstance(run_file, Mapping) else None
    name = strategy.get("name") if isinstance(strategy, Mapping) else None
    try:
        return registry_entry(str(name), test_strategies=settings.lab_test_strategies).kind
    except StrategyConfigError:
        return None


def _holdout_locked(settings: LabSettings, stored: StudyRow, store: StudyStore, today: date) -> int:
    plan: Mapping[str, object] = stored.plan
    run_file = plan["run_file"]
    grid = plan["grid"]
    assert isinstance(run_file, Mapping)
    assert isinstance(grid, list)
    thresholds = Thresholds.from_json(stored.thresholds)
    source, data = f"study:{stored.name}", canonical_json(run_file).encode()
    base: LoadedCandleRun | LoadedRun
    if stored.lab == StudyLab.REPLAY:
        base = replay_run_from_raw(
            source, data, run_file, Overrides(), test_strategies=settings.lab_test_strategies, grid_allowed=True
        )
        points = list(replay_points(base))
        variants: list[CandleVariantSpec] = []
    else:
        base = candle_run_from_raw(source, data, run_file, Overrides())
        variants = expand_candle_variants(base.run.slippage_bps, base.run.strategy.grid)
        points = [(v.name, dict(v.grid_point)) for v in variants]
    market: Market = (_coins(base), base.run.maker_fee, base.run.taker_fee)
    entry = registry_entry(base.run.strategy.name, test_strategies=settings.lab_test_strategies)
    with store.session() as session:
        done = repository.completed_grid_points(session, stored.id)
        missing = [name for i, (name, _) in enumerate(points) if i not in done]
        if missing:
            return _refuse("holdout", [f"grid points without a completed development run: {', '.join(missing)}"])
        final = repository.final_grid_point(session, stored.id)
        if final is None:  # every point completed, so a winner exists; refuse rather than guess
            return _refuse("holdout", [f"study {stored.name!r} has no development winner"])
        final_hash = done[final].frozen_hash
        own = repository.study_holdout(session, stored.id)
        spent = own is None and repository.holdout_spent(session, final_hash)
        have = repository.completed_benchmarks(session, stored.id)
    studied = base.run.strategy.name
    todo = [n for n in benchmark_names(entry.kind, studied) if n not in have]
    start = thresholds.holdout_start
    if own is not None:
        return _resume_benchmarks(settings, stored, store, market, studied, entry.kind, final_hash, own, todo)
    if spent:
        return _refuse("holdout", [f"frozen version {final_hash[:12]} already spent its holdout"])
    point = points[final][1]
    mode = StudySection.model_validate(plan["study"]).entry_mode if isinstance(base, LoadedRun) else None
    frozen = entry.frozen(_point_config(base, entry, point, mode))
    if frozen is None or frozen.frozen_hash != final_hash:
        return _refuse(
            "holdout", ["the strategy's code or engine changed since its development runs; start a new study"]
        )
    if isinstance(base, LoadedRun):
        section = StudySection.model_validate(plan["study"])
        assert section.entry_mode is not None  # StudySection requires it for replay
        return _replay_holdout(settings, stored, store, base, entry, section.entry_mode, final, point, todo, today)
    variant = variants[final]
    end = last_complete_day(settings, base.run.instruments, thresholds.holdout_start)
    if end is None or end < thresholds.holdout_start:
        return _refuse(
            "holdout", [f"the catalog holds no complete day from {thresholds.holdout_start}; run lab fetch-candles"]
        )
    holdout = replace(base, run=base.run.model_copy(update={"from_": start, "to": end}))
    log.info(
        "lab_holdout_planned",
        extra={"fields": {"study": stored.name, "grid_point": final, "range": [str(start), str(end)]}},
    )
    context = _context(
        store,
        stored,
        StudyRole.HOLDOUT,
        base.run.strategy.name,
        entry.kind,
        base.run.instruments[0],
        {variant.name: final},
    )
    code = backtest(settings, holdout, store, variants=[variant], study=context)
    if code != EXIT_OK:
        return code
    return _run_benchmarks(settings, stored, store, market, studied, entry.kind, todo, start, end)


def _replay_holdout(
    settings: LabSettings,
    stored: StudyRow,
    store: StudyStore,
    base: LoadedRun,
    entry: StrategyEntry,
    entry_mode: str,
    final: int,
    point: dict[str, GridValue],
    todo: list[str],
    today: date,
) -> int:
    """A replay study's holdout (AC-24): the final grid point once over the holdout days, then the benchmarks.

    The holdout days are every 1st of the month from ``holdout_start`` to the latest one before
    ``today``; a missing one exits 2 before any run, and one gapped in every hour is dropped. The
    benchmarks run as candle runs over ``holdout_start``..the last holdout day.
    """
    start = Thresholds.from_json(stored.thresholds).holdout_start
    days = holdout_days(start, today)
    if not days:
        return _refuse("holdout", [f"no holdout day yet: the first is the 1st of the month after {start}"])
    tree = SourceTree(settings.lab_catalog_dir / "tardis", base.run.instrument_id)
    missing = tree.missing_manifests(days)
    if missing:
        return missing_days_exit("holdout", missing)
    dropped = tuple(dropped_days(days, plan_days(tree, days).skipped))
    holdout = replace(base, run=base.run.model_copy(update={"from_": start, "to": days[-1]}))
    log.info(
        "lab_holdout_planned",
        extra={
            "fields": {
                "study": stored.name,
                "grid_point": final,
                "days": [d.isoformat() for d in days],
                "dropped": [d.isoformat() for d in dropped],
            }
        },
    )
    code = _replay_points(
        settings, holdout, stored, store, entry, entry_mode, [(final, point)], StudyRole.HOLDOUT, dropped
    )
    if code != EXIT_OK:
        return code
    market: Market = ((base.run.instrument_id,), base.run.maker_fee, base.run.taker_fee)
    return _run_benchmarks(settings, stored, store, market, base.run.strategy.name, entry.kind, todo, start, days[-1])


def _resume_benchmarks(
    settings: LabSettings,
    stored: StudyRow,
    store: StudyStore,
    market: Market,
    studied: str,
    kind: StrategyKind,
    final_hash: str,
    own: tuple[str, datetime | None],
    todo: list[str],
) -> int:
    """The study already ran its holdout (AC-5): never again; run only the missing benchmarks over its stored range.

    No frozen hash recheck and no catalog end check, because benchmarks don't use the studied code,
    and the range ends at the holdout run's ``data_end_at``, never a recomputed catalog end.
    """
    own_hash, data_end_at = own
    if own_hash != final_hash:
        return _refuse(
            "holdout",
            [f"the study's holdout ran frozen version {own_hash[:12]}, not the final grid point's {final_hash[:12]}"],
        )
    if not todo:
        return _refuse("holdout", [f"study {stored.name!r} already ran its holdout and every benchmark"])
    if data_end_at is None:
        return _refuse("holdout", [f"study {stored.name!r}: the holdout run has no data_end_at"])
    start = Thresholds.from_json(stored.thresholds).holdout_start
    end = (data_end_at - timedelta(microseconds=1)).astimezone(UTC).date()  # data_end_at is the exclusive midnight
    log.info(
        "lab_holdout_resumed",
        extra={"fields": {"study": stored.name, "benchmarks": todo, "range": [str(start), str(end)]}},
    )
    return _run_benchmarks(settings, stored, store, market, studied, kind, todo, start, end)


def _run_benchmarks(
    settings: LabSettings,
    stored: StudyRow,
    store: StudyStore,
    market: Market,
    studied: str,
    kind: StrategyKind,
    names: list[str],
    start: date,
    end: date,
) -> int:
    """Each named benchmark once over ``start``..``end`` as a candle run (AC-4); stops at the first failure.

    Any failed benchmark exits 1 (AC-5), whatever the backtest's own code (2 for missing candles, for example).
    """
    for name in names:
        bench = _benchmark_run(BENCHMARK_FILES[name], market, start, end)
        bench_entry = registry_entry(name)
        bench_variant = CandleVariantSpec("base", bench.run.slippage_bps)
        context = _context(
            store,
            stored,
            StudyRole.BENCHMARK,
            name,
            bench_entry.kind,
            bench.run.instruments[0],
            {bench_variant.name: None},
            studied=(studied, kind),
        )
        code = backtest(settings, bench, store, variants=[bench_variant], study=context)
        if code != EXIT_OK:
            return EXIT_FAILED
    return EXIT_OK
