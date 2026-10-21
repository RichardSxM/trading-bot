"""The ``gate`` command (spec 0015, *API surface*): ``kfs-compose run --rm gate <command>`` on the VPS.

``import [dir]`` loads lab bundle files (exit 1 when any file was refused); ``evaluate <study>`` appends
one ``candidate → paper`` promotion and prints its verdict, reasons, and metrics (64 for an unknown
study or one with nothing to evaluate). ``promote --manual --step candidate-paper`` appends a manual
pass after you type the strategy's name (64 without a terminal, an empty note, a wrong name, or the
live step); ``versions`` lists every version with its frozen hash and stage; ``check-version`` reads
``trader.version`` JSON on stdin and exits 0 when that version holds its paper pass, 3 when not, and 1 on
any error (``kfs-deploy`` runs it before the drain, AC-26).

Runs as ``gate_app`` in either ``TRADING_ENV``; everything it
imports is demo.
"""

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from gate.check_version import CheckVersionRefused, check_version
from gate.evaluate import CandleScreenError, NothingToEvaluateError, evaluate_candidate_step, evaluate_paper_step
from gate.importer import import_folder
from gate.manual import ManualPromotionRefused, promote_manual, versions
from gate.settings import GateSettings
from trading_core.boot import boot
from trading_core.logs import configure_logging
from trading_core.recording.gate_queries import UnknownFrozenHashError, UnknownStudyError
from trading_core.secrets import POSTGRES_PASSWORD

log = logging.getLogger(__name__)

PROCESS: Final = "gate"
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 64
EXIT_NOT_PROMOTED: Final = 3


class _Parser(argparse.ArgumentParser):
    """Bad arguments exit 64 (EX_USAGE)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def parser() -> argparse.ArgumentParser:
    """The command line."""
    root = _Parser(prog="gate", description="The promotion gate (spec 0015).")
    commands = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    load = commands.add_parser("import", help="import lab bundle files")
    load.add_argument("dir", nargs="?", type=Path, help="folder of bundle files (default GATE_BUNDLE_DIR)")
    evaluate = commands.add_parser("evaluate", help="evaluate a study, or a paper record, and append its promotion")
    evaluate.add_argument("study", nargs="?", help="the study's name (the candidate-paper step)")
    evaluate.add_argument("--step", choices=("candidate-paper", "paper-live"), default="candidate-paper")
    evaluate.add_argument("--frozen-hash", dest="frozen_hash", help="the version's frozen hash (the paper-live step)")
    promote = commands.add_parser("promote", help="promote a version to paper by hand (needs a terminal)")
    promote.add_argument("--manual", action="store_true", required=True, help="required: this is a manual decision")
    promote.add_argument("--step", required=True, choices=("candidate-paper", "paper-live"))
    promote.add_argument("--version", required=True, type=UUID, help="the strategy version id (gate versions)")
    promote.add_argument("--note", required=True, help="why, kept on the promotion")
    listing = commands.add_parser("versions", help="list strategy versions with their frozen hash and stage")
    listing.add_argument("--strategy", help="only this strategy")
    commands.add_parser("check-version", help="read trader.version JSON on stdin; exit 0 when it holds its paper pass")
    return root


def _import(settings: GateSettings, engine_url: str, folder: Path | None) -> int:
    engine = create_engine(engine_url, pool_size=1, pool_pre_ping=True)
    try:
        summary = import_folder(sessionmaker(engine), folder or settings.gate_bundle_dir)
    finally:
        engine.dispose()
    print(f"loaded {len(summary.loaded)}, already there {len(summary.skipped)}, refused {len(summary.refused)}")
    for name, why in summary.refused.items():
        print(f"refused {name}: {why}", file=sys.stderr)
    return EXIT_REFUSED if summary.refused else EXIT_OK


def _evaluate(settings: GateSettings, engine_url: str, args: argparse.Namespace) -> int:
    paper = args.step == "paper-live"
    target = args.frozen_hash if paper else args.study
    if not target or (paper and args.study) or (not paper and args.frozen_hash):
        print("gate evaluate: give a study name, or --step paper-live --frozen-hash <h>", file=sys.stderr)
        return EXIT_USAGE
    engine = create_engine(engine_url, pool_size=2, pool_pre_ping=True)
    now = datetime.now(UTC)
    try:
        if paper:
            done = evaluate_paper_step(engine, target, gate_commit=settings.git_commit, now=now)
        else:
            done = evaluate_candidate_step(engine, target, gate_commit=settings.git_commit, now=now)
    except (UnknownStudyError, NothingToEvaluateError, UnknownFrozenHashError, CandleScreenError) as exc:
        if isinstance(exc, UnknownStudyError):
            message = f"no study called {target!r}"
        elif isinstance(exc, UnknownFrozenHashError):
            message = f"no version with frozen hash {target!r}"
        else:
            message = str(exc)
        print(f"gate evaluate: {message}", file=sys.stderr)
        log.error("gate_bad_input", extra={"fields": {"problems": [message]}})
        return EXIT_USAGE
    finally:
        engine.dispose()
    result = done.result
    print(
        json.dumps(
            {
                "promotion_id": str(done.promotion_id),
                "verdict": result.verdict.value,
                "reasons": [r.to_json() for r in result.reasons],
                "metrics": result.metrics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    log.info(
        "gate_evaluated",
        extra={
            "fields": {
                "step": args.step,
                "target": target,
                "promotion_id": str(done.promotion_id),
                "verdict": result.verdict.value,
            }
        },
    )
    return EXIT_OK


def _promote(engine_url: str, args: argparse.Namespace) -> int:
    if args.step != "candidate-paper":
        print(
            "gate promote: only the gate can promote to live; a manual promotion is candidate-paper only",
            file=sys.stderr,
        )
        return EXIT_USAGE
    engine = create_engine(engine_url, pool_size=1)
    try:
        with Session(engine) as session:
            promotion_id = promote_manual(
                session,
                args.version,
                args.note,
                is_tty=sys.stdin.isatty() and sys.stdout.isatty(),
                confirm=input,
                now=datetime.now(UTC),
            )
            session.commit()
    except ManualPromotionRefused as exc:
        print(f"gate promote: {exc}", file=sys.stderr)
        log.error("gate_manual_promotion_refused", extra={"fields": {"version": str(args.version), "why": str(exc)}})
        return EXIT_USAGE
    finally:
        engine.dispose()
    print(f"promotion {promotion_id}: {args.version} candidate → paper, manual pass")
    log.warning(
        "gate_manual_promotion",
        extra={"fields": {"promotion_id": str(promotion_id), "version": str(args.version), "note": args.note}},
    )
    return EXIT_OK


def _versions(engine_url: str, strategy: str | None) -> int:
    engine = create_engine(engine_url, pool_size=1)
    try:
        with Session(engine) as session:
            lines = versions(session, strategy)
    finally:
        engine.dispose()
    print(f"{'version id':36}  {'strategy':20}  {'commit':12}  {'frozen hash':12}  stage")
    for line in lines:
        frozen = "-" if line.frozen_hash is None else line.frozen_hash[:12]
        print(f"{line.version_id!s:36}  {line.strategy:20}  {line.git_commit[:12]:12}  {frozen:12}  {line.stage}")
    return EXIT_OK


def _check_version(settings: GateSettings, engine_url: str, text: str) -> int:
    engine = create_engine(engine_url, pool_size=1)
    try:
        with Session(engine) as session:
            checked = check_version(session, text, settings.trading_env)
            session.commit()
    except CheckVersionRefused as exc:
        print(f"gate check-version: {exc}", file=sys.stderr)
        log.error("gate_check_version_refused", extra={"fields": {"why": str(exc)}})
        return EXIT_REFUSED
    finally:
        engine.dispose()
    print(json.dumps(checked.to_json(), sort_keys=True))
    log.info("gate_check_version", extra={"fields": checked.to_json()})
    return EXIT_OK if checked.passed else EXIT_NOT_PROMOTED


def main(argv: list[str] | None = None) -> NoReturn:
    """Entry point of the ``gate`` console script."""
    args = parser().parse_args(argv)
    configure_logging(process=PROCESS, env=os.environ.get("TRADING_ENV", ""))
    started = boot("worker", settings_cls=GateSettings, secrets=(POSTGRES_PASSWORD,))
    settings = started.settings
    url = settings.database_url(started.secrets[POSTGRES_PASSWORD])
    if args.command == "import":
        sys.exit(_import(settings, url, args.dir))
    if args.command == "evaluate":
        sys.exit(_evaluate(settings, url, args))
    if args.command == "promote":
        sys.exit(_promote(url, args))
    if args.command == "versions":
        sys.exit(_versions(url, args.strategy))
    if args.command == "check-version":
        sys.exit(_check_version(settings, url, sys.stdin.read()))
    sys.exit(EXIT_USAGE)  # unreachable: the subparser is required
