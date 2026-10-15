"""covers: spec 0012 AC-1 (the same run folder as 1.231.0's StreamingFeatherWriter), AC-2 (a batch serializes
to the per object rows), AC-9 (the compactor converts it to the same Parquet hours)."""

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from feed_fixture import H0, HOUR_NS, SECOND_NS
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.data import CustomData, OrderBookDeltas
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer
from writer_fixture import (
    BTC,
    ETH,
    buffered,
    cache_with,
    drive,
    mixed_messages,
    read_feather,
    stream_files,
    streaming,
)

from recorder.compactor.main import Compactor
from recorder.compactor.store import CatalogStore

MINUTE_NS = 60 * SECOND_NS
START, END = H0 - 4 * MINUTE_NS, H0 + 4 * MINUTE_NS


def _both(tmp_path: Path, messages, *, eth: bool = True) -> tuple[Path, Path]:
    """Write ``messages`` through both writers on one clock; return the (old, new) run folders."""
    clock = TestClock()
    clock.set_time(messages[0][0] - SECOND_NS)
    old_run, new_run = tmp_path / "old" / "live" / "run", tmp_path / "new" / "live" / "run"
    cache = cache_with(BTC, ETH) if eth else cache_with(BTC)
    old = streaming(old_run, clock, cache)
    new = buffered(new_run, clock, cache)
    drive(clock, [old, new], messages)
    return old_run, new_run


def test_the_buffered_writer_writes_the_same_streams_as_1_231_0(tmp_path):
    old_run, new_run = _both(tmp_path, mixed_messages(START, END))
    old, new = stream_files(old_run), stream_files(new_run)

    assert set(old) == set(new)
    assert "order_book_deltas/ETHUSDT-PERP.BINANCE/ETHUSDT-PERP.BINANCE" in new
    assert "custom_open_interest_update/BTCUSDT-PERP.BINANCE/BTCUSDT-PERP.BINANCE" in new
    for key in old:
        if key == "crypto_perpetual":
            continue
        assert len(old[key]) == len(new[key]) == 2, key  # one rotation at H0
        for old_file, new_file in zip(old[key], new[key], strict=True):
            a, b = read_feather(old_file), read_feather(new_file)
            assert a.schema.metadata == b.schema.metadata, key
            assert a.equals(b), key
    # Fix 2: one new shared instrument file at the hour, not one per instrument; same rows in order.
    assert len(old["crypto_perpetual"]) == 3
    assert len(new["crypto_perpetual"]) == 2
    old_rows = pa.concat_tables([read_feather(p) for p in old["crypto_perpetual"]])
    new_rows = pa.concat_tables([read_feather(p) for p in new["crypto_perpetual"]])
    assert read_feather(new["crypto_perpetual"][0]).schema.metadata == {b"class": b"CryptoPerpetual"}
    assert old_rows.equals(new_rows)
    assert (new_run / "config.json").read_bytes() == b"{}"


def _per_object(objs, cls):
    return pa.concat_tables([ArrowSerializer.serialize_batch([o], data_cls=cls) for o in objs])


@pytest.mark.parametrize(
    "table",
    [
        "order_book_deltas",
        "trade_tick",
        "crypto_perpetual",
        "mark_price_update",
        "index_price_update",
        "funding_rate_update",
        "custom_open_interest_update",
        "custom_liquidation_tick",
    ],
)
def test_a_batch_serializes_to_the_per_object_rows(table):
    from nautilus_trader.persistence.funcs import class_to_filename

    messages = [obj for _, obj in mixed_messages(START, START + 3 * MINUTE_NS, eth=False)]
    picked = []
    for obj in messages:
        cls = obj.data_type.type if isinstance(obj, CustomData) else type(obj)
        if class_to_filename(cls) == table:
            picked.append(obj)
    assert len(picked) >= 1
    if table != "crypto_perpetual":
        picked = picked[:50]
        assert len(picked) >= 2, table
    cls = picked[0].data_type.type if isinstance(picked[0], CustomData) else type(picked[0])

    batch = ArrowSerializer.serialize_batch(picked, data_cls=cls)

    assert batch.equals(_per_object(picked, cls))
    if cls is OrderBookDeltas:
        assert batch.num_rows == sum(len(d.deltas) for d in picked)


def _parquet(root: Path) -> dict[str, pa.Table]:
    return {str(p.relative_to(root)): pq.read_table(p) for p in sorted(root.rglob("*.parquet"))}


def test_the_compactor_converts_both_run_folders_to_the_same_hours(tmp_path):
    start, end = H0 + 7 * SECOND_NS, H0 + HOUR_NS + 5 * MINUTE_NS
    old_run, new_run = _both(tmp_path, mixed_messages(start, end, eth=False), eth=False)
    later = time.time_ns() + 10 * HOUR_NS
    for run in (old_run, new_run):
        store = CatalogStore(run.parent.parent)
        Compactor(store, grace_ns=0, floor_bytes=0, offline=True).run_pass(later)

    old, new = _parquet(old_run.parent.parent), _parquet(new_run.parent.parent)

    assert any("order_book_deltas" in key for key in new)
    assert set(old) == set(new)
    for key in old:
        assert old[key].equals(new[key]), key
