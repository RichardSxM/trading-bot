"""The ``lab`` command (spec 0008, *API surface*): ``docker compose run --rm lab <command>``.

Exit codes: 0 done, 1 a variant failed (``replay``), the parity ratio is under 0.95 (``parity``),
or a day could not be imported (``import-tardis``), 2 required days missing (or, for ``parity``, no
decision left to compare), 3 the catalog cap would be passed, 64 bad input (every problem listed),
78 ``TRADING_ENV=live`` (the lab only ever runs in demo, AC-17) or, for ``replay``, an image built
without ``GIT_COMMIT``, 130 interrupted by SIGINT or
SIGTERM (``replay``, ``parity``, AC-13).

``replay`` runs ``base`` plus the variants its flags add (AC-6); ``parity`` replays the paper loop's
strategy in memory and compares it with the paper decisions (AC-15). Only ``replay`` connects to
Postgres, so the other commands run with ``--no-deps``.
"""

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Final, NoReturn

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lab.paper_csv import read_paper_csv
from lab.parity import parity
from lab.replay import PostgresStore, replay
from lab.runfile import Overrides, RunFileError, load_run
from lab.settings import LabSettings, ProbeSwitch
from lab.strategies import registry_entry
from lab.tardis_import import import_tardis, prune
from trading_core.boot import boot
from trading_core.lab.parity import ParityInputError
from trading_core.lab.tardis_days import import_days
from trading_core.lab.variants import VariantError, VariantSpec, expand_variants
from trading_core.logs import configure_logging
from trading_core.secrets import POSTGRES_PASSWORD

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
    check = commands.add_parser("parity", help="compare a replay of the paper strategy with the paper decisions")
    check.add_argument("runfile", type=Path, help="a run file with [paper]")
    check.add_argument("csv", type=Path, help="the CSV from scripts/fetch-paper-decisions.sh")
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
    if args.command == "prune":
        sys.exit(_prune(args))
    sys.exit(EXIT_USAGE)  # unreachable: the subparser is required


if __name__ == "__main__":
    main()
