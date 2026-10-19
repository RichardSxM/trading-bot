"""The price path a 1 minute candle becomes (spec 0014 AC-9), pure.

A traded minute with open time ``m`` becomes four points: the open at ``m + 1 ms``, the two
extremes at ``m + 20 s`` and ``m + 40 s``, and the close at ``m + 59.999 s``. The extremes' order
follows the strategy's net position: long, low first; short, high first; flat, the extreme nearer
the open first (the high on a tie). Each point is a trade at the candle price, then a quote around
it with the slippage built into the spread, so a market fill always pays ``slippage_bps``. A quiet
minute is one quote at ``m + 59.999 s`` and no trade, so no volume is ever made up.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final

from trading_core.lab.candles import MINUTE_NS, CandleRow

MS_NS: Final = 1_000_000
POINT_OFFSETS_NS: Final = (MS_NS, 20_000 * MS_NS, 40_000 * MS_NS, MINUTE_NS - MS_NS)
# The quote size on both sides, far above any order check_entry can approve (the pin has no max quantity).
QUOTE_SIZE: Final = Decimal(1000)
# The final quote repeats the last close this long after the last point, for the range end close (AC-14).
FINAL_QUOTE_DELAY_NS: Final = 2 * MS_NS


@dataclass(frozen=True)
class PathPoint:
    """One point of the path: a trade of ``trade_size`` at ``price`` (none in a quiet minute), then a quote."""

    ts_ns: int
    index: int
    price: Decimal
    trade_size: Decimal | None
    bid: Decimal
    ask: Decimal

    @property
    def trade_id(self) -> str:
        """``<ts_init>-<point index>``."""
        return f"{self.ts_ns}-{self.index}"


def spread(price: Decimal, slippage_bps: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
    """The bid (rounded down to the tick) and ask (rounded up) ``slippage_bps`` either side of ``price``."""
    s = slippage_bps / 10_000
    bid = (price * (1 - s) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    ask = (price * (1 + s) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return bid, ask


def extremes(candle: CandleRow, side: int) -> tuple[Decimal, Decimal]:
    """The first and second extreme for a net position ``side`` (+1 long, -1 short, 0 flat)."""
    if side > 0:
        return candle.low, candle.high
    if side < 0:
        return candle.high, candle.low
    high_nearer = candle.high - candle.open <= candle.open - candle.low
    return (candle.high, candle.low) if high_nearer else (candle.low, candle.high)


def trade_sizes(volume: Decimal, step: Decimal) -> list[Decimal]:
    """The four trade sizes: ``max(step, volume / 4 floored to the step)`` thrice, then ``max(step, the rest)``."""
    quarter = max(step, (volume / 4 / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    return [quarter, quarter, quarter, max(step, volume - 3 * quarter)]


def path_points(candle: CandleRow, side: int, slippage_bps: Decimal, tick: Decimal, step: Decimal) -> list[PathPoint]:
    """The points of one stored minute (AC-9)."""
    open_ns = candle.open_ns
    if candle.quiet:
        bid, ask = spread(candle.close, slippage_bps, tick)
        return [PathPoint(open_ns + POINT_OFFSETS_NS[3], 0, candle.close, None, bid, ask)]
    first, second = extremes(candle, side)
    prices = (candle.open, first, second, candle.close)
    sizes = trade_sizes(candle.volume, step)
    points: list[PathPoint] = []
    for index, (offset, price, size) in enumerate(zip(POINT_OFFSETS_NS, prices, sizes, strict=True)):
        bid, ask = spread(price, slippage_bps, tick)
        points.append(PathPoint(open_ns + offset, index, price, size, bid, ask))
    return points


def final_point(last: PathPoint) -> PathPoint:
    """The final quote: the last point's price again, ``FINAL_QUOTE_DELAY_NS`` later, no trade."""
    return PathPoint(last.ts_ns + FINAL_QUOTE_DELAY_NS, 0, last.price, None, last.bid, last.ask)
