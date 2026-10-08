"""covers: spec 0007 AC-1 (only the recorded types are written, one new file per stream each UTC hour)."""

from pathlib import Path

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.persistence.writer import StreamingFeatherWriter
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs

from recorder.main import build_config
from recorder.settings import RecorderSettings

SECOND_NS = 1_000_000_000
# 2026-10-08 06:00:00 UTC
H = 1_791_439_200 * SECOND_NS


def _writer(tmp_path: Path, clock: TestClock, cache: Cache) -> StreamingFeatherWriter:
    streaming = build_config(RecorderSettings(trading_env="demo", catalog_path=str(tmp_path))).streaming
    assert streaming is not None
    return StreamingFeatherWriter(
        path=str(tmp_path / "run"),
        cache=cache,
        clock=clock,
        include_types=streaming.include_types,
        rotation_mode=streaming.rotation_mode,
        rotation_interval=streaming.rotation_interval,
        rotation_time=streaming.rotation_time,
        rotation_timezone=streaming.rotation_timezone,
    )


def _deltas(instrument_id, ts) -> OrderBookDeltas:
    # The data engine publishes deltas as OrderBookDeltas batches; a lone delta is not recorded.
    delta = TestDataStubs.order_book_delta(instrument_id=instrument_id, ts_event=ts, ts_init=ts)
    return OrderBookDeltas(instrument_id, [delta])


def test_only_the_recorded_types_get_files_and_deltas_rotate_at_the_hour(tmp_path):
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    cache = Cache()
    cache.add_instrument(instrument)
    clock = TestClock()
    clock.set_time(H - 10 * SECOND_NS)
    writer = _writer(tmp_path, clock, cache)

    writer.write(_deltas(instrument.id, H - 10 * SECOND_NS))
    writer.write(TestDataStubs.quote_tick(instrument=instrument))  # not a recorded type
    clock.set_time(H + SECOND_NS)
    writer.write(_deltas(instrument.id, H + SECOND_NS))
    writer.close()

    run = tmp_path / "run"
    files = sorted(p.relative_to(run).parts[0] for p in run.rglob("*.feather"))
    assert set(files) <= {"order_book_deltas", "trade_tick", f"crypto_perpetual_{H - 10 * SECOND_NS}.feather"}
    delta_files = list((run / "order_book_deltas").rglob("*.feather"))
    assert len(delta_files) == 2
    assert not (run / "quote_tick").exists()
