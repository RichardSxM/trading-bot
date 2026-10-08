"""Reading one source tree of the lab catalog (spec 0008, *Catalog on the laptop*).

The layout is spec 0007's: ``data/<table>/<instrument>/<hour interval>.parquet``,
``gaps/<day>.parquet``, and ``manifests/<day>.json`` (the day's commit point). Milestone 3 moves
the shared readers into ``trading_core.catalog``; until then the lab keeps its own.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from nautilus_trader.model.data import OrderBookDelta, TradeTick
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.catalog.parquet import _timestamps_to_filename
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

from trading_core.lab.segments import HourFiles
from trading_core.marketdata.gaps import Gap, gap_kind
from trading_core.marketdata.hours import hour_last_ns

DELTAS_TABLE: Final = class_to_filename(OrderBookDelta)
TRADES_TABLE: Final = class_to_filename(TradeTick)
INSTRUMENTS_TABLE: Final = class_to_filename(CryptoPerpetual)
# The streams whose incomplete hours a replay must skip (AC-4).
REPLAY_TABLES: Final = (DELTAS_TABLE, TRADES_TABLE)


class ManifestError(ValueError):
    """A manifest exists but does not hold what spec 0007 says it holds."""


class SourceTree:
    """One source tree (``<LAB_CATALOG_DIR>/<source>``) for one instrument."""

    def __init__(self, root: Path, instrument_id: str) -> None:
        self.root = root
        self.instrument_id = instrument_id

    def manifest_file(self, day: date) -> Path:
        """The day's manifest, written last as its commit point."""
        return self.root / "manifests" / f"{day.isoformat()}.json"

    def gap_file(self, day: date) -> Path:
        """The gaps that start on ``day``."""
        return self.root / "gaps" / f"{day.isoformat()}.parquet"

    def hour_file(self, table: str, hour_ns: int) -> Path:
        """One hour of one stream, named like the compactor names it."""
        name = _timestamps_to_filename(hour_ns, hour_last_ns(hour_ns))
        return self.root / "data" / table / urisafe_identifier(self.instrument_id) / name

    def missing_manifests(self, days: list[date]) -> list[date]:
        """The required days that have no manifest yet (AC-3)."""
        return [day for day in days if not self.manifest_file(day).is_file()]

    def incomplete_hours(self, day: date) -> set[int]:
        """The ``incomplete_hours`` of the instrument's delta and trade streams in the day's manifest."""
        try:
            manifest = json.loads(self.manifest_file(day).read_text(encoding="utf-8"))
            streams = manifest["streams"]
            return {
                int(hour)
                for stream in streams
                if stream["instrument_id"] == self.instrument_id and stream["type"] in REPLAY_TABLES
                for hour in stream["incomplete_hours"]
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"{self.manifest_file(day)}: {exc!r}") from None

    def gaps(self, days: list[date]) -> list[Gap]:
        """The instrument's gap rows filed under the given days and the day before the first (AC-4)."""
        if not days:
            return []
        found: list[Gap] = []
        for day in [min(days) - timedelta(days=1), *sorted(days)]:
            path = self.gap_file(day)
            if not path.is_file():
                continue
            for row in pq.read_table(path).to_pylist():
                if row["instrument_id"] == self.instrument_id:
                    found.append(Gap(row["start_ns"], row["instrument_id"], gap_kind(row["kind"]), row["end_ns"]))
        return sorted(found)

    def hour_files(self, hour_ns: int) -> HourFiles:
        """Whether both stream files exist, and whether the deltas open with a ``CLEAR`` row."""
        deltas = self.hour_file(DELTAS_TABLE, hour_ns)
        trades = self.hour_file(TRADES_TABLE, hour_ns)
        has_deltas, has_trades = deltas.is_file(), trades.is_file()
        return HourFiles(
            has_deltas=has_deltas,
            has_trades=has_trades,
            starts_with_clear=has_deltas and has_trades and _first_action(deltas) == BookAction.CLEAR,
        )


def last_ts_init(path: Path) -> int:
    """The newest ``ts_init`` in an hour file (rows are in ``ts_init`` order, so the last row group holds it)."""
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows == 0:
        raise ValueError(f"{path} has no rows")
    column = parquet.read_row_group(parquet.num_row_groups - 1, columns=["ts_init"]).column("ts_init")
    newest: int = max(value.as_py() for value in column)
    return newest


def _first_action(path: Path) -> int | None:
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows == 0:
        return None
    first = parquet.read_row_group(0, columns=["action"]).column("action")[0].as_py()
    return None if first is None else int(first)
