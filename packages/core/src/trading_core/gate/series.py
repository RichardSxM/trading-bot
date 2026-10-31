"""Daily return series and the daily Sharpe (spec 0015 AC-6), pure.

A candle run (and a chained paper series) is one continuous equity curve: a day's return is its close
over the previous day's close, minus 1, and the first day is measured against the starting balance.
A replay run is a set of closed one day segments, each starting at the starting balance: a day's
return is that day's net P&L over the starting balance.

Ruin (amended for spec 0026): a day's return is never below -1.0. In a chained series the first day
closing at or below zero equity is the ruin day, scored -1.0, and every later day is 0.0, because
nothing is left to lose. The days stay in the series, so folds line up.

Decimal turns into float only here, for the statistics. Sums use ``math.fsum`` over values in day
order, so the same rows always give the same float. Anything stored goes back through ``to_decimal``,
rounded to 6 places.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

ANNUALIZE: Final = math.sqrt(365)
_PLACES: Final = Decimal("0.000001")


@dataclass(frozen=True)
class DayEquity:
    """One ``run_daily_equity`` row as the gate reads it."""

    day: date
    equity_close: Decimal
    trades_closed: int
    fees: Decimal
    funding: Decimal


@dataclass(frozen=True)
class DailySeries:
    """Returns per UTC day, in day order, with the trades closed and the net P&L of each day.

    ``ruined_on`` is a chained series' ruin day (its first close at or below zero equity), else None.
    """

    days: tuple[date, ...]
    returns: tuple[float, ...]
    trades: tuple[int, ...]
    net_pnl: tuple[Decimal, ...]
    ruined_on: date | None = None

    def __post_init__(self) -> None:
        if not len(self.days) == len(self.returns) == len(self.trades) == len(self.net_pnl):
            raise ValueError("a daily series needs one value of each kind per day")
        if any(a >= b for a, b in zip(self.days, self.days[1:], strict=False)):
            raise ValueError("a daily series must be in strictly increasing day order")

    def __len__(self) -> int:
        return len(self.days)

    def between(self, first: date, last: date) -> DailySeries:
        """The days from ``first`` to ``last``, both included (a fold's slice)."""
        keep = [i for i, d in enumerate(self.days) if first <= d <= last]
        return self._pick(keep)

    def only(self, days: Iterable[date]) -> DailySeries:
        """The days in ``days`` only (a benchmark limited to another run's days)."""
        wanted = set(days)
        return self._pick([i for i, d in enumerate(self.days) if d in wanted])

    def _pick(self, keep: Sequence[int]) -> DailySeries:
        return DailySeries(
            days=tuple(self.days[i] for i in keep),
            returns=tuple(self.returns[i] for i in keep),
            trades=tuple(self.trades[i] for i in keep),
            net_pnl=tuple(self.net_pnl[i] for i in keep),
            ruined_on=self.ruined_on if self.ruined_on in {self.days[i] for i in keep} else None,
        )

    def total_trades(self) -> int:
        """Trades closed over the whole series."""
        return sum(self.trades)

    def total_net(self) -> Decimal:
        """Net P&L over the whole series."""
        return sum(self.net_pnl, Decimal(0))

    def compounded(self) -> float:
        """The series' total return, compounding each day's return (in day order)."""
        growth = 1.0
        for r in self.returns:
            growth *= 1.0 + r
        return growth - 1.0


def daily_series(rows: Sequence[DayEquity], starting_balance: Decimal, *, chained: bool) -> DailySeries:
    """The daily series of a run's ``run_daily_equity`` rows (any order).

    ``chained`` for a candle run or a paper series, else each day is its own replay segment.
    A return is never below -1.0; a chained series scores 0.0 after its ruin day (see the module doc).
    ``net_pnl`` stays each day's real change in equity. Raises ``ValueError`` for a non positive
    starting balance.
    """
    if starting_balance <= 0:
        raise ValueError(f"starting balance {starting_balance} must be positive")
    ordered = sorted(rows, key=lambda r: r.day)
    returns: list[float] = []
    net: list[Decimal] = []
    previous = starting_balance
    ruined_on: date | None = None
    for row in ordered:
        base = previous if chained else starting_balance
        change = row.equity_close - base
        net.append(change)
        previous = row.equity_close
        if ruined_on is not None:
            returns.append(0.0)
            continue
        returns.append(max(float(change / base), -1.0))
        if chained and row.equity_close <= 0:
            ruined_on = row.day
    return DailySeries(
        days=tuple(r.day for r in ordered),
        returns=tuple(returns),
        trades=tuple(r.trades_closed for r in ordered),
        net_pnl=tuple(net),
        ruined_on=ruined_on,
    )


def mean(values: Sequence[float]) -> float:
    """The mean, summed with ``math.fsum``; raises ``ValueError`` when empty."""
    if not values:
        raise ValueError("mean of no values")
    return math.fsum(values) / len(values)


def sample_std(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof 1); raises ``ValueError`` with fewer than 2 values."""
    if len(values) < 2:
        raise ValueError("a sample standard deviation needs at least 2 values")
    m = mean(values)
    return math.sqrt(math.fsum((v - m) ** 2 for v in values) / (len(values) - 1))


def sharpe(returns: Sequence[float]) -> float | None:
    """Per day Sharpe: mean over the sample std; None with fewer than 2 days or zero variance."""
    if len(returns) < 2:
        return None
    std = sample_std(returns)
    if std == 0.0:
        return None
    return mean(returns) / std


def annualized(per_day: float | None) -> float | None:
    """A per day Sharpe times √365, the figure stored and shown (a ranking convention for replay)."""
    return None if per_day is None else per_day * ANNUALIZE


def to_decimal(value: float | None) -> Decimal | None:
    """A statistic as a Decimal rounded half even to 6 places; None stays None."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"statistic {value!r} is not finite")
    return Decimal(repr(value)).quantize(_PLACES, rounding=ROUND_HALF_EVEN)
