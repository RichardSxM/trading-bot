"""covers: spec 0012 AC-2 (hold, tick, cap), AC-3 (rotation without drift, one shared instrument file),
AC-4 (fail closed, no instrument yet, exit 75), AC-5 (stop and close, close after a failed dispose),
AC-6 (stats, tick lag, slow flush)."""

import logging
import os
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from feed_fixture import H0, HOUR_NS, SECOND_NS
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from writer_fixture import BTC, ETH, buffered, cache_with, fake_kernel, read_feather, stream_files

import recorder.main as recorder_main
from recorder.disk import EXIT_DISK_FULL
from recorder.settings import RecorderSettings

MS = 1_000_000
DELTAS = "order_book_deltas/BTCUSDT-PERP.BINANCE/BTCUSDT-PERP.BINANCE"


def _deltas(ts, n=1):
    deltas = [TestDataStubs.order_book_delta(instrument_id=BTC.id, ts_event=ts, ts_init=ts) for _ in range(n)]
    return OrderBookDeltas(BTC.id, deltas)


def _clock(ts=H0 + SECOND_NS):
    clock = TestClock()
    clock.set_time(ts)
    return clock


def _codes(caplog):
    return [(r.levelname, r.fields["code"]) for r in caplog.records if hasattr(r, "fields")]


def _batches(path):
    with pa.OSFile(str(path)) as f:
        return list(pa.ipc.open_stream(f))


class _Stops:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def test_messages_are_held_until_the_tick_then_written_as_one_batch_in_order(tmp_path):
    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    for i in range(10):
        w.write(_deltas(H0 + SECOND_NS + i))
    (path,) = stream_files(tmp_path)[DELTAS]
    assert path.stat().st_size == 0  # nothing on disk before the tick

    w.flush_all(queue_depth=0, tick_lag_ns=0)

    batches = _batches(path)
    assert len(batches) == 1
    assert batches[0].column("ts_init").to_pylist() == [H0 + SECOND_NS + i for i in range(10)]


def test_a_stream_flushes_at_the_cap_without_a_tick(tmp_path):
    w = buffered(tmp_path, _clock(), cache_with(BTC))
    for i in range(4_096):
        w.write(_deltas(H0 + i))

    (path,) = stream_files(tmp_path)[DELTAS]
    assert read_feather(path).num_rows == 4_096


def test_a_quiet_streams_rows_are_on_disk_after_the_tick_and_an_empty_tick_writes_nothing(tmp_path):
    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    w.write(_deltas(H0 + SECOND_NS))
    w.flush_all(queue_depth=0, tick_lag_ns=0)
    (path,) = stream_files(tmp_path)[DELTAS]
    size, mtime = path.stat().st_size, path.stat().st_mtime_ns
    os.utime(path, ns=(mtime - SECOND_NS, mtime - SECOND_NS))
    w.take_stats(clock.timestamp_ns())

    w.write(_deltas(H0 + 2 * SECOND_NS))
    w.flush_all(queue_depth=0, tick_lag_ns=0)
    w.flush_all(queue_depth=0, tick_lag_ns=0)

    assert path.stat().st_size > size
    assert path.stat().st_mtime_ns > mtime - SECOND_NS
    assert w.take_stats(clock.timestamp_ns())["flushes"] == 1


def test_the_crossing_write_lands_in_the_old_file_and_the_next_boundary_is_the_whole_hour(tmp_path):
    clock = _clock(H0 - 10 * SECOND_NS)
    w = buffered(tmp_path, clock, cache_with(BTC))
    w.write(_deltas(H0 - 10 * SECOND_NS))
    clock.set_time(H0 + 7 * SECOND_NS)
    w.write(_deltas(H0 + 7 * SECOND_NS))  # crosses H0
    clock.set_time(H0 + HOUR_NS + SECOND_NS)
    w.write(_deltas(H0 + HOUR_NS + SECOND_NS))  # crosses H0 + 1 h, not H0 + 1 h + 7 s
    w.close()

    first, second, third = stream_files(tmp_path)[DELTAS]
    assert read_feather(first).column("ts_init").to_pylist() == [H0 - 10 * SECOND_NS, H0 + 7 * SECOND_NS]
    assert read_feather(second).column("ts_init").to_pylist() == [H0 + HOUR_NS + SECOND_NS]
    assert read_feather(third).num_rows == 0
    assert second.name.endswith(f"_{H0 + 7 * SECOND_NS}.feather")


def test_two_instrument_definitions_crossing_the_hour_give_one_new_shared_file(tmp_path):
    clock = _clock(H0 - 10 * SECOND_NS)
    w = buffered(tmp_path, clock, cache_with(BTC, ETH))
    w.write(BTC)
    w.write(ETH)
    clock.set_time(H0 + SECOND_NS)
    w.write(BTC)
    clock.set_time(H0 + 2 * SECOND_NS)
    w.write(ETH)
    w.close()

    files = stream_files(tmp_path)["crypto_perpetual"]
    assert len(files) == 2
    assert [read_feather(p).num_rows for p in files] == [3, 1]


def test_a_write_error_trips_stops_once_drops_later_writes_and_close_writes_nothing(tmp_path, caplog):
    stops = _Stops()
    w = buffered(tmp_path, _clock(), cache_with(BTC), stop=stops)
    w.write(_deltas(H0))

    class Boom:
        def write_table(self, _table):
            raise OSError("disk gone")

        def close(self):
            pass

    stream = w._streams[("order_book_deltas", "BTCUSDT-PERP.BINANCE")]
    stream.writer = Boom()
    with caplog.at_level(logging.INFO):
        w.flush_all(queue_depth=0, tick_lag_ns=0)
        w.write(_deltas(H0 + 1))
        w.write(_deltas(H0 + 2))
        w.flush_all(queue_depth=0, tick_lag_ns=0)
        w.close()

    assert w.tripped
    assert stops.calls == 1
    assert _codes(caplog) == [("CRITICAL", "recorder_write_failed")]
    fields = caplog.records[0].fields
    assert fields["table"] == "order_book_deltas"
    assert fields["instrument_id"] == "BTCUSDT-PERP.BINANCE"
    assert "disk gone" in fields["error"]
    assert w.take_stats(H0 + 60 * SECOND_NS)["dropped"] == 2
    assert stream.held == []


def test_an_error_while_routing_a_message_also_trips_and_stops_once(tmp_path, caplog, monkeypatch):
    # covers: AC-4 (any error while writing fails closed, routing included)
    stops = _Stops()
    w = buffered(tmp_path, _clock(), cache_with(BTC), stop=stops)

    def boom(_cls):
        raise ValueError("bad table")

    monkeypatch.setattr("recorder.writer.class_to_filename", boom)
    with caplog.at_level(logging.INFO):
        w.write(_deltas(H0))
        w.write(_deltas(H0 + 1))

    assert w.tripped
    assert stops.calls == 1
    assert _codes(caplog) == [("CRITICAL", "recorder_write_failed")]
    fields = caplog.records[0].fields
    assert fields["table"] == "OrderBookDeltas"
    assert fields["instrument_id"] is None
    assert "bad table" in fields["error"]
    assert w.take_stats(H0 + 60 * SECOND_NS)["dropped"] == 1


def test_messages_before_the_instrument_is_cached_are_dropped_with_one_warning(tmp_path, caplog):
    cache = cache_with()
    w = buffered(tmp_path, _clock(), cache)
    with caplog.at_level(logging.INFO):
        w.write(_deltas(H0))
        w.write(_deltas(H0 + 1))
        cache.add_instrument(BTC)
        w.write(_deltas(H0 + 2))
    w.close()

    assert _codes(caplog) == [("WARNING", "recorder_write_no_instrument")]
    (path,) = stream_files(tmp_path)[DELTAS]
    assert read_feather(path).column("ts_init").to_pylist() == [H0 + 2]
    assert not w.tripped


def test_stop_and_close_write_what_is_held_and_a_later_write_is_dropped_once(tmp_path, caplog):
    w = buffered(tmp_path, _clock(), cache_with(BTC))
    w.write(_deltas(H0))
    w.flush_on_stop()
    w.write(_deltas(H0 + 1))
    with caplog.at_level(logging.INFO):
        w.close()
        w.write(_deltas(H0 + 2))
        w.write(_deltas(H0 + 3))
        w.close()

    (path,) = stream_files(tmp_path)[DELTAS]
    assert read_feather(path).column("ts_init").to_pylist() == [H0, H0 + 1]
    assert _codes(caplog) == [("WARNING", "recorder_write_after_close")]


def test_stats_report_busy_time_queue_depth_and_tick_lag(tmp_path, caplog):
    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    w.start_stats(H0)
    w.write(_deltas(H0, n=2))
    w.write(TestDataStubs.trade_tick(instrument=BTC))
    w.flush_all(queue_depth=120, tick_lag_ns=40 * MS)
    w.flush_all(queue_depth=7, tick_lag_ns=3 * MS)
    w._stats.busy_ns = 15 * SECOND_NS
    with caplog.at_level(logging.INFO):
        fields = w.take_stats(H0 + 60 * SECOND_NS)

    assert fields["messages"] == 2
    assert fields["rows"] == 3
    assert fields["flushes"] == 2
    assert fields["busy_pct"] == 25
    assert fields["max_queue_depth"] == 120
    assert fields["max_tick_lag_ms"] == 40
    assert _codes(caplog) == [("INFO", "recorder_writer_stats"), ("WARNING", "recorder_writer_busy")]

    w._stats.busy_ns = 14 * SECOND_NS
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert w.take_stats(H0 + 120 * SECOND_NS)["busy_pct"] == 23
    assert _codes(caplog) == [("INFO", "recorder_writer_stats")]


def test_a_slow_flush_warns_once_a_minute(tmp_path, caplog, monkeypatch):
    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    ticks = iter(range(0, 10**12, 300 * MS))
    monkeypatch.setattr("recorder.writer.time.perf_counter_ns", lambda: next(ticks))
    with caplog.at_level(logging.INFO):
        for step in range(3):
            clock.set_time(H0 + SECOND_NS + step * 20 * SECOND_NS)
            w.write(_deltas(clock.timestamp_ns()))
            w.flush_all(queue_depth=0, tick_lag_ns=0)

    assert _codes(caplog) == [("WARNING", "recorder_flush_slow")]
    assert caplog.records[0].fields["messages"] == 1


def test_an_error_at_close_logs_and_trips_without_stopping_the_gone_node(tmp_path, caplog):
    # covers: AC-5 (an error during close logs recorder_write_failed and leads to exit 75)
    stops = _Stops()
    w = buffered(tmp_path, _clock(), cache_with(BTC), stop=stops)
    w.write(_deltas(H0))
    stream = w._streams[("order_book_deltas", "BTCUSDT-PERP.BINANCE")]
    real = stream.writer

    class FailsAtClose:
        def write_table(self, table):
            real.write_table(table)

        def close(self):
            real.close()
            raise OSError("close failed")

    stream.writer = FailsAtClose()
    with caplog.at_level(logging.INFO):
        w.close()

    assert w.tripped
    assert stops.calls == 0  # at close the node is already gone; main.py exits 75 on tripped
    assert _codes(caplog) == [("CRITICAL", "recorder_write_failed")]
    assert "close failed" in caplog.records[0].fields["error"]
    (path,) = stream_files(tmp_path)[DELTAS]
    assert read_feather(path).column("ts_init").to_pylist() == [H0]  # the held row was flushed first


def _flush_actor(writer, clock, *, depth=0):
    from nautilus_trader.test_kit.stubs.component import TestComponentStubs

    from recorder.actor import WriterFlushActor, WriterFlushConfig

    actor = WriterFlushActor(WriterFlushConfig(flush_ms=1_000), writer=writer, probe=lambda: depth)
    actor.register_base(
        portfolio=TestComponentStubs.portfolio(),
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=clock,
    )
    return actor


def test_the_flush_actor_ticks_on_the_node_clock_and_flushes_what_is_held_at_stop(tmp_path):
    # covers: AC-2 (the flush tick every RECORDER_FLUSH_MS), AC-5 (on_stop flushes every stream)
    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    actor = _flush_actor(w, clock, depth=9)
    actor.start()
    w.write(_deltas(H0 + SECOND_NS))
    (path,) = stream_files(tmp_path)[DELTAS]
    assert path.stat().st_size == 0

    for handler in clock.advance_time(H0 + 2 * SECOND_NS):
        handler.handle()
    assert read_feather(path).column("ts_init").to_pylist() == [H0 + SECOND_NS]

    w.write(_deltas(H0 + 2 * SECOND_NS + 1))
    actor.stop()
    assert clock.timer_names == []
    assert read_feather(path).column("ts_init").to_pylist() == [H0 + SECOND_NS, H0 + 2 * SECOND_NS + 1]
    assert w.take_stats(H0 + 3 * SECOND_NS)["max_queue_depth"] == 9


def test_the_flush_actor_passes_the_ticks_lag_behind_its_schedule(tmp_path):
    # covers: AC-6 (max_tick_lag_ms is the tick's actual time minus its scheduled time)
    from nautilus_trader.common.events import TimeEvent
    from nautilus_trader.core.uuid import UUID4

    clock = _clock()
    w = buffered(tmp_path, clock, cache_with(BTC))
    actor = _flush_actor(w, clock)
    due = H0 + 2 * SECOND_NS
    actor._flush(TimeEvent("writer_flush", UUID4(), due, due + 40 * MS))
    actor._flush(TimeEvent("writer_flush", UUID4(), due, due - 5 * MS))  # never negative

    assert w.take_stats(H0 + 3 * SECOND_NS)["max_tick_lag_ms"] == 40


class _WriterNode:
    """Stands in for ``TradingNode`` in ``main``: ``run`` writes one held message, then trips the writer
    or not, and ``dispose`` may raise."""

    def __init__(self, config, *, trip: bool, dispose_raises: bool):
        self.handlers: list[Any] = []
        self.actors: list[object] = []
        self.trader = SimpleNamespace(
            add_actor=self.actors.append, subscribe=lambda topic, handler: self.handlers.append(handler)
        )
        self.kernel = fake_kernel()
        self.kernel.cache.add_instrument(BTC)
        self._trip, self._dispose_raises = trip, dispose_raises

    def add_data_client_factory(self, *a):
        pass

    def build(self):
        pass

    def run(self):
        (write,) = self.handlers
        write(_deltas(H0))
        if self._trip:
            write.__self__.tripped = True

    def dispose(self):
        if self._dispose_raises:
            raise RuntimeError("dispose failed")


def _main(monkeypatch, tmp_path, **node_kwargs):
    settings = RecorderSettings(trading_env="demo", catalog_path=str(tmp_path))
    monkeypatch.setattr(recorder_main, "boot", lambda *a, **k: SimpleNamespace(settings=settings))
    monkeypatch.setattr(recorder_main, "wait_for_space", lambda path, floor: None)
    monkeypatch.setattr(recorder_main, "TradingNode", lambda config: _WriterNode(config, **node_kwargs))
    recorder_main.main()


def test_main_closes_the_writer_even_when_dispose_raises(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="dispose failed"):
        _main(monkeypatch, tmp_path, trip=False, dispose_raises=True)

    run = tmp_path / "live" / "run-1"
    assert (run / "config.json").exists()
    (path,) = stream_files(run)[DELTAS]
    assert read_feather(path).column("ts_init").to_pylist() == [H0]


def test_main_exits_75_once_the_writer_tripped(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as info:
        _main(monkeypatch, tmp_path, trip=True, dispose_raises=False)

    assert info.value.code == EXIT_DISK_FULL
