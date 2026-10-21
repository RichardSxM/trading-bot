"""The ``lab`` command (spec 0008, *API surface*): ``docker compose run --rm lab <command>``.

Exit codes: 0 done, 1 a variant failed (``replay``), the parity ratio is under 0.95 (``parity``),
or a day could not be imported (``import-tardis``), 2 required days missing (or, for ``parity``, no
decision left to compare), 3 the catalog cap would be passed, 64 bad input (every problem listed),
78 ``TRADING_ENV=live`` (the lab only ever runs in demo, AC-17) or, for ``replay`` and ``backtest``,
an image built without ``GIT_COMMIT``, 130 interrupted by SIGINT or SIGTERM (``replay``, ``parity``,
``backtest``, ``candle-parity``; AC-13, spec 0014 AC-18).

``replay`` runs ``base`` plus the variants its flags add (AC-6); ``parity`` replays the paper loop's
strategy in memory and compares it with the paper decisions (AC-15). ``fetch-candles`` and
``import-funding`` fill the candle lab's tree (spec 0014; 1 bad rows or a failed fetch, 3 the cap),
and ``backtest`` runs an hour level strategy over it (2 missing candles or funding; ``--lookahead-check``
reruns the first variant cut at three hours, AC-19). ``candle-parity`` runs one strategy over own
days through both the order book and the candle path and compares their entries (1 under 0.95, 2
missing data or nothing to compare, AC-21). ``study`` and ``holdout`` run a strategy under a plan
fixed in advance (spec 0015: 64 a bad study file, a reused name with another plan, missing grid
points, or a spent holdout; 2 a replay study day with no manifest, every one listed with the
``import-tardis`` command that fetches them); ``gate <study> --dry-run`` previews the promotion gate's verdict and
writes nothing (64 an unknown study). ``audit`` measures the Tardis development days' spreads, moves,
and order flow into report files (spec 0017 AC-1: 64 a range reaching the holdout, 2 a missing day).
Only ``replay``, ``backtest``, ``study``, ``holdout``, and ``gate`` connect to Postgres, so the other
commands run with ``--no-deps``.
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, NoReturn

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lab.audit import AuditRangeError, audit_days, run_audit
from lab.candle_backtest import backtest
from lab.candle_fetch import fetch_candles
from lab.candle_parity import candle_parity
from lab.funding_import import import_funding
from lab.paper_csv import read_paper_csv
from lab.parity import parity
from lab.replay import PostgresStore, replay
from lab.runfile import Overrides, RunFileError, load_candle_run, load_run
from lab.settings import LabSettings, ProbeSwitch
from lab.strategies import registry_entry
from lab.study import StudyStore, load_study, missing_days_exit, run_holdout, run_study
from lab.tardis_import import import_context, import_tardis, prune
from trading_core.boot import boot
from trading_core.gate.thresholds import ThresholdsError
from trading_core.gate.verdict import evaluate_candidate
from trading_core.lab.candles import fetch_months
from trading_core.lab.errors import CandleInputError, MissingDaysError
from trading_core.lab.parity import ParityInputError
from trading_core.lab.tardis_days import import_days
from trading_core.lab.variants import VariantError, VariantSpec, expand_candle_variants, expand_variants
from trading_core.logs import configure_logging
from trading_core.recording.gate_queries import UnknownStudyError, study_evidence
from trading_core.secrets import POSTGRES_PASSWORD
from trading_core.thresholds_file import packaged_thresholds

log = logging.getLogger(__name__)

PROCESS: Final = "lab"
EXIT_USAGE: Final = 64
EXIT_CONFIG: Final = 78


class _Parser(argparse.ArgumentParser):
    """Bad arguments exit 64 (EX_USAGE), not argparse's 2, which the lab uses for missing days."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _day(text: str) -> date:
    return date.fromisoformat(text)


def parser() -> argparse.ArgumentParser:
    """The command line."""
    root = _Parser(prog="lab", description="Replay recorded order books through the Nautilus backtest engine.")
    commands = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run = commands.add_parser("replay", help="replay a run file over recorded days")
    run.add_argument("runfile", type=Path)
    run.add_argument("--from", dest="from_", type=_day, help="first UTC day, overrides the run file")
    run.add_argument("--to", type=_day, help="last UTC day, overrides the run file")
    run.add_argument("--seed", type=int, help="overrides the run file's seed")
    run.add_argument("--queue-bound", action="store_true", help="add the queue=optimistic variant")
    run.add_argument("--sweep-latency", action="store_true", help="add the latency=0 and latency=4x variants")
    run.add_argument("--pair-entry", action="store_true", help="add the entry=taker variant")
    tardis = commands.add_parser("import-tardis", help="download and convert free Tardis days (first of month)")
    tardis.add_argument("from_", metavar="from", type=_day, help="first UTC day, a first of month")
    tardis.add_argument("to", type=_day, help="last UTC day, a first of month")
    tardis.add_argument(
        "--context",
        action="store_true",
        help="backfill only the market context tables of days already imported (spec 0009)",
    )
    check = commands.add_parser("parity", help="compare a replay of the paper strategy with the paper decisions")
    check.add_argument("runfile", type=Path, help="a run file with [paper]")
    check.add_argument("csv", type=Path, help="the CSV from scripts/fetch-paper-decisions.sh")
    test = commands.add_parser("backtest", help="backtest an hour level strategy over stored candles (spec 0014)")
    test.add_argument("runfile", type=Path)
    test.add_argument("--from", dest="from_", type=_day, help="first UTC day, overrides the run file")
    test.add_argument("--to", type=_day, help="last UTC day, overrides the run file")
    test.add_argument("--seed", type=int, help="overrides the run file's seed")
    test.add_argument(
        "--sweep-slippage", action="store_true", help="add the slippage=0 and slippage=2x variants (not with a grid)"
    )
    test.add_argument(
        "--lookahead-check",
        action="store_true",
        help="run the first variant again cut at three seeded hours and compare its decisions",
    )
    side = commands.add_parser(
        "candle-parity", help="compare the candle path with the order book over own days (spec 0014)"
    )
    side.add_argument("runfile", type=Path, help="a candle run file, [parity] optional")
    side.add_argument("--from", dest="from_", type=_day, help="first UTC day, overrides the run file")
    side.add_argument("--to", type=_day, help="last UTC day, overrides the run file")
    candles = commands.add_parser("fetch-candles", help="fetch Kraken 1 minute candles and funding (spec 0014)")
    candles.add_argument("from_", metavar="from", help="first UTC month, YYYY-MM")
    candles.add_argument("to", help="last UTC month, YYYY-MM")
    candles.add_argument("--refetch", action="store_true", help="fetch complete months again too")
    funding = commands.add_parser("import-funding", help="merge Kraken's funding export ZIP or CSV (spec 0014)")
    funding.add_argument("file", type=Path, help="the export ZIP, or its PF_XBTUSD.csv")
    study = commands.add_parser("study", help="run every grid point of a study file over its development range")
    study.add_argument("studyfile", type=Path)
    holdout = commands.add_parser("holdout", help="run a study's final grid point once over the holdout")
    holdout.add_argument("study", help="the study's name")
    preview = commands.add_parser("gate", help="preview the promotion gate's verdict on a study (writes nothing)")
    preview.add_argument("study", help="the study's name")
    preview.add_argument("--dry-run", action="store_true", required=True, help="required: the lab only previews")
    audit = commands.add_parser("audit", help="measure spreads, moves, and order flow of the Tardis development days")
    audit.add_argument("--from", dest="from_", type=_day, help="first UTC day (default 2022-04-01)")
    audit.add_argument("--to", type=_day, help="last UTC day (default the day before the holdout)")
    drop = commands.add_parser("prune", help="delete a source tree of the lab catalog")
    drop.add_argument("--source", choices=("tardis", "own"), help="the tree to delete (both without it)")
    return root


def _refuse_live() -> None:
    if os.environ.get("TRADING_ENV", "").strip().lower() == "live":
        configure_logging(process=PROCESS, env="live")
        log.critical(
            "the lab runs in demo only, refusing to start",
            extra={"fields": {"code": "lab_live_refused"}},
        )
        sys.exit(EXIT_CONFIG)


def _unknown_commit(settings: LabSettings) -> bool:
    """A replay records its strategy version by commit, so without one every code revision shares a version."""
    if settings.git_commit.strip().lower() not in ("", "unknown"):
        return False
    print(
        "lab replay: the image has no commit; build it with `GIT_COMMIT=$(git rev-parse HEAD) "
        "docker compose --profile lab build lab`",
        file=sys.stderr,
    )
    log.critical(
        "the image was built without GIT_COMMIT, refusing to record a replay",
        extra={"fields": {"code": "lab_unknown_commit"}},
    )
    return True


def _replay(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    problems: list[str] = []
    loaded = None
    variants: list[VariantSpec] = []
    test_strategies = ProbeSwitch().lab_test_strategies
    try:
        loaded = load_run(
            args.runfile, Overrides(from_=args.from_, to=args.to, seed=args.seed), test_strategies=test_strategies
        )
    except RunFileError as exc:
        problems += exc.problems
    if loaded is not None:
        run = loaded.run
        try:
            variants = expand_variants(
                run.latency_ms,
                registry_entry(run.strategy.name, test_strategies=test_strategies).entry_mode(run.strategy_values()),
                queue_bound=args.queue_bound,
                sweep_latency=args.sweep_latency,
                pair_entry=args.pair_entry,
            )
        except VariantError as exc:
            problems.append(str(exc))
    if problems or loaded is None:
        for problem in problems:
            print(f"lab replay: {problem}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": problems}})
        return EXIT_USAGE

    started = boot("worker", settings_cls=LabSettings, secrets=(POSTGRES_PASSWORD,))
    # The paper loop's recorder logs every fill at INFO; a replay makes thousands.
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    settings = started.settings
    if _unknown_commit(settings):
        return EXIT_CONFIG
    engine = create_engine(settings.database_url(started.secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True)
    try:
        return replay(settings, loaded, PostgresStore(sessionmaker(engine)), variants=variants)
    finally:
        engine.dispose()


def _import_tardis(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    try:
        days = import_days(args.from_, args.to)
    except ValueError as exc:
        print(f"lab import-tardis: {exc}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": [str(exc)]}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings)
    if args.context:
        return import_context(started.settings, days)
    return import_tardis(started.settings, days)


def _parity(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    problems: list[str] = []
    loaded = None
    paper = None
    try:
        loaded = load_run(args.runfile, Overrides())
    except RunFileError as exc:
        problems += exc.problems
    if loaded is not None and loaded.run.paper is None:
        problems.append(f"{args.runfile}: lab parity needs a run file with [paper]")
    try:
        paper = read_paper_csv(args.csv)
    except ParityInputError as exc:
        problems += exc.problems
    if problems or loaded is None or paper is None:
        for problem in problems:
            print(f"lab parity: {problem}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": problems}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings)
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    return parity(started.settings, loaded, paper)


def _backtest(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    try:
        loaded = load_candle_run(
            args.runfile,
            Overrides(from_=args.from_, to=args.to, seed=args.seed),
            test_strategies=ProbeSwitch().lab_test_strategies,
        )
        run = loaded.run
        variants = expand_candle_variants(run.slippage_bps, run.strategy.grid, sweep_slippage=args.sweep_slippage)
    except (RunFileError, VariantError) as exc:
        problems = exc.problems if isinstance(exc, RunFileError) else [str(exc)]
        for problem in problems:
            print(f"lab backtest: {problem}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": problems}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings, secrets=(POSTGRES_PASSWORD,))
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    settings = started.settings
    if _unknown_commit(settings):
        return EXIT_CONFIG
    engine = create_engine(settings.database_url(started.secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True)
    try:
        return backtest(
            settings,
            loaded,
            PostgresStore(sessionmaker(engine)),
            variants=variants,
            lookahead_check=args.lookahead_check,
        )
    finally:
        engine.dispose()


def _study_store() -> tuple[LabSettings, StudyStore] | int:
    """Boot as ``lab_app`` with a commit, or the exit code to stop with."""
    started = boot("worker", settings_cls=LabSettings, secrets=(POSTGRES_PASSWORD,))
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    settings = started.settings
    if _unknown_commit(settings):
        return EXIT_CONFIG
    engine = create_engine(settings.database_url(started.secrets[POSTGRES_PASSWORD]), pool_size=2, pool_pre_ping=True)
    return settings, StudyStore(engine)


def _study(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    # Booted first: a replay study checks its days in the settings' catalog before anything is written.
    ready = _study_store()
    if isinstance(ready, int):
        return ready
    settings, store = ready
    try:
        try:
            loaded = load_study(
                args.studyfile, catalog_dir=settings.lab_catalog_dir, test_strategies=settings.lab_test_strategies
            )
        except MissingDaysError as exc:
            return missing_days_exit("study", exc.days)
        except (RunFileError, VariantError, ThresholdsError) as exc:
            problems = exc.problems if isinstance(exc, RunFileError) else [str(exc)]
            for problem in problems:
                print(f"lab study: {problem}", file=sys.stderr)
            log.error("lab_bad_input", extra={"fields": {"problems": problems}})
            return EXIT_USAGE
        return run_study(settings, loaded, store)
    finally:
        store.engine.dispose()


def _holdout(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    ready = _study_store()
    if isinstance(ready, int):
        return ready
    settings, store = ready
    try:
        return run_holdout(settings, args.study, store, today=datetime.now(UTC).date())
    finally:
        store.engine.dispose()


def _gate_preview(args: argparse.Namespace) -> int:
    """``lab gate <study> --dry-run`` (spec 0015 AC-13): the same pure gate over the laptop Postgres; writes nothing."""
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    started = boot("worker", settings_cls=LabSettings, secrets=(POSTGRES_PASSWORD,))
    engine = create_engine(
        started.settings.database_url(started.secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True
    )
    try:
        with Session(engine) as session:
            evidence = study_evidence(session, args.study)
            session.rollback()
    except UnknownStudyError:
        print(f"lab gate: no study called {args.study!r}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        engine.dispose()
    result = evaluate_candidate(evidence.inputs)
    print(
        json.dumps(
            {
                "study": args.study,
                "verdict": result.verdict.value,
                "reasons": [r.to_json() for r in result.reasons],
                "metrics": result.metrics,
                "folds": [dataclasses.asdict(f) for f in result.folds],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _candle_parity(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    try:
        loaded = load_candle_run(args.runfile, Overrides(from_=args.from_, to=args.to))
    except RunFileError as exc:
        problems = exc.problems
    else:
        grid = loaded.run.strategy.grid
        problems = ["strategy.grid: lab candle-parity runs one config; remove the grid"] if grid else []
    if problems:
        for problem in problems:
            print(f"lab candle-parity: {problem}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": problems}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings)
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    return candle_parity(started.settings, loaded)


def _fetch_candles(args: argparse.Namespace) -> int:
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    try:
        months = fetch_months(args.from_, args.to, time.time_ns())
    except CandleInputError as exc:
        print(f"lab fetch-candles: {exc}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": [str(exc)]}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings)
    return fetch_candles(started.settings, months, refetch=args.refetch)


def _import_funding(args: argparse.Namespace) -> int:
    started = boot("worker", settings_cls=LabSettings)
    return import_funding(started.settings, args.file)


def _audit(args: argparse.Namespace) -> int:
    """``lab audit`` (spec 0017 AC-1): files only, no database."""
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    thresholds = packaged_thresholds()
    try:
        days = audit_days(args.from_, args.to, thresholds.holdout_start)
    except AuditRangeError as exc:
        for problem in exc.problems:
            print(f"lab audit: {problem}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": exc.problems}})
        return EXIT_USAGE
    started = boot("worker", settings_cls=LabSettings)
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    return run_audit(started.settings, days, thresholds.fee_hurdle_k, datetime.now(UTC))


def _prune(args: argparse.Namespace) -> int:
    started = boot("worker", settings_cls=LabSettings)
    try:
        freed = prune(started.settings, args.source)
    except OSError as exc:
        print(f"lab prune: {exc}", file=sys.stderr)
        log.error("lab_prune_failed", extra={"fields": {"error": str(exc)}})
        return 1
    print(f"freed {freed} bytes")
    return 0


def main(argv: list[str] | None = None) -> NoReturn:
    """Entry point of the ``lab`` console script."""
    _refuse_live()
    args = parser().parse_args(argv)
    if args.command == "replay":
        sys.exit(_replay(args))
    if args.command == "import-tardis":
        sys.exit(_import_tardis(args))
    if args.command == "parity":
        sys.exit(_parity(args))
    if args.command == "backtest":
        sys.exit(_backtest(args))
    if args.command == "candle-parity":
        sys.exit(_candle_parity(args))
    if args.command == "fetch-candles":
        sys.exit(_fetch_candles(args))
    if args.command == "import-funding":
        sys.exit(_import_funding(args))
    if args.command == "study":
        sys.exit(_study(args))
    if args.command == "holdout":
        sys.exit(_holdout(args))
    if args.command == "gate":
        sys.exit(_gate_preview(args))
    if args.command == "audit":
        sys.exit(_audit(args))
    if args.command == "prune":
        sys.exit(_prune(args))
    sys.exit(EXIT_USAGE)  # unreachable: the subparser is required


if __name__ == "__main__":
    main()
