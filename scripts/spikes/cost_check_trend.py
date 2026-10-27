"""Spec 0020 task 1 cost check: how wide are the noise bands and the trend stops against the cost? Throwaway.

Reads the development range's stored 1 minute candles through the candle lab's own reader, builds UTC
aligned 30 minute and 4 hour bars (a window with no stored candle is skipped), and prints:
  the median ATR(14) of 30 minute bars in bps (ATR / close x 10 000),
  for band_mult 1, 1.5, and 2: the median noise band half width in bps (band_mult x sigma x 10 000, over
    every 30 minute bar with a ready sigma) and the share of those bars whose close is beyond
    day_open x (1 +- band_mult x sigma), a rough guide to how often noise_breakout could signal,
  the median ATR(14) of 4 hour bars in bps, and 2 x and 3 x that (the donchian_4h stop distances).
sigma follows the spec's NoiseBands: the mean |close / day_open - 1| of the slot over the 14 earlier days
that had it, where day_open is the open of the day's 00:00 bar (no such bar: no bands that day). These are
properties of the data; no strategy is built or run, so it is not a trial (spec 0020 AC-8).

Run in the lab image from the main checkout (it needs the lab catalog):
  KFS_TAG=s0019 docker compose --profile lab run --rm --no-deps -T \
    -v "$PWD/scripts/spikes:/spike:ro" --entrypoint python lab /spike/cost_check_trend.py
"""

import sys
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from lab.candle_fetch import candle_tree
from trading_core.lab.candles import MINUTE_NS, Month
from trading_core.strategy.bar_indicators import Atr

FROM = datetime(2022, 4, 1, tzinfo=UTC)
TO = datetime(2025, 10, 19, tzinfo=UTC)  # exclusive: holdout_start
HALF_HOUR_NS = 30 * MINUTE_NS
FOUR_HOURS_NS = 240 * MINUTE_NS
DAY_NS = 1440 * MINUTE_NS
LOOKBACK_DAYS = 14
BAND_MULTS = (Decimal(1), Decimal("1.5"), Decimal(2))
BPS = Decimal(10_000)
ROUND_TRIP_BPS = Decimal(14)  # 2 x 5 bps taker + 2 x 2 bps slippage

type Ohlc = tuple[int, Decimal, Decimal, Decimal, Decimal]  # open time, open, high, low, close


class Bars:
    """Aggregates 1 minute candles into UTC aligned bars of ``interval_ns``; a window with no candle is skipped."""

    def __init__(self, interval_ns: int) -> None:
        self.interval_ns = interval_ns
        self.done: list[Ohlc] = []
        self._key: int | None = None
        self._open = self._high = self._low = self._close = Decimal(0)

    def add(self, open_ns: int, o: Decimal, h: Decimal, lo: Decimal, c: Decimal) -> None:
        key = open_ns // self.interval_ns
        if key != self._key:
            self.flush()
            self._key, self._open, self._high, self._low = key, o, h, lo
        self._high, self._low, self._close = max(self._high, h), min(self._low, lo), c

    def flush(self) -> None:
        if self._key is not None:
            self.done.append((self._key * self.interval_ns, self._open, self._high, self._low, self._close))
            self._key = None


def atr_bps(bars: list[Ohlc]) -> list[Decimal]:
    atr = Atr(14)
    out: list[Decimal] = []
    for _, _, h, lo, c in bars:
        a = atr.update(h, lo, c)
        if a is not None:
            out.append(a / c * BPS)
    return out


def noise(bars: list[Ohlc]) -> tuple[list[Decimal], dict[Decimal, int]]:
    """Every ready sigma, and per band_mult the count of closes beyond the band."""
    history: dict[int, deque[Decimal]] = {}
    sigmas: list[Decimal] = []
    beyond = dict.fromkeys(BAND_MULTS, 0)
    day: int | None = None
    day_open: Decimal | None = None
    for open_ns, o, _, _, c in bars:
        key, slot = divmod(open_ns, DAY_NS)
        slot //= HALF_HOUR_NS
        if key != day:
            day, day_open = key, o if slot == 0 else None
        past = history.setdefault(slot, deque(maxlen=LOOKBACK_DAYS))
        sigma = sum(past, Decimal(0)) / LOOKBACK_DAYS if len(past) == LOOKBACK_DAYS else None
        if day_open is not None and sigma is not None:
            sigmas.append(sigma)
            for mult in BAND_MULTS:
                if c > day_open * (1 + mult * sigma) or c < day_open * (1 - mult * sigma):
                    beyond[mult] += 1
        if day_open is not None:
            past.append(abs(c / day_open - 1))
    return sigmas, beyond


def main(catalog: Path) -> None:
    tree = candle_tree(catalog)
    start, end = int(FROM.timestamp()) * 10**9, int(TO.timestamp()) * 10**9
    month = Month.of_ns(start)
    half_hours, four_hours = Bars(HALF_HOUR_NS), Bars(FOUR_HOURS_NS)
    while month.start_ns < end:
        for row in tree.read_bars(month):
            if row.open_ns < start or row.ts_ns > end:
                continue
            half_hours.add(row.open_ns, row.open, row.high, row.low, row.close)
            four_hours.add(row.open_ns, row.open, row.high, row.low, row.close)
        month = month.next()
    half_hours.flush()
    four_hours.flush()

    print(f"range {FROM.date()} to {TO.date()} (exclusive)")
    print(f"round trip cost: about {ROUND_TRIP_BPS} bps")
    med_30 = Decimal(median(atr_bps(half_hours.done)))
    print(
        f"{len(half_hours.done)} thirty minute bars, "
        f"median ATR(14): {med_30:.2f} bps ({med_30 / ROUND_TRIP_BPS:.2f} x cost)"
    )
    sigmas, beyond = noise(half_hours.done)
    med_sigma = Decimal(median(sigmas))
    print(f"noise_breakout: {len(sigmas)} bars with a ready sigma (lookback {LOOKBACK_DAYS} days)")
    for mult in BAND_MULTS:
        width = mult * med_sigma * BPS
        share = Decimal(beyond[mult]) / len(sigmas) * 100
        print(
            f"  band_mult {mult}: median half width {width:.2f} bps ({width / ROUND_TRIP_BPS:.2f} x cost), "
            f"closes beyond the band {share:.1f}%"
        )
    med_4h = Decimal(median(atr_bps(four_hours.done)))
    print(
        f"{len(four_hours.done)} four hour bars, "
        f"median ATR(14): {med_4h:.2f} bps ({med_4h / ROUND_TRIP_BPS:.2f} x cost)"
    )
    for mult in (2, 3):
        print(
            f"  donchian_4h stop at {mult} ATR: {mult * med_4h:.2f} bps ({mult * med_4h / ROUND_TRIP_BPS:.2f} x cost)"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/catalog-lab"))
