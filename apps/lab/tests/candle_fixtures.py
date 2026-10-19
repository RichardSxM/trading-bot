"""A seeded synthetic candle month for the look ahead, determinism, failure, and parity tests (spec 0014)."""

import random
from decimal import Decimal
from pathlib import Path

from lab.candle_fetch import candle_tree
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month, MonthManifest
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
FEB = Month(2026, 2)
DAY = 24 * HOUR_NS
# One quiet minute (flat at the previous close) every this many minutes, and one 10 minute hole.
QUIET_EVERY = 997
HOLE = range(5000, 5010)


def walk(seed: int, minutes: int, start_ns: int, first: int = 60000) -> list[CandleRow]:
    """A random walk of 1 minute candles on whole ticks from ``start_ns``, with quiet minutes and one hole."""
    rng = random.Random(seed)
    rows: list[CandleRow] = []
    close = D(first)
    for m in range(minutes):
        ts = start_ns + (m + 1) * MINUTE_NS
        if m in HOLE:
            continue
        if m % QUIET_EVERY == QUIET_EVERY - 1:
            rows.append(CandleRow(ts, close, close, close, close, D(0), True))
            continue
        o = close
        close = max(D(1000), o + rng.randint(-40, 40))
        high = max(o, close) + rng.randint(0, 15)
        low = min(o, close) - rng.randint(0, 15)
        rows.append(CandleRow(ts, o, high, low, close, D("0.5"), False))
    return rows


def synthetic_month(catalog: Path, *, seed: int = 11, days: int = 28, month: Month = FEB) -> CandleTree:
    """``days`` days of ``walk`` from the month's start, committed, with an api funding row every hour."""
    tree = candle_tree(catalog)
    rows = walk(seed, days * 1440, month.start_ns)
    tree.write_bars(month, rows)
    tree.write_funding(
        month, [FundingRow(month.start_ns + h * HOUR_NS, D("0.0001"), HOUR_NS, "api") for h in range(days * 24 + 1)]
    )
    manifest = MonthManifest(
        str(month), len(rows), 0, 0, 0, 0, 0, [], rows[0].ts_ns, rows[-1].ts_ns, 0, 0, 0, False, "2026-10-01T00:00:00Z"
    )
    tree.write_manifest(manifest.with_funding(tree.read_funding(month)))
    return tree
