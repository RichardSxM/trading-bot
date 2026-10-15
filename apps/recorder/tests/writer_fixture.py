"""Messages and helpers for the buffered writer tests (spec 0012): every recorded type, both writers on
one clock, and a way to read a run folder back as streams."""

from collections import defaultdict
from datetime import time
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs; only Timedelta for the old writer
import pyarrow as pa
from feed_fixture import (
    SECOND_NS,
    liquidation,
    make_feed,
    make_funding,
    make_open_interest,
    make_prices,
    make_trades,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.data import (
    IndexPriceUpdate,
    MarkPriceUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.persistence.writer import RotationMode, StreamingFeatherWriter
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from recorder.main import RECORDED_TYPES
from recorder.writer import BufferedFeatherWriter

BTC = TestInstrumentProvider.btcusdt_perp_binance()
ETH = TestInstrumentProvider.ethusdt_perp_binance()
ETH_ID = str(ETH.id)


def cache_with(*instruments) -> Cache:
    cache = Cache()
    for instrument in instruments:
        cache.add_instrument(instrument)
    return cache


def buffered(path: Path, clock, cache, *, cap: int = 4_096, stop=lambda: None) -> BufferedFeatherWriter:
    return BufferedFeatherWriter(
        path,
        config_json=b"{}",
        cache=cache,
        clock=clock,
        include_types=RECORDED_TYPES,
        flush_max_messages=cap,
        stop=stop,
    )


def streaming(path: Path, clock, cache) -> StreamingFeatherWriter:
    """The kernel's writer as spec 0007 configured it, the parity reference."""
    return StreamingFeatherWriter(
        path=str(path),
        cache=cache,
        clock=clock,
        include_types=RECORDED_TYPES,
        rotation_mode=RotationMode.SCHEDULED_DATES,
        rotation_interval=pd.Timedelta(hours=1),
        rotation_time=time(0, 0),
        rotation_timezone="UTC",
    )


def fake_kernel():
    """The parts of ``node.kernel`` that ``main.py`` hands the writer, for a fake ``TradingNode``."""
    from types import SimpleNamespace

    clock = TestClock()
    clock.set_time(1_791_439_200 * SECOND_NS)
    return SimpleNamespace(instance_id="run-1", cache=Cache(), clock=clock)


def deltas_of(message) -> OrderBookDeltas:
    """A feed message (pyo3 deltas) as the ``OrderBookDeltas`` batch the data engine publishes."""
    deltas = OrderBookDelta.from_pyo3_list(message)
    return OrderBookDeltas(deltas[0].instrument_id, deltas)


def mixed_messages(start_ns: int, end_ns: int, *, eth: bool = True) -> list[tuple[int, Any]]:
    """``(clock time, message)`` for every recorded type from ``start_ns`` to ``end_ns``, in time order.

    The instruments come first, and again (a reload) a second and two seconds after each whole hour.
    """
    out: list[tuple[int, Any]] = [(start_ns, BTC)]
    out += [(m[0].ts_init, deltas_of(m)) for m in make_feed(start_ns, end_ns).messages]
    out += [(t.ts_init, t) for t in TradeTick.from_pyo3_list(make_trades(start_ns, end_ns))]
    out += [(r.ts_init, r) for r in make_prices(MarkPriceUpdate, start_ns, end_ns)]
    out += [(r.ts_init, r) for r in make_prices(IndexPriceUpdate, start_ns, end_ns, every_ns=3 * SECOND_NS)]
    out += [(r.ts_init, r) for r in make_funding(start_ns, end_ns)]
    # ``publish_data`` puts the context object itself on the bus, not a ``CustomData`` wrapper.
    out += [(r.ts_init, r) for r in make_open_interest(start_ns, end_ns)]
    out += [(ts, liquidation(ts, f"liq{ts}")) for ts in range(start_ns + 5 * SECOND_NS, end_ns, 41 * SECOND_NS)]
    if eth:
        out.append((start_ns, ETH))
        feed = make_feed(start_ns, end_ns, seed=8, instrument=ETH_ID)
        out += [(m[0].ts_init, deltas_of(m)) for m in feed.messages]
    hour = start_ns - start_ns % (3_600 * SECOND_NS) + 3_600 * SECOND_NS
    while hour < end_ns:
        out.append((hour + SECOND_NS, BTC))
        if eth:
            out.append((hour + 2 * SECOND_NS, ETH))
        hour += 3_600 * SECOND_NS
    out.sort(key=lambda item: item[0])  # stable: equal times keep their order
    return out


def drive(clock: TestClock, writers, messages, *, flush_every_ns: int = SECOND_NS) -> None:
    """Write ``messages`` through each writer on ``clock``, running a buffered writer's flush tick
    once per ``flush_every_ns`` of clock time, then close them."""
    next_tick: int | None = None
    for ts, obj in messages:
        clock.set_time(max(ts, clock.timestamp_ns()))
        if next_tick is None:
            next_tick = ts - ts % flush_every_ns + flush_every_ns
        while ts >= next_tick:
            for w in writers:
                if isinstance(w, BufferedFeatherWriter):
                    w.flush_all(queue_depth=0, tick_lag_ns=0)
            next_tick += flush_every_ns
        for w in writers:
            w.write(obj)
    for w in writers:
        w.close()


def read_feather(path: Path) -> pa.Table:
    with pa.OSFile(str(path)) as f:
        return pa.ipc.open_stream(f).read_all()


def stream_files(run: Path) -> dict[str, list[Path]]:
    """The run's feather files per stream (relative folder plus name without ``_<ns>``), in creation order."""
    found: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for p in run.rglob("*.feather"):
        name, ns = p.stem.rsplit("_", 1)
        found[str(p.relative_to(run).parent / name)].append((int(ns), p))
    return {key: [p for _, p in sorted(files)] for key, files in found.items()}
