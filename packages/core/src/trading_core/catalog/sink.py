"""Atomic writes and the Parquet file of one hour (spec 0007)."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from trading_core.marketdata.errors import HourCountMismatch

TMP_SUFFIX: Final = ".tmp"
COMPRESSION: Final = "zstd"
# Rows per Parquet row group. Fixed, so a file's bytes never depend on how its rows arrived.
ROW_GROUP_ROWS: Final = 8_192


def atomic_write(path: Path, write: Callable[[Path], object]) -> None:
    """Write through ``<name>.tmp`` and a rename, so a reader never sees a half written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    write(tmp)
    os.replace(tmp, path)


class HourSink:
    """The Parquet file of one hour being written, under ``<name>.tmp`` until its commit.

    Rows are buffered and written in row groups of exactly ``ROW_GROUP_ROWS`` (the last one
    shorter), so the same rows give the same bytes whatever chunks they came in.
    """

    def __init__(self, path: Path, schema: Any, hour: int) -> None:
        self.path = path
        self.schema = schema
        self.hour = hour
        self._tmp = path.with_name(path.name + TMP_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: Any = pq.ParquetWriter(str(self._tmp), schema, compression=COMPRESSION)
        self._pending: list[Any] = []
        self._pending_rows = 0

    def write(self, table: Any) -> None:
        """Append rows. The schema metadata (instrument and precisions) always comes from the hour."""
        self._pending.append(table.replace_schema_metadata(self.schema.metadata))
        self._pending_rows += table.num_rows
        if self._pending_rows >= ROW_GROUP_ROWS:
            self._drain(final=False)

    def _drain(self, *, final: bool) -> None:
        if not self._pending:
            return
        data = pa.concat_tables(self._pending)
        full = data.num_rows if final else data.num_rows - data.num_rows % ROW_GROUP_ROWS
        for start in range(0, full, ROW_GROUP_ROWS):
            self._writer.write_table(data.slice(start, min(ROW_GROUP_ROWS, full - start)))
        rest = data.slice(full)
        self._pending = [rest] if rest.num_rows else []
        self._pending_rows = rest.num_rows

    def commit(self, expected: int) -> int:
        """Close, check the footer's row count against ``expected``, rename. Returns the file's bytes."""
        self._drain(final=True)
        self._writer.close()
        written = pq.ParquetFile(str(self._tmp)).metadata.num_rows
        if written != expected:
            self._tmp.unlink(missing_ok=True)
            raise HourCountMismatch(self.hour, expected, written)
        os.replace(self._tmp, self.path)
        return self.path.stat().st_size

    def abort(self) -> None:
        """Drop the unfinished file."""
        self._pending = []
        try:
            self._writer.close()
        finally:
            self._tmp.unlink(missing_ok=True)
