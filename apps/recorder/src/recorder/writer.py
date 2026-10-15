"""The recorder's buffered feather writer (spec 0012).

Nautilus 1.231.0's ``StreamingFeatherWriter`` serializes and writes every message on its own, which
caps the recorder near 1,850 messages a second. This writer holds each stream's messages and
writes them as one batch on the flush tick, at the cap, at rotation, on stop, and at close. The run
folder it writes (paths, names, schemas, metadata, rows) is what 1.231.0 writes, pinned by
``tests/test_writer_parity.py``, so the compactor reads it unchanged.

It fails closed: any error opening, serializing, writing, flushing, or closing a file trips it,
refuses every later write, and stops the recorder (exit 75), so a loss always shows as a gap.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final

import pyarrow as pa
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import Clock
from nautilus_trader.model.data import (
    CustomData,
    FundingRateUpdate,
    IndexPriceUpdate,
    MarkPriceUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer, list_schemas

from recorder.disk import stop_process
from trading_core.marketdata.writer import (
    MS_NS,
    busy_pct,
    busy_warn,
    next_boundary_ns,
    rotation_due,
    should_flush,
    slow_flush_due,
)

log = logging.getLogger(__name__)

# The tables 1.231.0 writes one file per instrument for (its ``_per_instrument_writers``, as is).
PER_INSTRUMENT_TABLES: Final = frozenset(
    {
        "bar",
        "order_book_deltas",
        "order_book_depths",
        "quote_tick",
        "trade_tick",
        "funding_rate_update",
        "index_price_update",
        "mark_price_update",
        "option_greeks",
    }
)
CONFIG_FILE: Final = "config.json"

StreamKey = str | tuple[str, str]


class WriterFailed(Exception):
    """A feather file could not be opened, written, flushed, or closed."""


@dataclass
class _Stream:
    """One feather file: a shared table (``instrument_id`` None) or a table for one instrument."""

    table: str
    instrument_id: str | None
    cls: type
    file: BinaryIO
    writer: Any  # pa.RecordBatchStreamWriter
    boundary_ns: int
    held: list[object] = field(default_factory=list)


@dataclass
class _Stats:
    """Counters since the last stats tick (AC-6)."""

    messages: int = 0
    rows: int = 0
    flushes: int = 0
    dropped: int = 0
    busy_ns: int = 0
    max_flush_ns: int = 0
    max_queue_depth: int = 0
    max_tick_lag_ns: int = 0


class BufferedFeatherWriter:
    """Writes the recorded types to feather files under ``path``, held and written in batches.

    The constructor creates ``path``, writes ``config.json`` (``config_json``, the node config as
    the kernel wrote it), and opens every shared table file, as 1.231.0 does. ``main.py``
    subscribes ``write`` to the message bus; ``WriterFlushActor`` calls ``flush_all`` and
    ``take_stats`` on the node clock; ``main.py`` calls ``close`` after the node is disposed and
    exits 75 once ``tripped``.
    """

    def __init__(
        self,
        path: Path,
        *,
        config_json: bytes,
        cache: Cache,
        clock: Clock,
        include_types: list[type],
        flush_max_messages: int,
        stop: Callable[[], None] = stop_process,
    ) -> None:
        self.path = path
        self._cache = cache
        self._clock = clock
        self._include = include_types
        self._cap = flush_max_messages
        self._stop = stop
        self._lock = threading.Lock()
        self._schemas: dict[type, pa.Schema] = list_schemas()
        self._streams: dict[StreamKey, _Stream] = {}
        self._warned_no_instrument: set[StreamKey] = set()
        self._stats = _Stats()
        self._span_start_ns = clock.timestamp_ns()
        self._slow_logged_ns: int | None = None
        self._closed = False
        self._warned_after_close = False
        self.tripped = False
        path.mkdir(parents=True, exist_ok=True)
        (path / CONFIG_FILE).write_bytes(config_json)
        now_ns = clock.timestamp_ns()
        # 1.231.0 opens the shared table files of the included non custom types at once.
        for cls in self._schemas:
            table = class_to_filename(cls)
            if cls in self._include and table not in PER_INSTRUMENT_TABLES and not table.startswith("custom_"):
                self._streams[table] = self._open_shared(table, cls, now_ns)

    # The message bus handler.

    def write(self, obj: object) -> None:
        """Hold ``obj`` in its stream if its type is recorded; flush or rotate the stream when due."""
        with self._lock:
            start = time.perf_counter_ns()
            try:
                self._write(obj)
            finally:
                self._stats.busy_ns += time.perf_counter_ns() - start

    def _write(self, obj: object) -> None:
        if self._closed:
            self._stats.dropped += 1
            if not self._warned_after_close:
                self._warned_after_close = True
                log.warning(
                    "recorder write after the writer closed, dropped",
                    extra={"fields": {"code": "recorder_write_after_close", "type": type(obj).__name__}},
                )
            return
        cls: type = obj.data_type.type if isinstance(obj, CustomData) else type(obj)
        if cls not in self._include:
            return
        if self.tripped:
            self._stats.dropped += 1
            return
        actual: Any = obj.data if isinstance(obj, CustomData) else obj
        table = class_to_filename(cls)
        per_instrument = table in PER_INSTRUMENT_TABLES or (
            table.startswith("custom_") and hasattr(actual, "instrument_id")
        )
        instrument_id: str | None = actual.instrument_id.value if per_instrument else None
        # One read of the clock per write (AC-3).
        now_ns = self._clock.timestamp_ns()
        try:
            stream = self._stream_for(table, cls, actual, instrument_id, now_ns)
            if stream is None:
                self._stats.dropped += 1
                return
            stream.held.append(obj)
            self._stats.messages += 1
            self._stats.rows += len(obj.deltas) if isinstance(obj, OrderBookDeltas) else 1
            if rotation_due(now_ns, stream.boundary_ns):
                self._rotate(stream, actual, now_ns)
            elif should_flush(len(stream.held), self._cap):
                self._flush(stream)
        except Exception as exc:
            self._fail(table, instrument_id, exc)

    def _stream_for(self, table: str, cls: type, actual: Any, instrument_id: str | None, now_ns: int) -> _Stream | None:
        key: StreamKey = table if instrument_id is None else (table, instrument_id)
        stream = self._streams.get(key)
        if stream is not None:
            return stream
        if instrument_id is None:
            if not table.startswith("custom_"):
                return None  # 1.231.0 has no writer for it either
            stream = self._open_shared(table, cls, now_ns)
        else:
            # Like 1.231.0, a per instrument file opens only once its instrument is cached.
            if self._cache.instrument(actual.instrument_id) is None:
                if key not in self._warned_no_instrument:
                    self._warned_no_instrument.add(key)
                    log.warning(
                        "recorder message for an instrument not yet in the cache, dropped",
                        extra={
                            "fields": {
                                "code": "recorder_write_no_instrument",
                                "table": table,
                                "instrument_id": instrument_id,
                            }
                        },
                    )
                return None
            stream = self._open_instrument(table, cls, actual, instrument_id, now_ns)
        self._streams[key] = stream
        return stream

    # Opening, flushing, rotating.

    def _open_shared(self, table: str, cls: type, now_ns: int) -> _Stream:
        schema = self._schemas[cls].with_metadata({"class": cls.__name__})
        return self._open(self.path / f"{table}_{now_ns}.feather", schema, table, None, cls, now_ns)

    def _open_instrument(self, table: str, cls: type, actual: Any, instrument_id: str, now_ns: int) -> _Stream:
        schema = self._schemas[OrderBookDelta if cls is OrderBookDeltas else cls].with_metadata(self._metadata(actual))
        safe = urisafe_identifier(instrument_id)
        folder = self.path / table / safe
        folder.mkdir(parents=True, exist_ok=True)
        return self._open(folder / f"{safe}_{now_ns}.feather", schema, table, instrument_id, cls, now_ns)

    def _open(
        self, file_path: Path, schema: pa.Schema, table: str, instrument_id: str | None, cls: type, now_ns: int
    ) -> _Stream:
        f = file_path.open("wb")
        try:
            writer = pa.ipc.new_stream(f, schema)
        except Exception:
            f.close()
            raise
        return _Stream(table, instrument_id, cls, f, writer, next_boundary_ns(now_ns))

    def _metadata(self, actual: Any) -> dict[bytes, bytes]:
        # 1.231.0's ``_extract_obj_metadata`` for the types we record. Book deltas arrive as an
        # ``OrderBookDeltas`` batch, which 1.231.0 does not special case, so their precisions come
        # from the cached instrument, as for the custom types.
        metadata = {b"instrument_id": actual.instrument_id.value.encode()}
        if isinstance(actual, TradeTick):
            metadata[b"price_precision"] = str(actual.price.precision).encode()
            metadata[b"size_precision"] = str(actual.size.precision).encode()
        elif isinstance(actual, (MarkPriceUpdate, IndexPriceUpdate)):
            metadata[b"price_precision"] = str(actual.value.precision).encode()
        elif isinstance(actual, FundingRateUpdate):
            pass  # instrument_id only, as in 1.231.0
        else:
            instrument = self._cache.instrument(actual.instrument_id)
            if instrument is None:
                raise WriterFailed(f"instrument {actual.instrument_id} not in the cache")
            metadata[b"price_precision"] = str(instrument.price_precision).encode()
            metadata[b"size_precision"] = str(instrument.size_precision).encode()
        return metadata

    def _flush(self, stream: _Stream) -> None:
        if not stream.held:
            return
        start = time.perf_counter_ns()
        held, stream.held = stream.held, []
        serialized = ArrowSerializer.serialize_batch(held, data_cls=stream.cls)
        if serialized:  # an empty or None result writes nothing, as in 1.231.0
            stream.writer.write_table(serialized)
            stream.file.flush()
        took = time.perf_counter_ns() - start
        self._stats.flushes += 1
        self._stats.max_flush_ns = max(self._stats.max_flush_ns, took)
        now_ns = self._clock.timestamp_ns()
        if slow_flush_due(took, now_ns=now_ns, last_logged_ns=self._slow_logged_ns):
            self._slow_logged_ns = now_ns
            log.warning(
                "recorder flush slow",
                extra={
                    "fields": {
                        "code": "recorder_flush_slow",
                        "table": stream.table,
                        "instrument_id": stream.instrument_id,
                        "messages": len(held),
                        "ms": took // MS_NS,
                    }
                },
            )

    def _rotate(self, stream: _Stream, actual: Any, now_ns: int) -> None:
        # The crossing message is already held, so it lands in the old file, as in 1.231.0.
        self._flush(stream)
        stream.writer.close()
        stream.file.close()
        if stream.instrument_id is None:
            new = self._open_shared(stream.table, stream.cls, now_ns)
            self._streams[stream.table] = new
        else:
            new = self._open_instrument(stream.table, stream.cls, actual, stream.instrument_id, now_ns)
            self._streams[(stream.table, stream.instrument_id)] = new

    # The flush actor's calls.

    def flush_all(self, *, queue_depth: int, tick_lag_ns: int) -> None:
        """The flush tick: write every stream that holds something, and note the queue depth and
        the tick's lag behind its schedule (AC-2, AC-6)."""
        with self._lock:
            start = time.perf_counter_ns()
            try:
                self._stats.max_queue_depth = max(self._stats.max_queue_depth, queue_depth)
                self._stats.max_tick_lag_ns = max(self._stats.max_tick_lag_ns, tick_lag_ns)
                self._flush_streams()
            finally:
                self._stats.busy_ns += time.perf_counter_ns() - start

    def _flush_streams(self, *, stop: bool = True) -> None:
        if self.tripped or self._closed:
            return
        for stream in list(self._streams.values()):
            try:
                self._flush(stream)
            except Exception as exc:
                self._fail(stream.table, stream.instrument_id, exc, stop=stop)
                return

    def start_stats(self, now_ns: int) -> None:
        """Start the first stats span at ``now_ns`` (the flush actor's start)."""
        with self._lock:
            self._span_start_ns = now_ns
            self._stats = _Stats()

    def take_stats(self, now_ns: int) -> dict[str, object]:
        """Log ``recorder_writer_stats`` (and ``recorder_writer_busy`` when due) for the span ending
        at ``now_ns``, start a new span, and return the logged fields (AC-6)."""
        with self._lock:
            stats, span_ns = self._stats, now_ns - self._span_start_ns
            self._stats, self._span_start_ns = _Stats(), now_ns
        pct = busy_pct(stats.busy_ns, span_ns)
        fields: dict[str, object] = {
            "messages": stats.messages,
            "rows": stats.rows,
            "flushes": stats.flushes,
            "dropped": stats.dropped,
            "max_flush_ms": stats.max_flush_ns // MS_NS,
            "busy_pct": pct,
            "max_queue_depth": stats.max_queue_depth,
            "max_tick_lag_ms": stats.max_tick_lag_ns // MS_NS,
        }
        log.info("recorder writer stats", extra={"fields": {"code": "recorder_writer_stats", **fields}})
        if busy_warn(pct):
            log.warning("recorder writer busy", extra={"fields": {"code": "recorder_writer_busy", **fields}})
        return fields

    def flush_on_stop(self) -> None:
        """Write every held message, as the node stops (AC-5)."""
        with self._lock:
            self._flush_streams()

    def close(self) -> None:
        """Write what is held, then close every file (AC-5). Once tripped it writes nothing more,
        because a failed write may have left a torn batch; held rows are then lost (AC-4)."""
        with self._lock:
            if self._closed:
                return
            self._flush_streams(stop=False)
            self._closed = True
            for stream in self._streams.values():
                try:
                    stream.writer.close()
                except Exception as exc:
                    self._fail(stream.table, stream.instrument_id, exc, stop=False)
                try:
                    stream.file.close()
                except Exception as exc:
                    self._fail(stream.table, stream.instrument_id, exc, stop=False)

    def _fail(self, table: str, instrument_id: str | None, exc: Exception, *, stop: bool = True) -> None:
        first = not self.tripped
        self.tripped = True
        log.critical(
            "recorder feather write failed, stopping the recorder",
            extra={
                "fields": {
                    "code": "recorder_write_failed",
                    "table": table,
                    "instrument_id": instrument_id,
                    "error": repr(exc),
                }
            },
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        # At close the node is already gone; main.py exits 75 on ``tripped``.
        if first and stop:
            self._stop()
