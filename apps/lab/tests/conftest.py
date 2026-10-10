"""Lab test helpers: a clean environment, the committed fixture catalog, and tiny synthetic hour files."""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nautilus_trader.core import nautilus_pyo3 as p3

from lab.catalog import DELTAS_TABLE, TRADES_TABLE, SourceTree
from lab.settings import LabSettings
from trading_core.recording.rows import RunBundle
from trading_core.secrets import reset_registry

FIXTURES = Path(__file__).parent / "fixtures"
INSTRUMENT = "PF_XBTUSD.KRAKEN"
# 2026-10-08 17:00 UTC, the fixture's first hour (its data runs 17:55 to 18:05).
H17 = 1_791_478_800_000_000_000
HOUR = 3_600_000_000_000
F_SNAPSHOT = 32
F_LAST = 128

_ENV_PREFIXES = (
    "TRADING_ENV",
    "PROCESS_NAME",
    "LOG_LEVEL",
    "CLOCK_SERVERS",
    "POSTGRES_",
    "GIT_COMMIT",
    "SECRETS_DIR",
    "LAB_",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with none of our settings in it."""
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name)
    reset_registry()
    yield
    reset_registry()


def lab_settings(catalog: Path, reports: Path) -> LabSettings:
    """Demo settings pointing at a catalog root and a reports folder."""
    return LabSettings(trading_env="demo", lab_catalog_dir=catalog, lab_reports_dir=reports)


@dataclass(frozen=True)
class Row:
    """One synthetic delta: action and side as the pyo3 enums, price and size as text."""

    action: p3.BookAction
    side: p3.OrderSide
    price: str
    size: str
    ts: int
    flags: int = 0


def snapshot(ts: int, levels: Sequence[tuple[p3.OrderSide, str, str]]) -> list[Row]:
    """A compactor style snapshot block: ``CLEAR``, one ``ADD`` per level, ``F_LAST`` on the last row."""
    rows = [Row(p3.BookAction.CLEAR, p3.OrderSide.NO_ORDER_SIDE, "0", "0", ts, F_SNAPSHOT)]
    rows += [Row(p3.BookAction.ADD, side, price, size, ts, F_SNAPSHOT) for side, price, size in levels]
    last = rows[-1]
    rows[-1] = Row(last.action, last.side, last.price, last.size, last.ts, F_SNAPSHOT | F_LAST)
    return rows


def _size(text: str) -> p3.Quantity:
    # The instrument's size precision is 4; every row of a batch must share it.
    return p3.Quantity.from_str(f"{Decimal(text):.4f}")


def write_hour(root: Path, hour: int, rows: Sequence[Row], trades: Sequence[tuple[str, str, int]] = ()) -> SourceTree:
    """Write one hour of deltas and trades (``(price, size, ts)``) under ``root`` in the catalog layout."""
    tree = SourceTree(root, INSTRUMENT)
    instrument = p3.InstrumentId.from_str(INSTRUMENT)
    deltas = [
        p3.OrderBookDelta(
            instrument,
            row.action,
            p3.BookOrder(row.side, p3.Price.from_str(row.price), _size(row.size), 0),
            row.flags,
            n,
            row.ts,
            row.ts,
        )
        for n, row in enumerate(rows)
    ]
    path = tree.hour_file(DELTAS_TABLE, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.ipc.open_stream(pa.py_buffer(p3.book_deltas_to_arrow_record_batch_bytes(deltas))).read_all()
    pq.write_table(table, path)
    ticks = [
        p3.TradeTick(
            instrument,
            p3.Price.from_str(price),
            _size(size),
            p3.AggressorSide.BUYER,
            p3.TradeId(f"T{n}"),
            ts,
            ts,
        )
        for n, (price, size, ts) in enumerate(trades)
    ]
    path = tree.hour_file(TRADES_TABLE, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    if ticks:
        table = pa.ipc.open_stream(pa.py_buffer(p3.trades_to_arrow_record_batch_bytes(ticks))).read_all()
        pq.write_table(table, path)
    return tree


class MemoryStore:
    """A replay ``Store`` that keeps the bundles in memory, in the order they were written."""

    def __init__(self) -> None:
        self.bundles: list[RunBundle] = []

    def prepare(self, instrument, strategy, git_commit, config):
        return uuid4()

    def write_bundle(self, bundle) -> None:
        self.bundles.append(bundle)


def worker_settings(catalog: Path, reports: Path, workers: int, **extra) -> LabSettings:
    """``lab_settings`` with a worker count (and any other ``LabSettings`` field)."""
    return LabSettings(
        trading_env="demo", lab_catalog_dir=catalog, lab_reports_dir=reports, lab_workers=workers, **extra
    )
