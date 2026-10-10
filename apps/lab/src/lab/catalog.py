"""Reading one source tree of the lab catalog (spec 0008, *Catalog on the laptop*).

The layout is spec 0007's: ``data/<table>/<instrument>/<hour interval>.parquet``,
``gaps/<day>.parquet``, and ``manifests/<day>.json`` (the day's commit point), with the paths and
gap rows from ``trading_core.catalog``, shared with the recorder's compactor.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.enums import BookAction
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from trading_core.catalog.days import read_gaps
from trading_core.catalog.layout import (
    DELTAS_TABLE,
    FUNDING_TABLE,
    INSTRUMENTS_TABLE,
    TRADES_TABLE,
    CatalogLayout,
)
from trading_core.lab.funding import FundingRate
from trading_core.lab.segments import HourFiles, hours_of
from trading_core.marketdata.gaps import Gap

__all__ = [
    "DELTAS_TABLE",
    "FUNDING_TABLE",
    "INSTRUMENTS_TABLE",
    "REPLAY_TABLES",
    "TRADES_TABLE",
    "ManifestError",
    "SourceTree",
    "last_ts_init",
]

# The streams whose incomplete hours a replay must skip (AC-4).
REPLAY_TABLES: Final = (DELTAS_TABLE, TRADES_TABLE)


class ManifestError(ValueError):
    """A manifest exists but does not hold what spec 0007 says it holds."""


class SourceTree:
    """One source tree (``<LAB_CATALOG_DIR>/<source>``) for one instrument."""

    def __init__(self, root: Path, instrument_id: str) -> None:
        self.root = root
        self.instrument_id = instrument_id
        self.layout = CatalogLayout(root)

    def manifest_file(self, day: date) -> Path:
        """The day's manifest, written last as its commit point."""
        return self.layout.manifest_file(day.isoformat())

    def gap_file(self, day: date) -> Path:
        """The gaps that start on ``day``."""
        return self.layout.gap_file(day.isoformat())

    def hour_file(self, table: str, hour_ns: int) -> Path:
        """One hour of one stream, named like the compactor names it."""
        return self.layout.data_file(table, self.instrument_id, hour_ns)

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
            found.extend(g for g in read_gaps(self.layout, day.isoformat()) if g.instrument_id == self.instrument_id)
        return sorted(found)

    def funding_rates(self, days: list[date]) -> list[FundingRate]:
        """The ``funding_rate_update`` rows of the given days and the day before the first, by ``ts_init`` (AC-9)."""
        if not days:
            return []
        rates: list[FundingRate] = []
        for hour in hours_of([min(days) - timedelta(days=1), *days]):
            path = self.hour_file(FUNDING_TABLE, hour)
            if path.is_file():
                for update in ArrowSerializer.deserialize(FundingRateUpdate, pq.read_table(path)):
                    # The rate comes back from Parquet as text; money math needs a Decimal.
                    rates.append(FundingRate(update.ts_init, Decimal(str(update.rate))))
        return sorted(rates, key=lambda r: r.ts_ns)

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
