"""Walk forward folds (spec 0015 AC-9), pure: anchored and expanding, cut from the development runs by date.

Candle folds count calendar months from the first development day: the first in sample range is
``first_in_sample_months`` long, each out of sample step ``step_months``. Replay folds count positions
in the sorted list of development days every completed grid point has a row for: the first
``first_in_sample_days`` days in sample, then ``step_days`` per step. A trailing step shorter than a
full step is dropped. Each fold's in sample range runs from the start to its cut.

Per fold the grid point with the best in sample daily Sharpe is chosen (an undefined Sharpe ranks
last, ties to the lowest grid index) and scored on the out of sample slice. The walk forward series is
every fold's out of sample slice joined in date order; its Sharpe is the ranking key.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from trading_core.gate.series import DailySeries, sharpe


@dataclass(frozen=True)
class FoldRange:
    """One fold's ranges as day bounds (inclusive); the stored days are the real days inside them."""

    is_start: date
    is_end: date
    oos_start: date
    oos_end: date


@dataclass(frozen=True)
class ScoredFold:
    """One fold after scoring: its real day bounds, the chosen grid point, and its numbers."""

    fold: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    chosen_grid_point: int
    is_sharpe: float | None
    oos: DailySeries


@dataclass(frozen=True)
class WalkForward:
    """Every scored fold and the joined out of sample series; ``missing`` names folds with no usable grid point."""

    folds: tuple[ScoredFold, ...]
    series: DailySeries
    missing: tuple[int, ...]


def add_months(day: date, months: int) -> date:
    """``day`` moved by whole calendar months, clamped to the target month's last day."""
    month0 = day.month - 1 + months
    year, month = day.year + month0 // 12, month0 % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise AssertionError("unreachable: every month has at least 28 days")


def candle_ranges(first: date, last: date, first_in_sample_months: int, step_months: int) -> list[FoldRange]:
    """Month based folds over ``first``..``last`` (inclusive); a step that would pass ``last`` is dropped."""
    folds: list[FoldRange] = []
    cut = add_months(first, first_in_sample_months)
    while True:
        end = add_months(cut, step_months)
        if end - timedelta(days=1) > last:
            return folds
        folds.append(FoldRange(first, cut - timedelta(days=1), cut, end - timedelta(days=1)))
        cut = end


def replay_ranges(days: Sequence[date], first_in_sample_days: int, step_days: int) -> list[FoldRange]:
    """Position based folds over the sorted distinct ``days``; a short trailing step is dropped."""
    ordered = sorted(set(days))
    folds = []
    cut = first_in_sample_days
    while cut + step_days <= len(ordered):
        folds.append(FoldRange(ordered[0], ordered[cut - 1], ordered[cut], ordered[cut + step_days - 1]))
        cut += step_days
    return folds


def _choose(slices: Mapping[int, DailySeries]) -> tuple[int, float | None] | None:
    """The grid point with the best per day Sharpe (undefined last, ties to the lowest index)."""
    scored = [(point, sharpe(s.returns)) for point, s in slices.items() if len(s)]
    if not scored:
        return None
    scored.sort(key=lambda p: (p[1] is None, -(p[1] or 0.0), p[0]))
    return scored[0]


def _join(parts: Sequence[DailySeries]) -> DailySeries:
    return DailySeries(
        days=tuple(d for p in parts for d in p.days),
        returns=tuple(r for p in parts for r in p.returns),
        trades=tuple(t for p in parts for t in p.trades),
        net_pnl=tuple(n for p in parts for n in p.net_pnl),
    )


def walk_forward(ranges: Sequence[FoldRange], points: Mapping[int, DailySeries]) -> WalkForward:
    """Score every fold over the completed grid points' development series (keyed by grid index)."""
    scored: list[ScoredFold] = []
    missing: list[int] = []
    for number, fold in enumerate(ranges, start=1):
        chosen = _choose({p: s.between(fold.is_start, fold.is_end) for p, s in points.items()})
        if chosen is None:
            missing.append(number)
            continue
        point, is_sharpe = chosen
        series = points[point]
        in_sample = series.between(fold.is_start, fold.is_end)
        oos = series.between(fold.oos_start, fold.oos_end)
        if not len(oos):
            missing.append(number)
            continue
        scored.append(
            ScoredFold(
                fold=number,
                is_start=in_sample.days[0],
                is_end=in_sample.days[-1],
                oos_start=oos.days[0],
                oos_end=oos.days[-1],
                chosen_grid_point=point,
                is_sharpe=is_sharpe,
                oos=oos,
            )
        )
    return WalkForward(tuple(scored), _join([f.oos for f in scored]), tuple(missing))


def common_days(points: Mapping[int, DailySeries]) -> list[date]:
    """The days every series has a row for, sorted."""
    sets = [set(s.days) for s in points.values()]
    return sorted(set.intersection(*sets)) if sets else []


def fold_net(fold: ScoredFold) -> Decimal:
    """A fold's out of sample net P&L."""
    return fold.oos.total_net()
