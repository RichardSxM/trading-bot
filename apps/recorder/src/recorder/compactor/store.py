"""Catalog paths, atomic writes, and ``done.json``, the compactor's single commit point (spec 0007).

An hour counts as done only once ``done.json`` lists it. Parquet or book files of an hour that
is not listed are leftovers of a crash, and the next pass overwrites them.
"""

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from nautilus_trader.model.data import OrderBookDelta, TradeTick
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.catalog.parquet import _timestamps_to_filename
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

from trading_core.marketdata.hours import hour_last_ns

# Catalog folders, `class_to_filename(cls)`. For deltas and trades the recorder's stream folder
# under `live/<run>/` has the same name; instruments are `live/<run>/crypto_perpetual_<ns>.feather`.
DELTAS_TABLE: Final[str] = class_to_filename(OrderBookDelta)
TRADES_TABLE: Final[str] = class_to_filename(TradeTick)
INSTRUMENTS_TABLE: Final[str] = class_to_filename(CryptoPerpetual)
STREAM_TABLES: Final = (DELTAS_TABLE, TRADES_TABLE)
TMP_SUFFIX: Final = ".tmp"
COMPRESSION: Final = "zstd"
# Book files kept per instrument: the newest is the carry, the one before it a spare.
BOOKS_KEPT: Final = 2


def stream_key(table: str, instrument_id: str) -> str:
    """The key of one stream in ``done.json``, such as ``order_book_deltas/PF_XBTUSD.KRAKEN``."""
    return f"{table}/{instrument_id}"


@dataclass
class DoneRecord:
    """The commit record.

    - ``hours``: per stream key, the finished hours with their rows and bytes.
    - ``carry``: per instrument, the last hour whose book file is valid, or ``None``.
    - ``files``: converted feather files (relative to the catalog root), deleted at the pass end.
    - ``overlaps``: per stream key, hours left unconverted because their rows overlapped (AC-12).
    - ``kept``: feather files that hold an overlapped hour; never deleted.
    - ``instruments``: per instrument, the newest definition used for an hour (``to_dict`` form).
    - ``started_runs``: dead runs whose conversion began, so the space check (AC-9) runs once.
    - ``streams``: per instrument, the gap detector's state (``GapState.to_dict``), so detection
      resumes across passes (AC-4).
    - ``empty``: per instrument, hours without rows that lie wholly inside a ``recorder_down`` gap;
      they count towards closing a day (AC-5).
    - ``days``: the closed days (``YYYY-MM-DD``), each with its manifest written.
    """

    hours: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    carry: dict[str, int | None] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    overlaps: dict[str, set[int]] = field(default_factory=dict)
    kept: set[str] = field(default_factory=set)
    instruments: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_runs: set[str] = field(default_factory=set)
    streams: dict[str, dict[str, object]] = field(default_factory=dict)
    empty: dict[str, set[int]] = field(default_factory=dict)
    days: set[str] = field(default_factory=set)

    def is_done(self, key: str, hour: int) -> bool:
        """Whether a stream's hour is finished."""
        return hour in self.hours.get(key, {})

    def to_json(self) -> str:
        """The record as JSON with sorted keys and lists, so equal records give equal text."""
        return json.dumps(
            {
                "hours": {k: {str(h): v for h, v in sorted(hours.items())} for k, hours in sorted(self.hours.items())},
                "carry": dict(sorted(self.carry.items())),
                "files": sorted(self.files),
                "overlaps": {k: sorted(v) for k, v in sorted(self.overlaps.items())},
                "kept": sorted(self.kept),
                "instruments": dict(sorted(self.instruments.items())),
                "started_runs": sorted(self.started_runs),
                "streams": dict(sorted(self.streams.items())),
                "empty": {k: sorted(v) for k, v in sorted(self.empty.items())},
                "days": sorted(self.days),
            },
            indent=1,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> DoneRecord:
        """Parse a record written by ``to_json``."""
        raw = json.loads(text)
        return cls(
            hours={k: {int(h): v for h, v in hours.items()} for k, hours in raw.get("hours", {}).items()},
            carry=dict(raw.get("carry", {})),
            files=set(raw.get("files", [])),
            overlaps={k: set(v) for k, v in raw.get("overlaps", {}).items()},
            kept=set(raw.get("kept", [])),
            instruments=dict(raw.get("instruments", {})),
            started_runs=set(raw.get("started_runs", [])),
            streams=dict(raw.get("streams", {})),
            empty={k: set(v) for k, v in raw.get("empty", {}).items()},
            days=set(raw.get("days", [])),
        )


def atomic_write(path: Path, write: Callable[[Path], object]) -> None:
    """Write through ``<name>.tmp`` and a rename, so a reader never sees a half written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    write(tmp)
    os.replace(tmp, path)


class CatalogStore:
    """Where everything lives under the catalog root (spec 0007, *Catalog layout*)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.live = root / "live"
        self.state = root / ".compactor"
        self.done_path = self.state / "done.json"

    def relative(self, path: Path) -> str:
        """A path as ``done.json`` records it."""
        return str(path.relative_to(self.root))

    def data_file(self, table: str, instrument_id: str, hour: int) -> Path:
        """``data/<table>/<instrument>/<interval>.parquet``, the name ``ParquetDataCatalog`` reads."""
        name: str = _timestamps_to_filename(hour, hour_last_ns(hour))
        return self.root / "data" / table / urisafe_identifier(instrument_id) / name

    def book_file(self, instrument_id: str, hour: int) -> Path:
        """The carried book at the end of ``hour``, stored as the snapshot rows for the next hour."""
        return self.state / "books" / urisafe_identifier(instrument_id) / f"{hour}.parquet"

    def gap_file(self, day: str) -> Path:
        """``gaps/<YYYY-MM-DD>.parquet``, the gaps that start on that UTC day."""
        return self.root / "gaps" / f"{day}.parquet"

    def manifest_file(self, day: str) -> Path:
        """``manifests/<YYYY-MM-DD>.json``, the closed day's size and gap totals."""
        return self.root / "manifests" / f"{day}.json"

    def prune_books(self, instrument_id: str) -> None:
        """Keep only the newest ``BOOKS_KEPT`` book files of an instrument."""
        folder = self.state / "books" / urisafe_identifier(instrument_id)
        files = sorted(folder.glob("*.parquet"), key=lambda p: int(p.stem))
        for old in files[:-BOOKS_KEPT]:
            old.unlink(missing_ok=True)

    def remove_tmp_files(self) -> None:
        """Delete ``.tmp`` files a killed process left behind. Call only while nothing is writing."""
        for folder in (self.root / "data", self.root / "gaps", self.root / "manifests", self.state):
            for tmp in folder.rglob(f"*{TMP_SUFFIX}") if folder.exists() else ():
                tmp.unlink(missing_ok=True)

    def free_bytes(self) -> int:
        """Free space on the catalog's file system, as a non root process can use it."""
        stat = os.statvfs(self.root)
        return stat.f_bavail * stat.f_frsize

    def remove_run_if_empty(self, run: str) -> None:
        """Delete a finished run folder once no feather file is left in it."""
        folder = self.live / run
        if folder.is_dir() and not any(folder.rglob("*.feather")):
            shutil.rmtree(folder)

    def load_done(self) -> DoneRecord:
        """The commit record, or an empty one before the first commit."""
        try:
            return DoneRecord.from_json(self.done_path.read_text())
        except FileNotFoundError:
            return DoneRecord()

    def save_done(self, done: DoneRecord) -> None:
        """Rewrite the commit record atomically. This is the commit."""
        text = done.to_json()
        atomic_write(self.done_path, lambda tmp: tmp.write_text(text))
