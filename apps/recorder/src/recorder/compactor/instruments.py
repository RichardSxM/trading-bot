"""One instrument definition row per instrument and hour (spec 0007, *Catalog layout*).

The recorder's instrument files hold the whole venue's list, refreshed every 60 minutes, so they
rotate late. The compactor reads every instrument file, the current run's open one included (read
only, up to its last complete batch), and keeps the definitions of the instruments it records.
Each hour gets the newest definition read up to its end, with ``ts_init`` set to the hour, so any
single hour or day carries its own definition.
"""

import logging
from functools import partial
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from recorder.compactor.convert import FeatherFile, read_chunks
from recorder.compactor.store import COMPRESSION, INSTRUMENTS_TABLE, CatalogStore, DoneRecord, atomic_write, stream_key
from trading_core.marketdata.hours import hour_last_ns, hour_of

log = logging.getLogger(__name__)


class InstrumentDefinitions:
    """The definitions read so far, per instrument, oldest first."""

    def __init__(self, store: CatalogStore) -> None:
        self._store = store
        self._defs: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._rows_read: dict[str, int] = {}
        self.newest_ts: dict[str, int] = {}  # file -> newest ts_init read from it
        self._missing_logged: set[str] = set()

    def read(self, file: FeatherFile, wanted: set[str]) -> None:
        """Read the rows of an instrument file that were not read before."""
        rel = self._store.relative(file.path)
        skip = self._rows_read.get(rel, 0)
        seen = 0
        for chunk in read_chunks(file.path):
            rows = chunk.num_rows
            if seen + rows > skip:
                part = chunk.slice(max(0, skip - seen))
                ts = part.column("ts_init").to_numpy()
                self.newest_ts[rel] = max(self.newest_ts.get(rel, 0), int(ts.max()))
                ours = part.filter(pc.is_in(part.column("id"), value_set=pa.array(sorted(wanted))))
                if ours.num_rows:
                    for instrument in ArrowSerializer.deserialize(CryptoPerpetual, ours):
                        self._add(instrument.id.value, instrument.ts_init, CryptoPerpetual.to_dict(instrument))
            seen += rows
        self._rows_read[rel] = max(seen, skip)

    def _add(self, instrument_id: str, ts: int, definition: dict[str, Any]) -> None:
        defs = self._defs.setdefault(instrument_id, [])
        if defs and _same(defs[-1][1], definition):
            return  # the hourly refresh repeats an unchanged definition
        defs.append((ts, definition))
        defs.sort(key=lambda item: item[0])

    def definition_at(self, instrument_id: str, end_ns: int, done: DoneRecord) -> dict[str, Any] | None:
        """The newest definition read with ``ts_init`` at or before ``end_ns``, else the carried one."""
        best = None
        for ts, definition in self._defs.get(instrument_id, []):
            if ts <= end_ns:
                best = definition
        return best if best is not None else done.instruments.get(instrument_id)

    def write_hours(self, instruments: set[str], done: DoneRecord, stream_tables: tuple[str, ...]) -> bool:
        """Write the instrument row of every finished stream hour that lacks one. True if any was written."""
        wrote = False
        for instrument_id in sorted(instruments):
            key = stream_key(INSTRUMENTS_TABLE, instrument_id)
            hours: set[int] = set()
            for table in stream_tables:
                hours |= set(done.hours.get(stream_key(table, instrument_id), {}))
            for hour in sorted(hours - set(done.hours.get(key, {}))):
                definition = self.definition_at(instrument_id, hour_last_ns(hour), done)
                if definition is None:
                    if instrument_id not in self._missing_logged:
                        self._missing_logged.add(instrument_id)
                        log.warning(
                            "no instrument definition yet",
                            extra={"fields": {"code": "instrument_definition_missing", "instrument_id": instrument_id}},
                        )
                    continue
                row = CryptoPerpetual.from_dict({**definition, "ts_init": hour})
                table = pa.Table.from_batches([ArrowSerializer.serialize(row)])
                path = self._store.data_file(INSTRUMENTS_TABLE, instrument_id, hour)
                atomic_write(path, partial(pq.write_table, table, compression=COMPRESSION))
                done.hours.setdefault(key, {})[hour] = {"rows": 1, "bytes": path.stat().st_size}
                done.instruments[instrument_id] = definition
                wrote = True
        return wrote

    def deletable(self, file: FeatherFile, instruments: set[str], done: DoneRecord) -> bool:
        """A fully read, closed instrument file can go once every instrument has a row past its rows."""
        newest = self.newest_ts.get(self._store.relative(file.path))
        if newest is None:
            return False
        for instrument_id in instruments:
            hours = done.hours.get(stream_key(INSTRUMENTS_TABLE, instrument_id), {})
            if not hours or max(hours) < hour_of(newest):
                return False
        return True


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ignore = {"ts_event", "ts_init"}
    return {k: v for k, v in a.items() if k not in ignore} == {k: v for k, v in b.items() if k not in ignore}
