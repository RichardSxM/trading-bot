"""``gate import`` (spec 0015 AC-7): lab bundle files into the VPS Postgres, one transaction per file.

A file is refused whole unless every run in it is a demo ``backtest`` or ``replay`` run by
``BACKTESTER-LAB`` whose version carries both frozen hashes, so a lab file can never create a paper or
live row. A refused file is reported and the others still load. Importing the same files again adds
nothing; a row stored with other content refuses that file (``RecordConflictError``).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.bundle_codec import BundleFormatError, parse_bundle
from trading_core.recording.enums import SIM_MODES, Env
from trading_core.recording.errors import RecordingError
from trading_core.recording.rows import RunBundle

log = logging.getLogger(__name__)

LAB_TRADER: Final = "BACKTESTER-LAB"
SUFFIX: Final = ".bundle.json"


class BundleRefused(Exception):
    """A bundle the gate will not import, with why."""


@dataclass
class ImportSummary:
    """What an import did: files whose run was new, already there, or refused (with why)."""

    loaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)


def check_bundle(bundle: RunBundle) -> None:
    """Raises ``BundleRefused`` unless the bundle is a demo lab run with a frozen version (AC-7)."""
    run = bundle.run
    if run.env != Env.DEMO:
        raise BundleRefused(f"env {run.env}, not demo")
    if run.mode not in SIM_MODES:
        raise BundleRefused(f"mode {run.mode}, not backtest or replay")
    if run.trader_id != LAB_TRADER:
        raise BundleRefused(f"trader {run.trader_id}, not {LAB_TRADER}")
    version = bundle.version
    if version is None or version.code_hash is None or version.frozen_hash is None:
        raise BundleRefused("its version carries no code_hash and frozen_hash")


def bundle_files(folder: Path) -> list[Path]:
    """Every ``*.bundle.json`` under ``folder``, in path order (batch folders included)."""
    return sorted(p for p in folder.rglob(f"*{SUFFIX}") if p.is_file())


def import_folder(sessions: Callable[[], Session], folder: Path) -> ImportSummary:
    """Import every bundle file under ``folder``; never raises for one bad file."""
    summary = ImportSummary()
    for path in bundle_files(folder):
        name = str(path.relative_to(folder))
        try:
            bundle = parse_bundle(path.read_text(encoding="utf-8"))
            check_bundle(bundle)
            with sessions() as session:
                try:
                    new = repository.import_run_bundle(session, bundle)
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
        except (OSError, BundleFormatError, BundleRefused, RecordingError, SQLAlchemyError) as exc:
            summary.refused[name] = f"{type(exc).__name__}: {exc}"
            log.error("gate_bundle_refused", extra={"fields": {"file": name, "error": summary.refused[name]}})
            continue
        (summary.loaded if new else summary.skipped).append(name)
    log.info(
        "gate_import_done",
        extra={
            "fields": {"loaded": len(summary.loaded), "skipped": len(summary.skipped), "refused": len(summary.refused)}
        },
    )
    return summary
