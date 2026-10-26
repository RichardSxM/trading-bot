"""Spec 0019 task 1 cost check: is a 15 minute move several times the round trip cost? Throwaway.

Reads the development range's stored 1 minute candles through the candle lab's own reader, builds UTC
aligned 15 minute bars (a 15 minute window with no stored candle is skipped), and prints:
  the median ATR(14) in bps (ATR / close x 10 000),
  each new candidate's implied minimum target distance in bps (its ATR multiple x that median),
  for connors_rsi the median |close - SMA(5)| in bps.
These are properties of the data; no strategy is built or run, so it is not a trial (spec 0019 AC-9).

Run in the lab image from the main checkout (it needs the lab catalog):
  KFS_TAG=s0019 docker compose --profile lab run --rm --no-deps -T \
    -v "$WT/scripts/spikes:/spike:ro" --entrypoint python lab /spike/cost_check_15m.py
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from lab.candle_fetch import candle_tree
from trading_core.lab.candles import MINUTE_NS, Month
from trading_core.strategy.bar_indicators import Atr, Sma

FROM = datetime(2022, 4, 1, tzinfo=UTC)
TO = datetime(2025, 10, 19, tzinfo=UTC)  # exclusive: holdout_start
BAR_NS = 15 * MINUTE_NS
BPS = Decimal(10_000)
ROUND_TRIP_BPS = Decimal(14)  # 2 x 5 bps taker + 2 x 2 bps slippage
# Each new candidate's nearest target, in ATRs of 15 minute bars (the child specs' Rules).
TARGET_ATR = {
    "ema_pullback (1.5 R on a stop of at least 1 ATR)": Decimal("1.5"),
    "vwap_reversion (the VWAP at least stretch_atr 2 ATR away)": Decimal(2),
    "us_open_breakout (1.5 R on a stop of at least 1 ATR)": Decimal("1.5"),
}


def main(catalog: Path) -> None:
    tree = candle_tree(catalog)
    start, end = int(FROM.timestamp()) * 10**9, int(TO.timestamp()) * 10**9
    month = Month.of_ns(start)
    atr, sma = Atr(14), Sma(5)
    atr_bps: list[Decimal] = []
    sma_bps: list[Decimal] = []
    bars = 0
    window: list[tuple[Decimal, Decimal, Decimal]] = []
    window_key: int | None = None

    def close_window() -> None:
        nonlocal bars
        if not window:
            return
        high = max(h for h, _, _ in window)
        low = min(lo for _, lo, _ in window)
        close = window[-1][2]
        bars += 1
        a = atr.update(high, low, close)
        s = sma.update(close)
        if a is not None:
            atr_bps.append(a / close * BPS)
        if s is not None:
            sma_bps.append(abs(close - s) / close * BPS)

    while month.start_ns < end:
        for row in tree.read_bars(month):
            if row.open_ns < start or row.ts_ns > end:
                continue
            key = row.open_ns // BAR_NS
            if key != window_key:
                close_window()
                window, window_key = [], key
            window.append((row.high, row.low, row.close))
        month = month.next()
    close_window()

    med_atr = Decimal(median(atr_bps))
    print(f"range {FROM.date()} to {TO.date()} (exclusive), {bars} fifteen minute bars")
    print(f"round trip cost: about {ROUND_TRIP_BPS} bps")
    print(f"median ATR(14) of 15 minute bars: {med_atr:.2f} bps")
    for name, mult in TARGET_ATR.items():
        target = mult * med_atr
        print(f"  {name}: at least {target:.2f} bps ({target / ROUND_TRIP_BPS:.2f} x cost)")
    med_sma = Decimal(median(sma_bps))
    print(f"connors_rsi median |close - SMA(5)|: {med_sma:.2f} bps ({med_sma / ROUND_TRIP_BPS:.2f} x cost)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/catalog-lab"))
