"""Daily equity closes and per trade measures of a lab run (spec 0015 AC-6), pure, all USD ``Decimal``.

A candle run's close is its marked equity at each UTC midnight (the hourly curve of ``candle_metrics``),
a day with no newer mark carrying the last one forward. A replay run's days are closed segments, each
starting at the starting balance, so a day's close is the balance plus the net P&L of the positions
closed that day. Every row also carries the day's closed trades and fees (by close time) and funding
(by payment time).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trading_core.lab.candle_metrics import EquityPoint
from trading_core.lab.metrics import FundingCash

_DAY_NS = 86_400 * 1_000_000_000


@dataclass(frozen=True)
class ClosedPosition:
    """A closed position as these measures need it; ``funding_total`` is positive when received."""

    long: bool
    closed_ns: int
    realized_pnl: Decimal
    fees_total: Decimal
    funding_total: Decimal

    @property
    def net_pnl(self) -> Decimal:
        """Realized P&L (already after fees) plus funding."""
        return self.realized_pnl + self.funding_total


@dataclass(frozen=True)
class DayClose:
    """One UTC day's equity close and its sums."""

    day: date
    equity_close: Decimal
    trades_closed: int
    fees: Decimal
    funding: Decimal


@dataclass(frozen=True)
class TradeMeasures:
    """The long and short split, profit factor, expectancy, concentration, and edge and cost per trade."""

    long_trades: int
    long_net_pnl: Decimal
    short_trades: int
    short_net_pnl: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    top_trades_profit_share: Decimal | None
    avg_gross_edge: Decimal | None
    avg_round_trip_cost: Decimal | None


def day_of(ns: int) -> date:
    """The UTC day of a nanosecond time."""
    return datetime.fromtimestamp(ns // 1_000_000_000, UTC).date()


def _day_start_ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def _sums(
    day: date, positions: Sequence[ClosedPosition], funding: Sequence[FundingCash]
) -> tuple[int, Decimal, Decimal]:
    closed = [p for p in positions if day_of(p.closed_ns) == day]
    paid = sum((f.amount for f in funding if day_of(f.paid_ns) == day), Decimal(0))
    return len(closed), sum((p.fees_total for p in closed), Decimal(0)), paid


def marked_daily(
    curve: Sequence[EquityPoint],
    start_ns: int,
    end_ns: int,
    starting_balance: Decimal,
    positions: Sequence[ClosedPosition],
    funding: Sequence[FundingCash],
) -> list[DayClose]:
    """One row per UTC day from ``start_ns`` to ``end_ns``: the marked equity at the day's close."""
    points = sorted(curve, key=lambda p: p.ts_ns)
    rows: list[DayClose] = []
    equity = starting_balance
    index = 0
    day = day_of(start_ns)
    while _day_start_ns(day) < end_ns:
        close_ns = _day_start_ns(day) + _DAY_NS
        while index < len(points) and points[index].ts_ns <= close_ns:
            equity = points[index].equity
            index += 1
        trades, fees, paid = _sums(day, positions, funding)
        rows.append(DayClose(day, equity, trades, fees, paid))
        day += timedelta(days=1)
    return rows


def realized_daily(
    days: Sequence[date],
    starting_balance: Decimal,
    positions: Sequence[ClosedPosition],
    funding: Sequence[FundingCash],
) -> list[DayClose]:
    """One row per replayed day: the starting balance plus the net P&L of that day's closed positions."""
    rows = []
    for day in sorted(set(days)):
        net = sum((p.net_pnl for p in positions if day_of(p.closed_ns) == day), Decimal(0))
        trades, fees, paid = _sums(day, positions, funding)
        rows.append(DayClose(day, starting_balance + net, trades, fees, paid))
    return rows


def trade_measures(positions: Sequence[ClosedPosition], top_quantile: Decimal) -> TradeMeasures:
    """The per trade measures of a run's closed positions.

    ``top_trades_profit_share`` is the share of net profit from the best ``ceil(top_quantile * n)``
    trades, None when net profit is not positive. Gross edge is net P&L plus fees minus funding;
    round trip cost is fees plus any funding paid, both per trade (spec 0015 Feature design).
    """
    n = len(positions)
    longs = [p for p in positions if p.long]
    shorts = [p for p in positions if not p.long]
    nets = sorted((p.net_pnl for p in positions), reverse=True)
    total = sum(nets, Decimal(0))
    wins = sum((v for v in nets if v > 0), Decimal(0))
    losses = -sum((v for v in nets if v < 0), Decimal(0))
    top: Decimal | None = None
    if n and total > 0:
        k = math.ceil(top_quantile * n)
        top = sum(nets[:k], Decimal(0)) / total
    fees = sum((p.fees_total for p in positions), Decimal(0))
    funding = sum((p.funding_total for p in positions), Decimal(0))
    return TradeMeasures(
        long_trades=len(longs),
        long_net_pnl=sum((p.net_pnl for p in longs), Decimal(0)),
        short_trades=len(shorts),
        short_net_pnl=sum((p.net_pnl for p in shorts), Decimal(0)),
        profit_factor=wins / losses if losses else None,
        expectancy=total / n if n else None,
        top_trades_profit_share=top,
        avg_gross_edge=sum((p.net_pnl + p.fees_total - p.funding_total for p in positions), Decimal(0)) / n
        if n
        else None,
        avg_round_trip_cost=(fees + max(Decimal(0), -funding)) / n if n else None,
    )
