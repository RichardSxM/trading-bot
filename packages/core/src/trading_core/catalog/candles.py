"""The candles tree of the lab catalog (spec 0014, *Data model sketch*): one file per UTC month.

``data/bar/<instrument>-1-MINUTE-LAST-EXTERNAL/<YYYY-MM>.parquet`` holds Nautilus ``Bar`` rows plus a
``quiet`` column, ``data/funding_rate_update/<instrument>/<YYYY-MM>.parquet`` the ``FundingRateUpdate``
rows by charge time plus a ``source`` column, ``gaps/<YYYY-MM>.parquet`` the gap rows, and
``manifests/<YYYY-MM>.json`` the month's commit point, written last. Every write is atomic.

BTC, the first coin, keeps those gap and manifest paths; every other coin puts them one folder
down, ``gaps/<instrument>/`` and ``manifests/<instrument>/`` (spec 0022 AC-2), so no BTC file moves.
"""

import json
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
from nautilus_trader.model.data import Bar, BarType, FundingRateUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.funcs import urisafe_identifier
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from trading_core.catalog.context import read_funding_updates
from trading_core.catalog.layout import FUNDING_TABLE
from trading_core.catalog.sink import COMPRESSION, atomic_write
from trading_core.lab.candles import CandleGap, CandleRow, FundingRow, FundingSource, Month, MonthManifest
from trading_core.lab.errors import OutdatedManifestError

BAR_TABLE: Final = "bar"
# The coin whose gaps and manifests sit at the tree's root, as before spec 0022.
ROOT_INSTRUMENT_ID: Final = "PF_XBTUSD.KRAKEN"
GAP_SCHEMA: Final = pa.schema(
    [
        pa.field("start_ns", pa.uint64(), nullable=False),
        pa.field("end_ns", pa.uint64(), nullable=False),
        pa.field("minutes", pa.uint32(), nullable=False),
    ]
)


def minute_bar_type(instrument_id: str) -> BarType:
    """The stored bar type, ``<instrument>-1-MINUTE-LAST-EXTERNAL``."""
    return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")


class CandleTree:
    """The paths and month files of ``<LAB_CATALOG_DIR>/candles`` for one instrument."""

    def __init__(self, root: Path, instrument: CryptoPerpetual) -> None:
        self.root = root
        self.instrument = instrument
        self.instrument_id = instrument.id.value
        self.bar_type = minute_bar_type(self.instrument_id)

    def bar_file(self, month: Month) -> Path:
        """The month's 1 minute candles."""
        return self.root / "data" / BAR_TABLE / urisafe_identifier(str(self.bar_type)) / f"{month}.parquet"

    def funding_file(self, month: Month) -> Path:
        """The funding rows charged in the month."""
        return self.root / "data" / FUNDING_TABLE / urisafe_identifier(self.instrument_id) / f"{month}.parquet"

    def _own(self, folder: str) -> Path:
        base = self.root / folder
        return base if self.instrument_id == ROOT_INSTRUMENT_ID else base / urisafe_identifier(self.instrument_id)

    def gap_file(self, month: Month) -> Path:
        """The month's gap rows."""
        return self._own("gaps") / f"{month}.parquet"

    def manifest_file(self, month: Month) -> Path:
        """The month's manifest, its commit point."""
        return self._own("manifests") / f"{month}.json"

    # Candles ---------------------------------------------------------------------------------

    def write_bars(self, month: Month, rows: list[CandleRow]) -> None:
        """Write the month's candles as ``Bar`` rows at the instrument's precisions, plus ``quiet``."""
        price, size = self.instrument.price_precision, self.instrument.size_precision
        bars = [
            Bar(
                self.bar_type,
                Price(r.open, price),
                Price(r.high, price),
                Price(r.low, price),
                Price(r.close, price),
                Quantity(r.volume, size),
                r.ts_ns,
                r.ts_ns,
            )
            for r in rows
        ]
        if bars:
            table = ArrowSerializer.serialize_batch(bars, data_cls=Bar)
        else:
            table = pa.table({"ts_init": pa.array([], pa.uint64())})
        table = table.append_column("quiet", pa.array([r.quiet for r in rows], pa.bool_()))
        atomic_write(self.bar_file(month), partial(pq.write_table, table, compression=COMPRESSION))

    def read_bars(self, month: Month) -> list[CandleRow]:
        """The month's stored candles, in ``ts_init`` order. Empty when the file holds none."""
        table = pq.read_table(self.bar_file(month))
        if table.num_rows == 0:
            return []
        quiet = table.column("quiet").to_pylist()
        bars = ArrowSerializer.deserialize(Bar, table.drop_columns(["quiet"]))
        return [
            CandleRow(
                int(bar.ts_init),
                Decimal(str(bar.open)),
                Decimal(str(bar.high)),
                Decimal(str(bar.low)),
                Decimal(str(bar.close)),
                Decimal(str(bar.volume)),
                bool(q),
            )
            for bar, q in zip(bars, quiet, strict=True)
        ]

    # Funding ---------------------------------------------------------------------------------

    def write_funding(self, month: Month, rows: list[FundingRow]) -> None:
        """Write the month's funding rows (all of them, merged) as ``FundingRateUpdate`` plus ``source``."""
        instrument_id = InstrumentId.from_str(self.instrument_id)
        # The funding serializer takes one update at a time.
        batches = [
            ArrowSerializer.serialize(
                FundingRateUpdate(instrument_id, r.rate, r.charge_ns, r.charge_ns, next_funding_ns=r.next_funding_ns)
            )
            for r in rows
        ]
        table = pa.Table.from_batches(batches)
        table = table.append_column("source", pa.array([r.source for r in rows], pa.string()))
        atomic_write(self.funding_file(month), partial(pq.write_table, table, compression=COMPRESSION))

    def read_funding(self, month: Month) -> list[FundingRow]:
        """The funding rows charged in the month, by charge time; empty when there is no file."""
        path = self.funding_file(month)
        if not path.is_file():
            return []
        table = pq.read_table(path)
        sources: list[FundingSource] = table.column("source").to_pylist()
        updates = read_funding_updates(table.drop_columns(["source"]))
        rows = [
            FundingRow(u.ts_init, Decimal(str(u.rate)), int(u.next_funding_ns) - u.ts_init, source)
            for u, source in zip(updates, sources, strict=True)
        ]
        return sorted(rows, key=lambda r: r.charge_ns)

    def funding_months(self) -> list[Month]:
        """Every month with a stored funding file, oldest first."""
        folder = self.funding_file(Month(2000, 1)).parent
        return sorted(Month.parse(p.stem) for p in folder.glob("*.parquet")) if folder.is_dir() else []

    # Gaps and manifests ----------------------------------------------------------------------

    def write_gaps(self, month: Month, gaps: list[CandleGap]) -> None:
        """Write the month's gap rows (an empty file when it has none)."""
        table = pa.Table.from_pylist([asdict(g) for g in gaps], schema=GAP_SCHEMA)
        atomic_write(self.gap_file(month), partial(pq.write_table, table, compression=COMPRESSION))

    def read_gaps(self, month: Month) -> list[CandleGap]:
        """The month's gap rows; empty when there is no file."""
        path = self.gap_file(month)
        if not path.is_file():
            return []
        return [CandleGap(**row) for row in pq.read_table(path, schema=GAP_SCHEMA).to_pylist()]

    def write_manifest(self, manifest: MonthManifest) -> None:
        """Write the month's manifest, the last write of a month."""
        text = json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n"
        atomic_write(self.manifest_file(Month.parse(manifest.month)), lambda tmp: tmp.write_text(text, "utf-8"))

    def read_manifest(self, month: Month) -> MonthManifest | None:
        """The month's manifest, or ``None`` when the month is not committed.

        A manifest written before a required field existed counts as no manifest everywhere (spec
        0014 AC-4), so the month is fetched again. Any other malformed manifest raises
        ``CandleInputError``.
        """
        path = self.manifest_file(month)
        if not path.is_file():
            return None
        try:
            return MonthManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
        except OutdatedManifestError:
            return None
