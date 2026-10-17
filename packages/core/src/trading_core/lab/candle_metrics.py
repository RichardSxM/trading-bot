"""The candle report's extra measures (spec 0014 AC-16), pure, all USD ``Decimal``.

``run_results.max_drawdown`` keeps spec 0008's realized only meaning; these sit beside it in the
report only: a marked to market equity curve sampled at every hour close (realized equity, plus
the open position's P&L at that close's mid, plus funding booked so far) with its drawdown and the
longest drawdown duration, the long and short split, exposure time, profit factor, expectancy, and
the buy and hold benchmark.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_core.lab.metrics import FundingCash
from trading_core.marketdata.hours import HOUR_NS


@dataclass(frozen=True)
class MarkFill:
    """One fill as the equity curve needs it: when, which way, how much, at what price, and its fee."""

    ts_ns: int
    buy: bool
    quantity: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class TradeOutcome:
    """One position: its side, when it was open (``closed_ns`` None while open), and its net P&L when closed."""

    long: bool
    opened_ns: int
    closed_ns: int | None
    net_pnl: Decimal


@dataclass(frozen=True)
class EquityPoint:
    """The marked equity at one hour close."""

    ts_ns: int
    equity: Decimal


@dataclass(frozen=True)
class SideSplit:
    """Closed trades and their net P&L on one side."""

    trades: int
    net_pnl: Decimal


@dataclass(frozen=True)
class CandleMeasures:
    """Everything the candle report adds."""

    marked_max_drawdown: Decimal
    longest_drawdown_hours: int
    long: SideSplit
    short: SideSplit
    exposure: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    benchmark: Decimal | None


def marked_equity(
    starting_balance: Decimal,
    fills: Sequence[MarkFill],
    funding: Sequence[FundingCash],
    marks: Sequence[tuple[int, Decimal]],
) -> list[EquityPoint]:
    """The equity at each ``(hour close, mid)`` in ``marks``: the netted position's realized P&L after
    fees through the fills up to that time, its open P&L at the mid, and the funding paid by then."""
    ordered = sorted(fills, key=lambda f: f.ts_ns)
    paid = sorted(funding, key=lambda f: f.paid_ns)
    paid_times = [f.paid_ns for f in paid]
    qty = avg = realized = Decimal(0)
    index = 0
    points: list[EquityPoint] = []
    for ts_ns, mid in sorted(marks):
        while index < len(ordered) and ordered[index].ts_ns <= ts_ns:
            fill = ordered[index]
            signed = fill.quantity if fill.buy else -fill.quantity
            realized -= fill.fee
            if qty == 0 or (qty > 0) == (signed > 0):
                avg = (avg * abs(qty) + fill.price * abs(signed)) / (abs(qty) + abs(signed))
                qty += signed
            else:
                closing = min(abs(qty), abs(signed))
                realized += (fill.price - avg) * closing * (1 if qty > 0 else -1)
                qty += signed
                if qty == 0:
                    avg = Decimal(0)
                elif (qty > 0) == (signed > 0):  # flipped through flat: the rest opens at this price
                    avg = fill.price
            index += 1
        booked = sum((f.amount for f in paid[: bisect_right(paid_times, ts_ns)]), Decimal(0))
        points.append(EquityPoint(ts_ns, starting_balance + realized + (mid - avg) * qty + booked))
    return points


def drawdown(points: Sequence[EquityPoint], starting_balance: Decimal) -> tuple[Decimal, int]:
    """The largest fall from a running peak (a fraction of the peak), and the longest time in hours from a
    peak until equity is back above it, or until the last point. The starting balance is the first peak."""
    peak, peak_ns = starting_balance, points[0].ts_ns if points else 0
    worst = Decimal(0)
    longest = 0
    under = False
    for point in points:
        if point.equity > peak:
            if under:
                longest = max(longest, (point.ts_ns - peak_ns) // HOUR_NS)
            peak, peak_ns, under = point.equity, point.ts_ns, False
        elif point.equity < peak:
            under = True
            if peak > 0:
                worst = max(worst, (peak - point.equity) / peak)
    if under and points:
        longest = max(longest, (points[-1].ts_ns - peak_ns) // HOUR_NS)
    return worst, longest


def exposure(trades: Sequence[TradeOutcome], start_ns: int, end_ns: int) -> Decimal:
    """The share of ``start_ns``..``end_ns`` with a position open."""
    if end_ns <= start_ns:
        return Decimal(0)
    held = 0
    for trade in trades:
        opened = max(trade.opened_ns, start_ns)
        closed = min(end_ns if trade.closed_ns is None else trade.closed_ns, end_ns)
        held += max(0, closed - opened)
    return Decimal(held) / Decimal(end_ns - start_ns)


def benchmark(first_close: Decimal | None, last_close: Decimal | None) -> Decimal | None:
    """Buy and hold before costs: ``(last close - first close) / first close``; None without candles."""
    if first_close is None or last_close is None or first_close == 0:
        return None
    return (last_close - first_close) / first_close


def candle_measures(
    starting_balance: Decimal,
    curve: Sequence[EquityPoint],
    trades: Sequence[TradeOutcome],
    start_ns: int,
    end_ns: int,
    first_close: Decimal | None,
    last_close: Decimal | None,
) -> CandleMeasures:
    """Every measure the candle report adds, from the curve, the positions, and the range's candles."""
    worst, longest = drawdown(curve, starting_balance)
    closed = [t for t in trades if t.closed_ns is not None]
    wins = sum((t.net_pnl for t in closed if t.net_pnl > 0), Decimal(0))
    losses = -sum((t.net_pnl for t in closed if t.net_pnl < 0), Decimal(0))
    longs = [t for t in closed if t.long]
    shorts = [t for t in closed if not t.long]
    return CandleMeasures(
        marked_max_drawdown=worst,
        longest_drawdown_hours=longest,
        long=SideSplit(len(longs), sum((t.net_pnl for t in longs), Decimal(0))),
        short=SideSplit(len(shorts), sum((t.net_pnl for t in shorts), Decimal(0))),
        exposure=exposure(trades, start_ns, end_ns),
        profit_factor=wins / losses if losses else None,
        expectancy=sum((t.net_pnl for t in closed), Decimal(0)) / len(closed) if closed else None,
        benchmark=benchmark(first_close, last_close),
    )
