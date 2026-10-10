"""Rebuild the lab's committed test catalog from two real own recorder hours (spec 0008, *Fixtures*).

Takes 2026-10-08 17:00 and 18:00 UTC of ``PF_XBTUSD.KRAKEN`` from ``catalog-lab/own`` and keeps
17:55 to 18:05, so the two hour files are back to back across the hour boundary:

- 17:00 file: the book carried to 17:55 as a compactor style snapshot block (``BookCarry``'s
  layout: one ``CLEAR``, one ``ADD`` per level, ``F_SNAPSHOT``, ``F_LAST`` on the last row), then
  the real deltas and trades of 17:55:00 to 17:59:59.999.
- 18:00 file: the real compactor snapshot and deltas and trades of 18:00:00 to 18:04:59.999.

It also writes the day's manifest (no incomplete hours) and no gap file. Run in the lab image:

  docker compose run --rm --no-deps -T --entrypoint python \\
    -v "$PWD/catalog-lab:/catalog-lab:ro" -v "$PWD/apps/lab/tests/fixtures:/out" \\
    lab /out/make_fixtures.py /catalog-lab/own /out/own
"""

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.persistence.catalog.parquet import _timestamps_to_filename
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

INSTRUMENT = "PF_XBTUSD.KRAKEN"
DAY = "2026-10-08"
S = 1_000_000_000
H17 = 1_791_478_800 * S
H18 = H17 + 3600 * S
CUT = H17 + 55 * 60 * S
END = H18 + 5 * 60 * S
F_LAST = 128
F_SNAPSHOT = 32


def name(hour: int) -> str:
    return _timestamps_to_filename(hour, hour + 3600 * S - 1)


def rows_between(table: pa.Table, start: int, end: int) -> pa.Table:
    ts = table.column("ts_init")
    return table.filter(pc.and_(pc.greater_equal(ts, start), pc.less(ts, end)))


def snapshot_at(table: pa.Table, cut: int) -> pa.Table:
    """The book after every row before ``cut``, as snapshot rows stamped ``cut``."""
    book = p3.OrderBook(p3.InstrumentId.from_str(INSTRUMENT), p3.BookType.L2_MBP)
    for batch in rows_between(table, 0, cut).to_batches(max_chunksize=200_000):
        piece = pa.Table.from_batches([batch]).replace_schema_metadata(table.schema.metadata)
        for delta in ArrowSerializer.deserialize(OrderBookDelta, piece):
            book.apply_delta(delta)
    meta = table.schema.metadata
    price_precision, size_precision = int(meta[b"price_precision"]), int(meta[b"size_precision"])
    orders = [p3.BookOrder(p3.OrderSide.NO_ORDER_SIDE, p3.Price(0, price_precision), p3.Quantity(0, size_precision), 0)]
    actions = [p3.BookAction.CLEAR]
    for level in [*book.bids(), *book.asks()]:
        first = level.first()
        if first is not None:
            orders.append(p3.BookOrder(first.side, first.price, first.size, 0))
            actions.append(p3.BookAction.ADD)
    last = len(orders) - 1
    instrument = p3.InstrumentId.from_str(INSTRUMENT)
    deltas = [
        p3.OrderBookDelta(instrument, a, o, F_SNAPSHOT | (F_LAST if i == last else 0), book.sequence, cut, cut)
        for i, (a, o) in enumerate(zip(actions, orders, strict=True))
    ]
    data = p3.book_deltas_to_arrow_record_batch_bytes(deltas)
    return pa.ipc.open_stream(pa.py_buffer(data)).read_all().replace_schema_metadata(meta)


def write(table: pa.Table, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd", row_group_size=65_536)
    return path.stat().st_size


def main(source: Path, out: Path) -> None:
    deltas_dir = Path("data/order_book_deltas") / INSTRUMENT
    trades_dir = Path("data/trade_tick") / INSTRUMENT
    d17 = pq.read_table(source / deltas_dir / name(H17))
    d18 = pq.read_table(source / deltas_dir / name(H18))
    t17 = pq.read_table(source / trades_dir / name(H17))
    t18 = pq.read_table(source / trades_dir / name(H18))

    hour17 = pa.concat_tables([snapshot_at(d17, CUT), rows_between(d17, CUT, H18).cast(d17.schema)])
    hour18 = rows_between(d18, H18, END)
    streams = {
        "order_book_deltas": [(H17, hour17), (H18, hour18)],
        "trade_tick": [(H17, rows_between(t17, CUT, H18)), (H18, rows_between(t18, H18, END))],
    }
    manifest_streams = []
    total = 0
    for table_name, hours in streams.items():
        rows = size = 0
        for hour, table in hours:
            size += write(table, out / "data" / table_name / INSTRUMENT / name(hour))
            rows += table.num_rows
        total += size
        manifest_streams.append(
            {
                "instrument_id": INSTRUMENT,
                "type": table_name,
                "rows": rows,
                "bytes": size,
                "hours": len(hours),
                "late_rows": 0,
                "incomplete_hours": [],
            }
        )
    manifest = {"date": DAY, "total_bytes": total, "streams": manifest_streams, "gaps": []}
    (out / "manifests").mkdir(parents=True, exist_ok=True)
    (out / "manifests" / f"{DAY}.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
