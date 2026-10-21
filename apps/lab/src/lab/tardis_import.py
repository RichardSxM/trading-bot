"""``lab import-tardis``: free Tardis days into the lab's catalog, hour by hour (spec 0008 AC-1, AC-2).

Each first of month day of ``cryptofacilities`` ``PF_XBTUSD`` is downloaded as CSV into
``tardis/.raw/<day>/`` and converted into spec 0007's layout under ``tardis/``:

- Rows go through the compactor's reorder window, then into hour files by ``ts_init``.
- Each hour of deltas opens with the carried book as snapshot rows (the compactor's
  ``BookCarry``). The day's first snapshot block is written as it is; every later block becomes
  the level changes against the carried book (``diff_book``), so no ``CLEAR`` follows an hour's
  start and resting orders keep their place in the queue on replay.
- A pause of 10 s or more between raw rows is a ``reconnect`` gap when it ends in a later
  snapshot block, else a ``silence`` gap.
- Trades go to ``trade_tick``, the pinned instrument to one ``crypto_perpetual`` row per hour,
  and funding rates (only where the rate changes) to ``funding_rate_update``.

The manifest is written last, as the day's commit point; then the CSV files go. A day without a
manifest is converted again from scratch, to the same bytes.
"""

import gzip
import json
import logging
import shutil
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from nautilus_trader.adapters.tardis.loaders import TardisCSVDataLoader
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import FundingRateUpdate, IndexPriceUpdate, MarkPriceUpdate, OrderBookDelta
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.catalog import (
    DELTAS_TABLE,
    FUNDING_TABLE,
    INDEX_TABLE,
    INSTRUMENTS_TABLE,
    MARK_TABLE,
    OPEN_INTEREST_TABLE,
    TRADES_TABLE,
    SourceTree,
)
from lab.instruments import pinned_instrument
from lab.settings import LabSettings
from trading_core.catalog.book import CLEAR, F_LAST, BookCarry
from trading_core.catalog.days import build_manifest, manifest_text, write_gaps, write_text
from trading_core.catalog.layout import CatalogLayout
from trading_core.catalog.reorder import ReorderWindow
from trading_core.catalog.sink import COMPRESSION, TMP_SUFFIX, HourSink, atomic_write
from trading_core.lab.book_diff import Level, LevelChange, Side, diff_book
from trading_core.lab.segments import day_start_ns
from trading_core.lab.tardis_days import fits_under_cap
from trading_core.marketdata.gaps import Gap, GapKind
from trading_core.marketdata.hours import GB, HOUR_NS, SECOND_NS
from trading_core.node.context_types import OpenInterestUpdate

log = logging.getLogger(__name__)

EXCHANGE: Final = "cryptofacilities"
SYMBOL: Final = "PF_XBTUSD"
INSTRUMENT_ID: Final = "PF_XBTUSD.KRAKEN"
BOOK_CSV: Final = "incremental_book_L2"
TRADES_CSV: Final = "trades"
TICKER_CSV: Final = "derivative_ticker"
DATA_TYPES: Final = (BOOK_CSV, TRADES_CSV, TICKER_CSV)
# The tables a Tardis day's ticker CSV gives (spec 0009 AC-15): Tardis has no liquidations.
CONTEXT_TABLES: Final = (FUNDING_TABLE, MARK_TABLE, INDEX_TABLE, OPEN_INTEREST_TABLE)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_CATALOG_FULL: Final = 3

# The compactor's settings: the reorder window and its row cap, and the silence threshold.
WINDOW_NS: Final = 600 * SECOND_NS
WINDOW_MAX_ROWS: Final = 2_000_000
SILENCE_NS: Final = 10 * SECOND_NS
# Rows per loader chunk: a few MB of Arrow at a time.
CHUNK_ROWS: Final = 100_000
# The reorder window's run name: one Tardis day is one unbroken feed.
RUN: Final = "tardis"
ADD: Final[int] = int(p3.BookAction.ADD.value)
_ACTIONS: Final = {"add": p3.BookAction.ADD, "update": p3.BookAction.UPDATE, "delete": p3.BookAction.DELETE}


class ImportFailedError(Exception):
    """A day could not be imported (download failed, or a price or size off its tick)."""


class TickMismatchError(ImportFailedError):
    """A Tardis price or size is not a multiple of the pinned instrument's increment."""


Downloader = Callable[[date, Path], None]


def raw_file(raw_dir: Path, data_type: str, day: date) -> Path:
    """The CSV of one data type and day, named as ``tardis-dev`` names it."""
    return raw_dir / f"{EXCHANGE}_{data_type}_{day.isoformat()}_{SYMBOL}.csv.gz"


def download_day(day: date, raw_dir: Path) -> None:
    """Download the day's three CSV files with ``tardis-dev``, without an API key (free days only).

    The client writes each file through a temporary name and a rename, so a killed download
    leaves no half file under the final name.
    """
    # Imported here: only the import command needs it. tardis-dev ships no type hints, and the
    # Mac's tools image does not install it (the code is ignored either way).
    import tardis_dev  # type: ignore[import-not-found, import-untyped, unused-ignore]

    try:
        tardis_dev.download_datasets(
            exchange=EXCHANGE,
            data_types=list(DATA_TYPES),
            symbols=[SYMBOL],
            from_date=day.isoformat(),
            to_date=(day + timedelta(days=1)).isoformat(),
            download_dir=str(raw_dir),
        )
    except Exception as exc:  # the client raises plain exceptions for HTTP and network errors
        raise ImportFailedError(f"download of {day} failed: {exc}") from exc
    missing = [t for t in DATA_TYPES if not raw_file(raw_dir, t, day).is_file()]
    if missing:
        raise ImportFailedError(f"download of {day} gave no file for {', '.join(missing)}")


def download_ticker(day: date, raw_dir: Path) -> None:
    """Download only the day's ``derivative_ticker`` CSV (the ``--context`` backfill, spec 0009 AC-15)."""
    import tardis_dev  # type: ignore[import-not-found, import-untyped, unused-ignore]

    try:
        tardis_dev.download_datasets(
            exchange=EXCHANGE,
            data_types=[TICKER_CSV],
            symbols=[SYMBOL],
            from_date=day.isoformat(),
            to_date=(day + timedelta(days=1)).isoformat(),
            download_dir=str(raw_dir),
        )
    except Exception as exc:  # the client raises plain exceptions for HTTP and network errors
        raise ImportFailedError(f"download of {day} failed: {exc}") from exc
    if not raw_file(raw_dir, TICKER_CSV, day).is_file():
        raise ImportFailedError(f"download of {day} gave no file for {TICKER_CSV}")


def context_manifest_file(tree: SourceTree, day: date) -> Path:
    """``manifests/context/<day>.json``: the ``--context`` backfill's commit point, in a subfolder so
    the day manifest globs never read it (spec 0009 AC-15)."""
    return tree.root / "manifests" / "context" / f"{day.isoformat()}.json"


def import_context(settings: LabSettings, days: Iterable[date], *, download: Downloader = download_ticker) -> int:
    """Backfill the context tables of days already imported (``--context``). Returns the exit code.

    A day without its day manifest is not imported yet (a plain import writes its context) and is
    skipped with a warning; a day with a context manifest is done and skipped.
    """
    tree = SourceTree(settings.lab_catalog_dir / "tardis", INSTRUMENT_ID)
    for day in days:
        if not tree.manifest_file(day).is_file():
            log.warning("lab_context_day_not_imported", extra={"fields": {"day": day.isoformat()}})
            continue
        if context_manifest_file(tree, day).is_file():
            log.info("lab_import_day_skipped", extra={"fields": {"day": day.isoformat(), "context": True}})
            continue
        try:
            import_context_day(tree, day, download=download)
        except ImportFailedError as exc:
            log.error("lab_import_failed", extra={"fields": {"day": day.isoformat(), "error": str(exc)}})
            return EXIT_FAILED
    return EXIT_OK


def import_context_day(tree: SourceTree, day: date, *, download: Downloader = download_ticker) -> None:
    """Rewrite one imported day's context tables from its ticker CSV, then commit its context manifest.

    The day's deltas, trades, gap file, and day manifest stay untouched.
    """
    instrument = pinned_instrument(INSTRUMENT_ID)
    raw_dir = tree.root / ".raw" / f"{day.isoformat()}-context"
    if not raw_file(raw_dir, TICKER_CSV, day).is_file():
        shutil.rmtree(raw_dir, ignore_errors=True)
        raw_dir.mkdir(parents=True)
        download(day, raw_dir)
    # Removed only once the download is in, so a failed one leaves the day's funding in place.
    _remove_hours(tree, day, CONTEXT_TABLES)
    loader = TardisCSVDataLoader(
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        instrument_id=InstrumentId.from_str(INSTRUMENT_ID),
    )
    counts = DayCounts()
    ticker = raw_file(raw_dir, TICKER_CSV, day)
    convert_funding(tree.layout, loader, ticker, counts)
    convert_ticker_context(tree.layout, instrument, ticker, counts)
    tables = {}
    for table in CONTEXT_TABLES:
        stats = counts.hours.get(f"{table}/{INSTRUMENT_ID}", {}).values()
        tables[table] = {"rows": sum(s["rows"] for s in stats), "bytes": sum(s["bytes"] for s in stats)}
    manifest = {"day": day.isoformat(), "tables": tables}
    atomic_write(
        context_manifest_file(tree, day),
        partial(write_text, json.dumps(manifest, indent=1, sort_keys=True)),
    )
    shutil.rmtree(raw_dir, ignore_errors=True)
    log.info("lab_import_context_done", extra={"fields": {"day": day.isoformat(), "tables": tables}})


def tree_bytes(root: Path) -> int:
    """Bytes of every file under ``root`` (both source trees and any ``.raw`` files)."""
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def import_tardis(settings: LabSettings, days: Iterable[date], *, download: Downloader = download_day) -> int:
    """Import the given first of month days in order. Returns the exit code.

    A day with a manifest is skipped. Before each other day, the cap check (AC-2) stops the
    import with exit 3; the days already converted stay.
    """
    tree = SourceTree(settings.lab_catalog_dir / "tardis", INSTRUMENT_ID)
    for day in days:
        if tree.manifest_file(day).is_file():
            log.info("lab_import_day_skipped", extra={"fields": {"day": day.isoformat()}})
            continue
        used = tree_bytes(settings.lab_catalog_dir)
        if not fits_under_cap(used, settings.lab_tardis_day_gb, settings.lab_catalog_cap_gb):
            log.error(
                "lab_catalog_full",
                extra={
                    "fields": {
                        "day": day.isoformat(),
                        "used_gb": round(used / GB, 3),
                        "day_gb": str(settings.lab_tardis_day_gb),
                        "cap_gb": str(settings.lab_catalog_cap_gb),
                    }
                },
            )
            return EXIT_CATALOG_FULL
        try:
            import_day(tree, day, download=download)
        except TickMismatchError as exc:
            log.error("lab_tick_mismatch", extra={"fields": {"day": day.isoformat(), "error": str(exc)}})
            return EXIT_FAILED
        except ImportFailedError as exc:
            log.error("lab_import_failed", extra={"fields": {"day": day.isoformat(), "error": str(exc)}})
            return EXIT_FAILED
    return EXIT_OK


@dataclass
class DayCounts:
    """What one converted day holds, per stream key: hour stats and late rows."""

    hours: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    incomplete: dict[str, dict[int, int]] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    snapshot_diffs: int = 0


def import_day(tree: SourceTree, day: date, *, download: Downloader = download_day) -> None:
    """Download (unless the CSV files are there) and convert one day, then commit it with its manifest."""
    instrument = pinned_instrument(INSTRUMENT_ID)
    raw_dir = tree.root / ".raw" / day.isoformat()
    _remove_leftovers(tree, day)
    if not all(raw_file(raw_dir, t, day).is_file() for t in DATA_TYPES):
        shutil.rmtree(raw_dir, ignore_errors=True)
        raw_dir.mkdir(parents=True)
        download(day, raw_dir)
    check_ticks(instrument, raw_file(raw_dir, BOOK_CSV, day), raw_file(raw_dir, TRADES_CSV, day))
    dropped = drop_off_tick_trades(instrument, raw_file(raw_dir, TRADES_CSV, day))
    if dropped:
        log.warning(
            "lab_off_tick_trades_dropped",
            extra={"fields": {"day": day.isoformat(), "rows": dropped.rows, "prices": dropped.prices[:3]}},
        )
    loader = TardisCSVDataLoader(
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        instrument_id=InstrumentId.from_str(INSTRUMENT_ID),
    )
    counts = DayCounts()
    convert_deltas(tree.layout, loader, raw_file(raw_dir, BOOK_CSV, day), counts)
    convert_trades(tree.layout, loader, raw_file(raw_dir, TRADES_CSV, day), counts)
    convert_funding(tree.layout, loader, raw_file(raw_dir, TICKER_CSV, day), counts)
    convert_ticker_context(tree.layout, instrument, raw_file(raw_dir, TICKER_CSV, day), counts)
    write_instruments(tree.layout, instrument, counts)
    write_gaps(tree.layout, counts.gaps)
    manifest = build_manifest(tree.layout, day_start_ns(day), counts.hours, counts.incomplete)
    atomic_write(tree.manifest_file(day), partial(write_text, manifest_text(manifest)))
    shutil.rmtree(raw_dir, ignore_errors=True)
    log.info(
        "lab_import_day_done",
        extra={
            "fields": {
                "day": day.isoformat(),
                "rows": sum(s["rows"] for s in manifest["streams"]),
                "bytes": manifest["total_bytes"],
                "gaps": len(counts.gaps),
                "snapshot_diffs": counts.snapshot_diffs,
                "late_rows": sum(n for hours in counts.incomplete.values() for n in hours.values()),
                "off_tick_trades": dropped.rows,
            }
        },
    )


def _remove_leftovers(tree: SourceTree, day: date) -> None:
    """Delete what a killed run of this day may have left, so it converts again from scratch."""
    _remove_hours(tree, day, (DELTAS_TABLE, TRADES_TABLE, INSTRUMENTS_TABLE, *CONTEXT_TABLES))
    tree.layout.gap_file(day.isoformat()).unlink(missing_ok=True)


def _remove_hours(tree: SourceTree, day: date, tables: Iterable[str]) -> None:
    start = day_start_ns(day)
    for table in tables:
        for hour in range(start, start + 24 * HOUR_NS, HOUR_NS):
            path = tree.layout.data_file(table, INSTRUMENT_ID, hour)
            path.unlink(missing_ok=True)
            path.with_name(path.name + TMP_SUFFIX).unlink(missing_ok=True)


def check_ticks(instrument: CryptoPerpetual, book_csv: Path, trades_csv: Path) -> None:
    """Refuse a day whose book prices, or any sizes, are not multiples of the pinned increments (no rounding).

    The loader would round them to the instrument's precision silently, so the raw CSV text is
    checked first. A trade price off the tick is not refused here: ``drop_off_tick_trades`` removes
    those rows (spec 0016, assumed).
    """
    price_step = instrument.price_increment.as_decimal()
    size_step = instrument.size_increment.as_decimal()
    for path in (book_csv, trades_csv):
        bad = [f"price {p}" for p in _off_step(_distinct(path, "price"), price_step)] if path == book_csv else []
        bad += [f"size {s}" for s in _off_step(_distinct(path, "amount"), size_step)]
        if bad:
            raise TickMismatchError(f"{path.name}: {len(bad)} values off the tick, such as {', '.join(bad[:3])}")


@dataclass(frozen=True)
class DroppedTrades:
    """The trade rows ``drop_off_tick_trades`` removed: how many, and their distinct prices."""

    rows: int = 0
    prices: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.rows > 0


def drop_off_tick_trades(instrument: CryptoPerpetual, trades_csv: Path) -> DroppedTrades:
    """Remove the trade rows whose price is off the pinned tick, rewriting the CSV in place (spec 0016, assumed).

    From 2023-10 Kraken's trade feed carries a few bursts a day of tiny fills at one computed price
    (such as 27567.42278253538), never a price the book could show. Rounding would invent a price,
    so the rows go; every other value is kept as its original text. The rewrite goes through a
    temporary name, so a killed run leaves the original or the filtered file, never half of one.
    """
    off = _off_step(_distinct(trades_csv, "price"), instrument.price_increment.as_decimal())
    if not off:
        return DroppedTrades()
    with pa.input_stream(trades_csv, compression="gzip") as source:
        table = pacsv.read_csv(source, convert_options=pacsv.ConvertOptions(column_types=_all_text(trades_csv)))
    keep = pc.invert(pc.is_in(table.column("price"), value_set=pa.array(off, pa.string())))
    kept = table.filter(keep)
    tmp = trades_csv.with_name(trades_csv.name + TMP_SUFFIX)
    with pa.output_stream(tmp, compression="gzip") as sink:
        pacsv.write_csv(kept, sink, write_options=pacsv.WriteOptions(quoting_style="none"))
    tmp.replace(trades_csv)
    return DroppedTrades(table.num_rows - kept.num_rows, off)


def _off_step(values: set[str], step: Decimal) -> list[str]:
    return [v for v in sorted(values) if Decimal(v) % step]


def _all_text(path: Path) -> dict[str, pa.DataType]:
    with gzip.open(path, "rt") as source:
        header = source.readline().strip().split(",")
    return dict.fromkeys(header, pa.string())


def _distinct(path: Path, column: str) -> set[str]:
    options = pacsv.ConvertOptions(include_columns=[column], column_types={column: pa.string()})
    reader = pacsv.open_csv(path, convert_options=options, read_options=pacsv.ReadOptions(block_size=64 << 20))
    found: set[str] = set()
    for batch in reader:
        found.update(pc.unique(batch.column(column)).to_pylist())
    return found


class HourFiles:
    """The hour files of one stream, one open at a time, committed with their row count checked."""

    def __init__(self, layout: CatalogLayout, table: str, counts: DayCounts) -> None:
        self._layout = layout
        self.key = f"{table}/{INSTRUMENT_ID}"
        self._table = table
        self._stats = counts.hours.setdefault(self.key, {})
        self._sink: HourSink | None = None
        self._rows = 0

    @property
    def hour(self) -> int | None:
        """The hour open now."""
        return None if self._sink is None else self._sink.hour

    def open(self, hour: int, schema: Any) -> None:
        """Commit the open hour, if any, and start ``hour``."""
        self.close()
        self._sink = HourSink(self._layout.data_file(self._table, INSTRUMENT_ID, hour), schema, hour)
        self._rows = 0

    def write(self, rows: Any) -> None:
        """Append rows to the open hour."""
        assert self._sink is not None
        self._sink.write(rows)
        self._rows += rows.num_rows

    def close(self) -> None:
        """Commit the open hour."""
        sink, self._sink = self._sink, None
        if sink is not None:
            size = sink.commit(self._rows)
            self._stats[sink.hour] = {"rows": self._rows, "bytes": size}


def by_hour(rows: Any) -> Iterator[tuple[int, Any]]:
    """The rows of a ``ts_init`` ordered table, cut at hour boundaries."""
    ts = rows.column("ts_init").to_numpy()
    hours = ts - ts % HOUR_NS
    cuts = [0, *(np.flatnonzero(hours[1:] != hours[:-1]) + 1).tolist(), len(ts)]
    for start, end in pairwise(cuts):
        if end > start:
            yield int(hours[start]), rows.slice(start, end - start)


def in_order(tables: Iterable[Any], key: str, counts: DayCounts) -> Iterator[Any]:
    """Tables through the reorder window (spec 0007 AC-12). Late rows are dropped and their hours
    marked incomplete (the gate saw none on a Tardis day: ``local_timestamp`` never goes back)."""
    window = ReorderWindow(key, window_ns=WINDOW_NS, max_rows=WINDOW_MAX_ROWS)
    for table in tables:
        released, late = window.push(table, RUN)
        if late is not None:
            ts = late.rows.column("ts_init").to_numpy()
            hours, n = np.unique(ts - ts % HOUR_NS, return_counts=True)
            incomplete = counts.incomplete.setdefault(key, {})
            for hour, rows in zip(hours.tolist(), n.tolist(), strict=True):
                incomplete[hour] = incomplete.get(hour, 0) + rows
            log.warning("lab_import_late_rows", extra={"fields": {"stream": key, "rows": late.rows.num_rows}})
        if released is not None:
            yield released
    rest = window.end_run()
    if rest is not None:
        yield rest


def _arrow(data: bytes, metadata: Any = None) -> Any:
    table = pa.ipc.open_stream(pa.py_buffer(data)).read_all()
    return table if metadata is None else table.replace_schema_metadata(metadata)


class DeltaWriter:
    """Book deltas into hour files: hour start snapshots, the day's first block, later blocks as diffs, gaps."""

    def __init__(self, layout: CatalogLayout, counts: DayCounts, *, size_precision: int, price_precision: int) -> None:
        self._files = HourFiles(layout, DELTAS_TABLE, counts)
        self._counts = counts
        self.carry = BookCarry(INSTRUMENT_ID)
        self._instrument = p3.InstrumentId.from_str(INSTRUMENT_ID)
        self._price_precision = price_precision
        self._size_precision = size_precision
        self._schema: Any = None
        self._block: list[Any] = []
        self._block_ts = 0
        self._first_block_done = False
        self._last_ts: int | None = None
        self._clears_seen = 0

    def feed(self, table: Any) -> None:
        """Take the next rows in ``ts_init`` order."""
        if table.num_rows == 0:
            return
        if self._schema is None:
            self._schema = table.schema
        ts = table.column("ts_init").to_numpy()
        actions = table.column("action").to_numpy()
        self._scan_gaps(ts, actions == CLEAR)
        n, pos = table.num_rows, 0
        if self._block:
            end = self._block_end(ts, actions, 0)
            self._block.append(table.slice(0, end))
            if end == n:
                return  # the block may go on in the next table
            self._finish_block()
            pos = end
        while pos < n:
            clears = np.flatnonzero(actions[pos:] == CLEAR)
            clear = pos + int(clears[0]) if clears.size else n
            if clear > pos:
                self._write_plain(table.slice(pos, clear - pos))
            if clear == n:
                return
            self._block_ts = int(ts[clear])
            end = self._block_end(ts, actions, clear + 1)
            self._block = [table.slice(clear, end - clear)]
            if end == n:
                return
            self._finish_block()
            pos = end

    def finish(self) -> None:
        """The day's rows have all been fed: close the last block and hour."""
        if self._block:
            self._finish_block()
        self._files.close()

    def _block_end(self, ts: Any, actions: Any, start: int) -> int:
        # A snapshot block is its CLEAR and the ADD rows after it at the same ts_init.
        outside = (actions[start:] != ADD) | (ts[start:] != self._block_ts)
        return start + int(np.argmax(outside)) if outside.any() else len(ts)

    def _scan_gaps(self, ts: Any, is_clear: Any) -> None:
        """Pauses of 10 s or more between raw rows, blocks included: ``reconnect`` when the pause
        ends in a snapshot block after the day's first, else ``silence``."""
        # Each row against the row before it; the day's first row has none.
        first = ts[0] if self._last_ts is None else self._last_ts
        previous = np.concatenate(([first], ts[:-1]))
        for i in np.flatnonzero(ts - previous >= SILENCE_NS).tolist():
            later_block = bool(is_clear[i]) and (self._clears_seen + int(is_clear[:i].sum())) > 0
            kind: GapKind = "reconnect" if later_block else "silence"
            self._counts.gaps.append(Gap(int(previous[i]), INSTRUMENT_ID, kind, int(ts[i])))
        self._last_ts = int(ts[-1])
        self._clears_seen += int(is_clear.sum())

    def _open_hour(self, hour: int) -> None:
        if self._files.hour == hour:
            return
        self._files.open(hour, self._schema)
        if self.carry.valid:
            self._files.write(self.carry.snapshot_rows(hour, self._schema))

    def _write_plain(self, rows: Any) -> None:
        for hour, part in by_hour(rows):
            self._open_hour(hour)
            self._files.write(part)
            self.carry.apply(part)

    def _finish_block(self) -> None:
        block = pa.concat_tables(self._block)
        self._block = []
        self._open_hour(self._block_ts - self._block_ts % HOUR_NS)
        if not self._first_block_done:
            # The day's first block is the snapshot every later hour builds on.
            self._first_block_done = True
            self._files.write(block)
            self.carry.apply(block)
            return
        self._counts.snapshot_diffs += 1
        changes = diff_book(self._carried_levels(), _block_levels(block))
        if not changes:
            return
        rows = self._change_rows(changes, block)
        self._files.write(rows)
        self.carry.apply(rows)

    def _carried_levels(self) -> list[Level]:
        levels = []
        sides: tuple[tuple[Side, Any], ...] = (("bid", self.carry.book.bids()), ("ask", self.carry.book.asks()))
        for side, book_levels in sides:
            for level in book_levels:
                first = level.first()
                if first is not None:
                    levels.append(Level(side, Decimal(str(first.price)), Decimal(str(first.size))))
        return levels

    def _change_rows(self, changes: list[LevelChange], block: Any) -> Any:
        """The level changes as delta rows with the block's ``CLEAR`` times and sequence, ``F_LAST`` last."""
        ts_event = int(block.column("ts_event")[0].as_py())
        ts_init = int(block.column("ts_init")[0].as_py())
        sequence = int(block.column("sequence")[0].as_py())
        last = len(changes) - 1
        deltas = []
        for i, change in enumerate(changes):
            side = p3.OrderSide.BUY if change.side == "bid" else p3.OrderSide.SELL
            price = p3.Price.from_str(f"{change.price:.{self._price_precision}f}")
            size = p3.Quantity.from_str(f"{change.size:.{self._size_precision}f}")
            order = p3.BookOrder(side, price, size, 0)
            flags = F_LAST if i == last else 0
            deltas.append(
                p3.OrderBookDelta(self._instrument, _ACTIONS[change.action], order, flags, sequence, ts_event, ts_init)
            )
        return _arrow(p3.book_deltas_to_arrow_record_batch_bytes(deltas), self._schema.metadata)


def _block_levels(block: Any) -> list[Level]:
    """The levels of a snapshot block (its ``ADD`` rows; a level without size holds nothing)."""
    levels = []
    for delta in ArrowSerializer.deserialize(OrderBookDelta, block.slice(1)):
        size = delta.order.size.as_decimal()
        if size > 0:
            side: Side = "bid" if delta.order.side == OrderSide.BUY else "ask"
            levels.append(Level(side, delta.order.price.as_decimal(), size))
    return levels


def convert_deltas(layout: CatalogLayout, loader: TardisCSVDataLoader, path: Path, counts: DayCounts) -> None:
    """The book CSV into ``order_book_deltas`` hour files."""
    price_precision, size_precision = loader_precision(loader, "price"), loader_precision(loader, "size")
    writer = DeltaWriter(layout, counts, price_precision=price_precision, size_precision=size_precision)
    chunks = (
        _arrow(p3.book_deltas_to_arrow_record_batch_bytes(chunk))
        for chunk in loader.stream_deltas(path, chunk_size=CHUNK_ROWS, as_legacy_cython=False)
    )
    for table in in_order(chunks, f"{DELTAS_TABLE}/{INSTRUMENT_ID}", counts):
        writer.feed(table)
    writer.finish()


def loader_precision(loader: TardisCSVDataLoader, kind: str) -> int:
    """The price or size precision the loader was built with."""
    value = getattr(loader, f"_{kind}_precision")
    if not isinstance(value, int):
        raise TypeError(f"the loader has no {kind} precision")
    return value


def convert_trades(layout: CatalogLayout, loader: TardisCSVDataLoader, path: Path, counts: DayCounts) -> None:
    """The trades CSV into ``trade_tick`` hour files."""
    files = HourFiles(layout, TRADES_TABLE, counts)
    chunks = (
        _arrow(p3.trades_to_arrow_record_batch_bytes(chunk))
        for chunk in loader.stream_trades(path, chunk_size=CHUNK_ROWS, as_legacy_cython=False)
    )
    for table in in_order(chunks, files.key, counts):
        for hour, part in by_hour(table):
            if files.hour != hour:
                files.open(hour, table.schema)
            files.write(part)
    files.close()


def convert_funding(layout: CatalogLayout, loader: TardisCSVDataLoader, path: Path, counts: DayCounts) -> None:
    """The ticker CSV's funding rates into ``funding_rate_update`` hour files, every row, so its age
    matches our own recorded funding (spec 0009 AC-15)."""
    files = HourFiles(layout, FUNDING_TABLE, counts)

    def rows() -> Iterator[Any]:
        for chunk in loader.stream_funding_rates(path, chunk_size=CHUNK_ROWS):
            for update in chunk:
                if not isinstance(update, FundingRateUpdate):
                    raise TypeError(f"unexpected funding row {type(update).__name__}")
            if chunk:
                # The funding serializer takes one update at a time.
                yield pa.Table.from_batches([ArrowSerializer.serialize(u) for u in chunk])

    for table in in_order(rows(), files.key, counts):
        for hour, part in by_hour(table):
            if files.hour != hour:
                files.open(hour, table.schema)
            files.write(part)
    files.close()


def _ticker_values(path: Path, column: str) -> Iterator[tuple[int, int, str]]:
    """``(ts_event, ts_init, text)`` of every ticker row with a value in ``column``, in file order.

    ``timestamp`` and ``local_timestamp`` are microseconds; the value stays text, never a float.
    """
    times = ("timestamp", "local_timestamp")
    options = pacsv.ConvertOptions(
        include_columns=[*times, column],
        column_types={"timestamp": pa.int64(), "local_timestamp": pa.int64(), column: pa.string()},
        strings_can_be_null=False,
    )
    reader = pacsv.open_csv(path, convert_options=options, read_options=pacsv.ReadOptions(block_size=16 << 20))
    for batch in reader:
        events = batch.column("timestamp").to_pylist()
        inits = batch.column("local_timestamp").to_pylist()
        for event, init, text in zip(events, inits, batch.column(column).to_pylist(), strict=True):
            if text:
                yield event * 1_000, init * 1_000, text


def convert_ticker_context(layout: CatalogLayout, instrument: CryptoPerpetual, path: Path, counts: DayCounts) -> None:
    """The ticker CSV's mark price, index price, and open interest into their hour files, one row per
    CSV row that has the value (spec 0009 AC-15).

    Mark and index are at the instrument's price precision, as the Kraken adapter gives them live (the
    spec 0009 gate); open interest at its size precision, as the context feed records it.
    """
    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)

    def mark(text: str, event: int, init: int) -> Any:
        return MarkPriceUpdate(instrument_id, instrument.make_price(Decimal(text)), event, init)

    def index(text: str, event: int, init: int) -> Any:
        return IndexPriceUpdate(instrument_id, instrument.make_price(Decimal(text)), event, init)

    def open_interest(text: str, event: int, init: int) -> Any:
        return OpenInterestUpdate(instrument_id, instrument.make_qty(Decimal(text)), event, init)

    streams: tuple[tuple[str, str, type, Callable[[str, int, int], Any]], ...] = (
        (MARK_TABLE, "mark_price", MarkPriceUpdate, mark),
        (INDEX_TABLE, "index_price", IndexPriceUpdate, index),
        (OPEN_INTEREST_TABLE, "open_interest", OpenInterestUpdate, open_interest),
    )
    for table_name, column, cls, make in streams:
        files = HourFiles(layout, table_name, counts)

        def tables(column: str = column, cls: type = cls, make: Callable[[str, int, int], Any] = make) -> Iterator[Any]:
            batch: list[Any] = []
            for event, init, text in _ticker_values(path, column):
                try:
                    batch.append(make(text, event, init))
                except (InvalidOperation, ValueError) as exc:
                    raise ImportFailedError(f"{path.name}: {column} {text!r} is not a number") from exc
                if len(batch) >= CHUNK_ROWS:
                    yield ArrowSerializer.serialize_batch(batch, data_cls=cls)
                    batch = []
            if batch:
                yield ArrowSerializer.serialize_batch(batch, data_cls=cls)

        for table in in_order(tables(), files.key, counts):
            for hour, part in by_hour(table):
                if files.hour != hour:
                    files.open(hour, table.schema)
                files.write(part)
        files.close()


def write_instruments(layout: CatalogLayout, instrument: CryptoPerpetual, counts: DayCounts) -> None:
    """One pinned instrument row per hour that has deltas or trades, with ``ts_init`` set to the hour."""
    key = f"{INSTRUMENTS_TABLE}/{INSTRUMENT_ID}"
    stats = counts.hours.setdefault(key, {})
    hours: set[int] = set()
    for table in (DELTAS_TABLE, TRADES_TABLE):
        hours |= set(counts.hours.get(f"{table}/{INSTRUMENT_ID}", {}))
    definition = CryptoPerpetual.to_dict(instrument)
    for hour in sorted(hours):
        row = CryptoPerpetual.from_dict({**definition, "ts_init": hour})
        table = pa.Table.from_batches([ArrowSerializer.serialize(row)])
        path = layout.data_file(INSTRUMENTS_TABLE, INSTRUMENT_ID, hour)
        atomic_write(path, partial(pq.write_table, table, compression=COMPRESSION))
        stats[hour] = {"rows": 1, "bytes": path.stat().st_size}


def prune(settings: LabSettings, source: str | None) -> int:
    """Delete one source tree (both with no source) and return the bytes freed (AC-2).

    A file that cannot be deleted raises ``OSError``: the cap check trusts these bytes to be gone.
    """
    roots = [settings.lab_catalog_dir / name for name in ((source,) if source else ("tardis", "own"))]
    freed = 0
    for root in roots:
        if not root.exists():
            continue
        size = tree_bytes(root)
        shutil.rmtree(root)
        freed += size
    log.info("lab_pruned", extra={"fields": {"roots": [str(r) for r in roots], "bytes": freed}})
    return freed
