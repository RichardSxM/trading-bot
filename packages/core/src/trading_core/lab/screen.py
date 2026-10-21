"""The candle screen's rule (spec 0017 AC-9): a cheap filter before a scalping candidate's replay study.

It reads a candle study's development runs as the gate reads them (``CandidateInputs``) and passes
when three things hold: the walk forward Sharpe (the gate's folds, best in sample point per fold) is
above 0; the final grid point's average gross edge per trade is at least ``fee_hurdle_k`` times its
average round trip cost (both in USD per trade, as the gate's ``fee_hurdle``); and that point trades at
least ``SCREEN_MIN_TRADES_PER_DAY`` a day (its trade count over its days of daily equity). A figure
that is missing fails the screen with ``missing:<figure>``. A pass is a filter, never evidence of an edge.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.gate.folds import candle_ranges, walk_forward
from trading_core.gate.inputs import CandidateInputs, Role
from trading_core.gate.series import DailySeries, annualized, sharpe, to_decimal

# The ceiling of 200 / 12: the replay holdout's trade floor over its 12 free Tardis days.
SCREEN_MIN_TRADES_PER_DAY: Final = 17

WALK_FORWARD: Final = "walk_forward_sharpe"
FEE_HURDLE: Final = "fee_hurdle"
TRADES_PER_DAY: Final = "trades_per_day"
MISSING_PREFIX: Final = "missing:"


@dataclass(frozen=True)
class ScreenFigures:
    """What the rule reads; ``None`` where the study has no such figure."""

    walk_forward_sharpe: Decimal | None
    avg_gross_edge: Decimal | None
    avg_round_trip_cost: Decimal | None
    trade_count: int | None
    days: int

    @property
    def trades_per_day(self) -> Decimal | None:
        """The final point's trades per day of daily equity, or None without either."""
        if self.trade_count is None or self.days == 0:
            return None
        return Decimal(self.trade_count) / self.days


@dataclass(frozen=True)
class ScreenVerdict:
    """Pass or fail, with every failing reason (empty on a pass)."""

    passed: bool
    reasons: tuple[str, ...]

    @property
    def trade_rate_only(self) -> bool:
        """True when only the trade rate failed: the candle lab's fills or the signal are too rare, not the idea."""
        return self.reasons == (TRADES_PER_DAY,)


def screen_verdict(figures: ScreenFigures, fee_hurdle_k: Decimal) -> ScreenVerdict:
    """Apply the AC-9 rule to ``figures``."""
    reasons: list[str] = []
    if figures.walk_forward_sharpe is None:
        reasons.append(MISSING_PREFIX + WALK_FORWARD)
    elif figures.walk_forward_sharpe <= 0:
        reasons.append(WALK_FORWARD)
    edge, cost = figures.avg_gross_edge, figures.avg_round_trip_cost
    if edge is None:
        reasons.append(MISSING_PREFIX + "avg_gross_edge")
    if cost is None:
        reasons.append(MISSING_PREFIX + "avg_round_trip_cost")
    if edge is not None and cost is not None and edge < fee_hurdle_k * cost:
        reasons.append(FEE_HURDLE)
    rate = figures.trades_per_day
    if rate is None:
        reasons.append(MISSING_PREFIX + TRADES_PER_DAY)
    elif rate < SCREEN_MIN_TRADES_PER_DAY:
        reasons.append(TRADES_PER_DAY)
    return ScreenVerdict(passed=not reasons, reasons=tuple(reasons))


def _development_points(inputs: CandidateInputs) -> dict[int, DailySeries]:
    points: dict[int, DailySeries] = {}
    for run in inputs.by_role(Role.DEVELOPMENT):
        if run.completed and run.grid_point is not None and run.grid_point not in points:
            points[run.grid_point] = run.series(inputs.lab)
    return points


def candle_walk_forward_sharpe(inputs: CandidateInputs) -> Decimal | None:
    """The annualized walk forward Sharpe of a candle study's development runs, with its frozen fold layout.

    None without a fold, with a fold lacking a run, or with zero variance.
    """
    points = _development_points(inputs)
    days = sorted({d for s in points.values() for d in s.days})
    if not days:
        return None
    layout = dict(inputs.fold_overrides)
    t = inputs.thresholds.candle
    ranges = candle_ranges(
        days[0],
        days[-1],
        layout.get("first_in_sample_months", t.first_in_sample_months),
        layout.get("step_months", t.step_months),
    )
    if not ranges:
        return None
    wf = walk_forward(ranges, points)
    if wf.missing:
        return None
    return to_decimal(annualized(sharpe(wf.series.returns)))


def screen_figures(inputs: CandidateInputs, final_grid_point: int) -> ScreenFigures:
    """The rule's figures: the walk forward Sharpe, and the final point's edge, cost, trades, and days."""
    final = next(
        (r for r in inputs.by_role(Role.DEVELOPMENT) if r.completed and r.grid_point == final_grid_point), None
    )
    measures = None if final is None else final.measures
    summary = None if final is None else final.summary
    return ScreenFigures(
        walk_forward_sharpe=candle_walk_forward_sharpe(inputs),
        avg_gross_edge=None if measures is None else measures.avg_gross_edge,
        avg_round_trip_cost=None if measures is None else measures.avg_round_trip_cost,
        trade_count=None if summary is None else summary.trade_count,
        days=0 if final is None else len(final.daily),
    )
