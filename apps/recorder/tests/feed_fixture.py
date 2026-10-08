"""A synthetic L2 feed written the way the recorder writes it, for the compactor tests (spec 0007).

One Arrow batch per feed message, the stream schema with the instrument and precision metadata,
a ``CLEAR`` with ``F_SNAPSHOT`` first, and ``F_LAST`` on the last row of every message.
"""

import os
import random
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.instruments import CryptoPerpetual

from recorder.compactor.convert import F_LAST, F_SNAPSHOT

SECOND_NS = 1_000_000_000
HOUR_NS = 3_600 * SECOND_NS
# 2026-10-08 06:00:00 UTC
H0 = 1_791_439_200 * SECOND_NS
INSTRUMENT = "BTCUSDT-PERP.BINANCE"  # TestInstrumentProvider.btcusdt_perp_binance()
PRICE_PRECISION = 1
SIZE_PRECISION = 3


@dataclass
class Feed:
    """The messages of a synthetic feed: each a list of pyo3 deltas sharing one ``ts_init``."""

    messages: list[list[p3.OrderBookDelta]] = field(default_factory=list)


def _delta(action, side, price, size, flags, seq, ts):
    order = p3.BookOrder(side, p3.Price(price, PRICE_PRECISION), p3.Quantity(size, SIZE_PRECISION), 0)
    return p3.OrderBookDelta(p3.InstrumentId.from_str(INSTRUMENT), action, order, flags, seq, ts, ts)


def make_feed(start_ns: int, end_ns: int, seed: int = 7, step_ms: tuple[int, int] = (200, 900)) -> Feed:
    """A random walk L2 book from ``start_ns`` until ``end_ns``, starting with a feed snapshot."""
    rng = random.Random(seed)
    bids = {80_000.0 - i * 0.5: round(rng.uniform(0.01, 2), 3) for i in range(1, 30)}
    asks = {80_000.0 + i * 0.5: round(rng.uniform(0.01, 2), 3) for i in range(1, 30)}
    seq = 1
    ts = start_ns
    snapshot = [_delta(p3.BookAction.CLEAR, p3.OrderSide.NO_ORDER_SIDE, 0, 0, F_SNAPSHOT, seq, ts)]
    for side, levels in ((p3.OrderSide.BUY, bids), (p3.OrderSide.SELL, asks)):
        for price, size in levels.items():
            seq += 1
            snapshot.append(_delta(p3.BookAction.ADD, side, price, size, 0, seq, ts))
    snapshot[-1] = _with_flags(snapshot[-1], F_LAST)
    feed = Feed([snapshot])
    while True:
        ts += rng.randint(*step_ms) * 1_000_000
        if ts >= end_ns:
            return feed
        message = []
        for _ in range(rng.randint(1, 3)):
            seq += 1
            side, levels, best = (
                (p3.OrderSide.BUY, bids, max(bids)) if rng.random() < 0.5 else (p3.OrderSide.SELL, asks, min(asks))
            )
            sign = -1 if side == p3.OrderSide.BUY else 1
            price = best + sign * 0.5 * rng.randint(0, 15)
            roll = rng.random()
            if price in levels and roll < 0.3 and len(levels) > 5:
                del levels[price]
                message.append(_delta(p3.BookAction.DELETE, side, price, 0, 0, seq, ts))
            else:
                action = p3.BookAction.UPDATE if price in levels else p3.BookAction.ADD
                levels[price] = round(rng.uniform(0.01, 2), 3)
                message.append(_delta(action, side, price, levels[price], 0, seq, ts))
        message[-1] = _with_flags(message[-1], F_LAST)
        feed.messages.append(message)


def _with_flags(delta, flags):
    return p3.OrderBookDelta(
        delta.instrument_id,
        delta.action,
        delta.order,
        delta.flags | flags,
        delta.sequence,
        delta.ts_event,
        delta.ts_init,
    )


def _schema_metadata() -> dict[bytes, bytes]:
    return {
        b"instrument_id": INSTRUMENT.encode(),
        b"price_precision": str(PRICE_PRECISION).encode(),
        b"size_precision": str(SIZE_PRECISION).encode(),
    }


def _table(batch_bytes: bytes):
    table = pa.ipc.open_stream(pa.py_buffer(batch_bytes)).read_all()
    return table.replace_schema_metadata(_schema_metadata())


def write_run(live: Path, run: str, messages: list[list[p3.OrderBookDelta]], rotate_at: list[int]) -> list[Path]:
    """Write delta messages as the recorder does: one batch per message, rotating at each time."""
    items = [(m[0].ts_init, _table(p3.book_deltas_to_arrow_record_batch_bytes(m))) for m in messages]
    return write_stream(live / run / "order_book_deltas" / INSTRUMENT, items, rotate_at)


def make_trades(start_ns: int, end_ns: int, seed: int = 11) -> list[p3.TradeTick]:
    """Trades every few seconds."""
    rng = random.Random(seed)
    trades: list[p3.TradeTick] = []
    ts = start_ns
    n = 0
    while True:
        ts += rng.randint(1, 8) * SECOND_NS
        if ts >= end_ns:
            return trades
        n += 1
        side = p3.AggressorSide.BUYER if rng.random() < 0.5 else p3.AggressorSide.SELLER
        trades.append(
            p3.TradeTick(
                p3.InstrumentId.from_str(INSTRUMENT),
                p3.Price(80_000 + rng.randint(-20, 20) * 0.5, PRICE_PRECISION),
                p3.Quantity(round(rng.uniform(0.001, 1), 3), SIZE_PRECISION),
                side,
                p3.TradeId(str(n)),
                ts,
                ts,
            )
        )


def write_trades(live: Path, run: str, trades: list[p3.TradeTick], rotate_at: list[int]) -> list[Path]:
    """Write trades as the recorder does: one batch per trade, rotating at each time."""
    items = [(t.ts_init, _table(p3.trades_to_arrow_record_batch_bytes([t]))) for t in trades]
    return write_stream(live / run / "trade_tick" / INSTRUMENT, items, rotate_at)


def write_instruments(live: Path, run: str, instruments: list[CryptoPerpetual], created_ns: int) -> Path:
    """An instrument file in the run folder, with the venue list (ours and others)."""
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    path = live / run / f"crypto_perpetual_{created_ns}.feather"
    path.parent.mkdir(parents=True, exist_ok=True)
    batches = [ArrowSerializer.serialize(i) for i in instruments]
    with pa.OSFile(str(path), "wb") as sink:
        writer = pa.ipc.new_stream(sink, batches[0].schema)
        for batch in batches:
            writer.write_batch(batch)
        writer.close()
    return path


def write_stream(folder: Path, items: list[tuple[int, pa.Table]], rotate_at: list[int]) -> list[Path]:
    """Write ``(ts, table)`` items as the recorder does: one batch per item, a new file at each rotation.

    Like the real writer, the file rotates on the first item at or after each rotation time,
    and that item is the last one written to the old file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    os.utime(folder.parent.parent)
    paths: list[Path] = []
    writer = sink = None
    pending_rotations = sorted(rotate_at)

    def open_file(created: int):
        nonlocal writer, sink
        path = folder / f"{folder.name}_{created}.feather"
        paths.append(path)
        sink = pa.OSFile(str(path), "wb")
        writer = None

    open_file(items[0][0])
    for ts, table in items:
        if writer is None:
            writer = pa.ipc.new_stream(sink, table.schema)
        for batch in table.to_batches():
            writer.write_batch(batch)
        if pending_rotations and ts >= pending_rotations[0]:
            pending_rotations.pop(0)
            assert writer is not None
            assert sink is not None
            writer.close()
            sink.close()
            open_file(ts + 1)
    if writer is not None:
        writer.close()
    assert sink is not None
    sink.close()
    return paths
