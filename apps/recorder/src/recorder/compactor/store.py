"""Catalog paths, atomic writes, and ``done.json``, the compactor's single commit point (spec 0007).

An hour counts as done only once ``done.json`` lists it. Parquet or book files of an hour that
is not listed are leftovers of a crash, and the next pass overwrites them.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.persistence.catalog.parquet import _timestamps_to_filename
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

from trading_core.marketdata.hours import hour_last_ns

# The catalog folder Nautilus reads deltas from, `class_to_filename(OrderBookDelta)`.
DELTAS_TABLE: Final[str] = class_to_filename(OrderBookDelta)
# The recorder's stream folder for deltas (it writes OrderBookDeltas batches).
LIVE_DELTAS: Final = "order_book_deltas"
TMP_SUFFIX: Final = ".tmp"
# Book files kept per instrument: the newest is the carry, the one before it a spare.
BOOKS_KEPT: Final = 2


def stream_key(table: str, instrument_id: str) -> str:
    """The key of one stream in ``done.json``, such as ``order_book_deltas/PF_XBTUSD.KRAKEN``."""
    return f"{table}/{instrument_id}"


@dataclass
class DoneRecord:
    """The commit record.

    ``hours`` maps a stream key to its finished hours and their rows and bytes. ``carry`` maps an
    instrument to the last hour whose book file is valid, or ``None``. ``files`` lists the feather
    files (relative to the catalog root) whose rows are all in finished hours.
    """

    hours: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    carry: dict[str, int | None] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)

    def last_hour(self, key: str) -> int | None:
        """The newest finished hour of a stream, or ``None`` before its first one."""
        hours = self.hours.get(key)
        return max(hours) if hours else None

    def to_json(self) -> str:
        """The record as JSON, hour keys as strings and files sorted, so equal records give equal text."""
        return json.dumps(
            {
                "hours": {k: {str(h): v for h, v in sorted(hours.items())} for k, hours in sorted(self.hours.items())},
                "carry": dict(sorted(self.carry.items())),
                "files": sorted(self.files),
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> DoneRecord:
        """Parse a record written by ``to_json``."""
        raw = json.loads(text)
        return cls(
            hours={k: {int(h): v for h, v in hours.items()} for k, hours in raw.get("hours", {}).items()},
            carry=dict(raw.get("carry", {})),
            files=set(raw.get("files", [])),
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

    def data_file(self, table: str, instrument_id: str, hour: int) -> Path:
        """``data/<table>/<instrument>/<interval>.parquet``, the name ``ParquetDataCatalog`` reads."""
        name: str = _timestamps_to_filename(hour, hour_last_ns(hour))
        return self.root / "data" / table / urisafe_identifier(instrument_id) / name

    def book_file(self, instrument_id: str, hour: int) -> Path:
        """The carried book at the end of ``hour``, stored as the snapshot rows for the next hour."""
        return self.state / "books" / urisafe_identifier(instrument_id) / f"{hour}.parquet"

    def prune_books(self, instrument_id: str) -> None:
        """Keep only the newest ``BOOKS_KEPT`` book files of an instrument."""
        folder = self.state / "books" / urisafe_identifier(instrument_id)
        files = sorted(folder.glob("*.parquet"), key=lambda p: int(p.stem))
        for old in files[:-BOOKS_KEPT]:
            old.unlink(missing_ok=True)

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
