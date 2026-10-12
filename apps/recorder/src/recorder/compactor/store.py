"""Catalog paths, atomic writes, and ``done.json``, the compactor's single commit point (spec 0007).

An hour counts as done only once ``done.json`` lists it. Parquet or book files of an hour that
is not listed are leftovers of a crash, and the next pass overwrites them.
"""

import json
import shutil
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from nautilus_trader.persistence.funcs import urisafe_identifier

from recorder.disk import free_bytes
from recorder.sequence_holes import HOLES_FILE
from trading_core.catalog.layout import (
    DELTAS_TABLE,
    FUNDING_TABLE,
    INDEX_TABLE,
    INSTRUMENTS_TABLE,
    LIQUIDATION_TABLE,
    MARK_TABLE,
    OPEN_INTEREST_TABLE,
    TRADES_TABLE,
    CatalogLayout,
    hour_filename,
    parse_hour_filename,
)
from trading_core.catalog.sink import COMPRESSION, TMP_SUFFIX, atomic_write
from trading_core.marketdata.context_feed import OUTAGES_FILE
from trading_core.marketdata.errors import BackupStateInvalid
from trading_core.marketdata.retention import HourFiles

__all__ = [
    "BOOKS_KEPT",
    "COMPRESSION",
    "CONTEXT_TABLES",
    "DELTAS_TABLE",
    "FUNDING_TABLE",
    "INDEX_TABLE",
    "INSTRUMENTS_TABLE",
    "LIQUIDATION_TABLE",
    "MARK_TABLE",
    "OPEN_INTEREST_TABLE",
    "RAW_FEED_TABLES",
    "SILENCE_TABLES",
    "STREAM_TABLES",
    "TMP_SUFFIX",
    "TRADES_TABLE",
    "CatalogStore",
    "DoneRecord",
    "atomic_write",
    "merge_quarantine",
    "read_backup_state",
    "stream_key",
]

# The market context streams (spec 0009): they convert like trades, and get the book's
# `recorder_down` gaps as their own (AC-7).
CONTEXT_TABLES: Final = (MARK_TABLE, INDEX_TABLE, FUNDING_TABLE, OPEN_INTEREST_TABLE, LIQUIDATION_TABLE)
# The context streams that push steadily, so a long pause between two rows is a `silence` gap
# (AC-7); funding and liquidations have quiet spells by nature.
SILENCE_TABLES: Final = (MARK_TABLE, INDEX_TABLE, OPEN_INTEREST_TABLE)
# The raw feed's streams: its outages are their `reconnect` gaps (AC-7).
RAW_FEED_TABLES: Final = (OPEN_INTEREST_TABLE, LIQUIDATION_TABLE)
# Each stream's folder under `live/<run>/` has the catalog table's name; instruments are
# `live/<run>/crypto_perpetual_<ns>.feather`.
STREAM_TABLES: Final = (DELTAS_TABLE, TRADES_TABLE, *CONTEXT_TABLES)
# Book files kept per instrument: the newest is the carry, the one before it a spare.
BOOKS_KEPT: Final = 2


def _complete_lines(path: Path) -> tuple[list[str], int] | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    end = data.rfind(b"\n") + 1
    return data[:end].decode(errors="replace").splitlines(), end


def stream_key(table: str, instrument_id: str) -> str:
    """The key of one stream in ``done.json``, such as ``order_book_deltas/PF_XBTUSD.KRAKEN``."""
    return f"{table}/{instrument_id}"


@dataclass
class DoneRecord:
    """The commit record.

    - ``hours``: per stream key, the finished hours with their rows and bytes.
    - ``carry``: per instrument, the last hour whose book file is valid, or ``None``.
    - ``files``: converted feather files (relative to the catalog root), deleted at the pass end.
    - ``incomplete``: per stream key, the hours that miss late rows (AC-12), with the late row count
      (the rows in the hour's quarantine file), or 0 for an hour whose snapshot missed a late delta.
    - ``watermarks``: per feather file not yet finished, the stream's reorder window state when
      reading of that file began; per stream key, the state at the end of its last run (AC-12).
    - ``instruments``: per instrument, the newest definition used for an hour (``to_dict`` form).
    - ``started_runs``: dead runs whose conversion began, so the space check (AC-9) runs once.
    - ``streams``: per instrument, the gap detector's state (``GapState.to_dict``), so detection
      resumes across passes (AC-4).
    - ``context_streams``: per context stream key, the silence detector's state
      (``SilenceState.to_dict``, spec 0009 AC-7).
    - ``outages``: per run, how many lines of its ``context_outages.jsonl`` became gaps (AC-7).
    - ``liquidation_uids``: per liquidation stream key, the last finished hour and its trade ids, so a
      duplicate in the next hour is dropped the same way on a rerun (AC-5).
    - ``empty``: per instrument, hours without rows that lie wholly inside a ``recorder_down`` gap;
      they count towards closing a day (AC-5).
    - ``days``: the closed days (``YYYY-MM-DD``), each with its manifest written.
    - ``holes``: per run, its holes file as last read: the bytes read, the last ``checked_to_ns``,
      and whether it was ``closed``; or ``unchecked`` for a run without one (AC-16).
    """

    hours: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    carry: dict[str, int | None] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    incomplete: dict[str, dict[int, int]] = field(default_factory=dict)
    watermarks: dict[str, dict[str, object]] = field(default_factory=dict)
    instruments: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_runs: set[str] = field(default_factory=set)
    streams: dict[str, dict[str, object]] = field(default_factory=dict)
    context_streams: dict[str, dict[str, object]] = field(default_factory=dict)
    outages: dict[str, int] = field(default_factory=dict)
    liquidation_uids: dict[str, dict[str, Any]] = field(default_factory=dict)
    empty: dict[str, set[int]] = field(default_factory=dict)
    days: set[str] = field(default_factory=set)
    holes: dict[str, dict[str, object]] = field(default_factory=dict)

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
                "incomplete": {
                    k: {str(h): n for h, n in sorted(hours.items())} for k, hours in sorted(self.incomplete.items())
                },
                "watermarks": dict(sorted(self.watermarks.items())),
                "instruments": dict(sorted(self.instruments.items())),
                "started_runs": sorted(self.started_runs),
                "streams": dict(sorted(self.streams.items())),
                "context_streams": dict(sorted(self.context_streams.items())),
                "outages": dict(sorted(self.outages.items())),
                "liquidation_uids": dict(sorted(self.liquidation_uids.items())),
                "empty": {k: sorted(v) for k, v in sorted(self.empty.items())},
                "days": sorted(self.days),
                "holes": dict(sorted(self.holes.items())),
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
            incomplete={k: {int(h): n for h, n in hours.items()} for k, hours in raw.get("incomplete", {}).items()},
            watermarks=dict(raw.get("watermarks", {})),
            instruments=dict(raw.get("instruments", {})),
            started_runs=set(raw.get("started_runs", [])),
            streams=dict(raw.get("streams", {})),
            context_streams=dict(raw.get("context_streams", {})),
            outages=dict(raw.get("outages", {})),
            liquidation_uids=dict(raw.get("liquidation_uids", {})),
            empty={k: set(v) for k, v in raw.get("empty", {}).items()},
            days=set(raw.get("days", [])),
            holes=dict(raw.get("holes", {})),
        )


def merge_quarantine(path: Path, rows: Any) -> int:
    """Merge late rows into an hour's quarantine file (spec 0007 AC-12). Returns its row count.

    Read, union, keep the first of identical rows, stable sort by ``ts_init``, write atomically.
    Merging the same rows again changes nothing, so a rerun gives the same file.
    """
    meta = rows.schema.metadata
    tables = [rows.replace_schema_metadata(meta)]
    if path.exists():
        tables.insert(0, pq.read_table(str(path)).replace_schema_metadata(meta))
    merged = pa.concat_tables(tables)
    seen: set[tuple[object, ...]] = set()
    keep: list[int] = []
    for i, row in enumerate(zip(*(column.to_pylist() for column in merged.columns), strict=True)):
        if row not in seen:
            seen.add(row)
            keep.append(i)
    unique = merged.take(keep)
    unique = unique.take(pc.sort_indices(unique, sort_keys=[("ts_init", "ascending")]))
    atomic_write(path, partial(pq.write_table, unique, compression=COMPRESSION))
    return int(unique.num_rows)


class CatalogStore(CatalogLayout):
    """Where everything lives under the catalog root (spec 0007, *Catalog layout*)."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.live = root / "live"
        self.state = root / ".compactor"
        self.done_path = self.state / "done.json"

    def relative(self, path: Path) -> str:
        """A path as ``done.json`` records it."""
        return str(path.relative_to(self.root))

    def quarantine_file(self, table: str, instrument_id: str, hour: int) -> Path:
        """``quarantine/<table>/<instrument>/<interval>.parquet``, the late rows of that hour (AC-12)."""
        return self.root / "quarantine" / self.data_file(table, instrument_id, hour).relative_to(self.root / "data")

    def book_file(self, instrument_id: str, hour: int) -> Path:
        """The carried book at the end of ``hour``, stored as the snapshot rows for the next hour."""
        return self.state / "books" / urisafe_identifier(instrument_id) / f"{hour}.parquet"

    def prune_books(self, instrument_id: str) -> None:
        """Keep only the newest ``BOOKS_KEPT`` book files of an instrument."""
        folder = self.state / "books" / urisafe_identifier(instrument_id)
        files = sorted(folder.glob("*.parquet"), key=lambda p: int(p.stem))
        for old in files[:-BOOKS_KEPT]:
            old.unlink(missing_ok=True)

    def remove_tmp_files(self) -> None:
        """Delete ``.tmp`` files a killed process left behind. Call only while nothing is writing."""
        folders = ("data", "quarantine", "gaps", "manifests")
        for folder in (*(self.root / name for name in folders), self.state):
            for tmp in folder.rglob(f"*{TMP_SUFFIX}") if folder.exists() else ():
                tmp.unlink(missing_ok=True)

    def free_bytes(self) -> int:
        """Free space on the catalog's file system, as a non root process can use it."""
        return free_bytes(self.root)

    def holes_file(self, run: str) -> Path:
        """The recorder's ``sequence_holes.jsonl`` of a run (spec 0007 AC-15)."""
        return self.live / run / HOLES_FILE

    def read_holes_lines(self, run: str) -> tuple[list[str], int] | None:
        """The complete lines of a run's holes file and their bytes, or ``None`` when it has none.

        A last line without its newline is still being written (or was torn by a crash), so it is
        left out.
        """
        return _complete_lines(self.holes_file(run))

    def outages_file(self, run: str) -> Path:
        """The recorder's ``context_outages.jsonl`` of a run (spec 0009 AC-6)."""
        return self.live / run / OUTAGES_FILE

    def read_outage_lines(self, run: str) -> list[str] | None:
        """The complete lines of a run's outages file, or ``None`` when it has none (as for holes)."""
        read = _complete_lines(self.outages_file(run))
        return None if read is None else read[0]

    def remove_run_if_empty(self, run: str) -> None:
        """Delete a finished run folder once no feather file is left in it."""
        folder = self.live / run
        if folder.is_dir() and not any(folder.rglob("*.feather")):
            shutil.rmtree(folder)

    def hour_inventory(self) -> tuple[list[HourFiles], int]:
        """Every catalog hour on disk (``data/`` and its ``quarantine/`` files), and the bytes under
        ``data/`` (spec 0007 AC-6). Files whose name is not one whole hour are not ours to retire."""
        found: dict[int, list[int]] = {}  # hour -> [data bytes, total bytes, newest mtime]
        catalog_bytes = 0
        for folder in ("data", "quarantine"):
            for path in (self.root / folder).glob("*/*/*.parquet"):
                hour = parse_hour_filename(path.name)
                if hour is None:
                    continue
                stat = path.stat()
                entry = found.setdefault(hour, [0, 0, 0])
                if folder == "data":
                    entry[0] += stat.st_size
                    catalog_bytes += stat.st_size
                entry[1] += stat.st_size
                entry[2] = max(entry[2], stat.st_mtime_ns)
        hours = [HourFiles(hour, data, total, mtime) for hour, (data, total, mtime) in sorted(found.items())]
        return hours, catalog_bytes

    def delete_hour(self, hour: int) -> None:
        """Delete one whole catalog hour: every table and instrument, with its quarantine files."""
        name = hour_filename(hour)
        for folder in ("data", "quarantine"):
            for path in (self.root / folder).glob(f"*/*/{name}"):
                path.unlink(missing_ok=True)

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


def read_backup_state(path: Path) -> object | None:
    """The parsed backup state file (spec 0007 AC-8), or ``None`` when there is none yet.

    Raises ``BackupStateInvalid`` when it cannot be read or is not JSON; ``parse_backup_state``
    checks the fields.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupStateInvalid(f"{path} is unreadable: {exc.strerror}") from exc
    try:
        raw: object = json.loads(text)
    except ValueError as exc:
        raise BackupStateInvalid(f"{path} is not JSON") from exc
    return raw
