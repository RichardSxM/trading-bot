"""The recorder's feather files under ``live/``: runs, the current run, and closed files (spec 0007).

Standard library only, so the recorder's health check can use the same current run rule as the
compactor without importing pyarrow or Nautilus every 10 s.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# The recorder's stream folder names under `live/<run>/`. They equal the catalog's
# `class_to_filename` names, which `compactor/store.py` derives from Nautilus (a test checks both).
DELTAS_STREAM: Final = "order_book_deltas"
TRADES_STREAM: Final = "trade_tick"
INSTRUMENTS_STREAM: Final = "crypto_perpetual"


@dataclass(frozen=True, order=True)
class FeatherFile:
    """One recorder feather file. Files sort by the creation time in their name."""

    created_ns: int
    path: Path = field(compare=False)
    run: str = field(compare=False)

    @classmethod
    def parse(cls, path: Path, run: str) -> FeatherFile:
        """Read the creation time from ``<name>_<ns>.feather``."""
        return cls(created_ns=int(path.stem.rsplit("_", 1)[1]), path=path, run=run)


@dataclass
class Found:
    """The recorder's feather files: per (table, instrument) stream, instrument files, and per run."""

    streams: dict[tuple[str, str], list[FeatherFile]] = field(default_factory=dict)
    instruments: list[FeatherFile] = field(default_factory=list)
    runs: dict[str, list[FeatherFile]] = field(default_factory=dict)


def discover(
    live: Path,
    stream_tables: tuple[str, ...] = (DELTAS_STREAM, TRADES_STREAM),
    instruments_table: str = INSTRUMENTS_STREAM,
) -> Found:
    """Every feather file under ``live``, each list oldest first."""
    found = Found()
    if not live.exists():
        return found
    for run in sorted(p for p in live.iterdir() if p.is_dir()):
        for table in stream_tables:
            for folder in (run / table).glob("*"):
                for path in folder.glob("*.feather"):
                    file = FeatherFile.parse(path, run.name)
                    found.streams.setdefault((table, folder.name), []).append(file)
                    found.runs.setdefault(run.name, []).append(file)
        for path in run.glob(f"{instruments_table}_*.feather"):
            file = FeatherFile.parse(path, run.name)
            found.instruments.append(file)
            found.runs.setdefault(run.name, []).append(file)
    for files in [*found.streams.values(), found.instruments, *found.runs.values()]:
        files.sort()
    return found


def current_run(found: Found) -> str | None:
    """The run the recorder writes now: the one with the newest file creation time in its names.

    File names come from the recorder's clock and never change, unlike a folder's ctime, which a
    deletion inside a dead run would move forward.
    """
    newest = [(files[-1].created_ns, run) for run, files in found.runs.items() if files]
    return max(newest)[1] if newest else None


def is_closed(file: FeatherFile, siblings: list[FeatherFile], current: str | None) -> bool:
    """Closed when its run is no longer the current one, or its run has a newer file (it rotated)."""
    if file.run != current:
        return True
    return any(other.run == file.run and other.created_ns > file.created_ns for other in siblings)
