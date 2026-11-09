"""The pure verdict of the liquidity trap pre check (spec 0031 AC-17).

Only signals with a 15 s markout count. The result is ``pass`` only when all four checks hold.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from trading_core.marketdata.hours import DAY_NS

APPROXIMATE_LABEL: Final = "approximate: Tardis has no liquidation flag"
_BPS: Final = Decimal(10_000)

type Verdict = Literal["pass", "fail"]


@dataclass(frozen=True)
class DayMarkouts:
    """One day's 15 s markouts in bps and its eligible time in nanoseconds."""

    day: str
    markouts: tuple[Decimal, ...]
    eligible_ns: int


@dataclass(frozen=True)
class VerdictCheck:
    """One check with its value (None when it cannot be computed) and its threshold."""

    name: str
    value: Decimal | None
    threshold: Decimal
    passed: bool


@dataclass(frozen=True)
class LtsVerdict:
    """The result, every check, and the cost it was judged against."""

    verdict: Verdict
    cost_bps: Decimal
    checks: tuple[VerdictCheck, ...]
    label: str


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _positive(day: DayMarkouts) -> bool:
    mean = _mean(day.markouts)
    return day.eligible_ns > 0 and mean is not None and mean > 0


def _without_best_day(days: Sequence[DayMarkouts]) -> Decimal | None:
    """The pooled mean without the day of largest markout sum, ties to the earliest; None if nothing is left."""
    with_signals = [day for day in days if day.markouts]
    if not with_signals:
        return None
    best = max(with_signals, key=lambda day: sum(day.markouts, Decimal(0)))
    return _mean([m for day in with_signals if day is not best for m in day.markouts])


def _check(name: str, value: Decimal | None, threshold: Decimal) -> VerdictCheck:
    return VerdictCheck(name, value, threshold, value is not None and value >= threshold)


def evaluate_lts(
    days: Sequence[DayMarkouts],
    *,
    maker_fee: Decimal,
    taker_fee: Decimal,
    hurdle_k: Decimal,
    min_signals_per_day: int,
    min_positive_days: int,
) -> LtsVerdict:
    """The four checks of AC-17 over the days' 15 s markouts."""
    cost = (maker_fee + taker_fee) * _BPS
    pooled = [m for day in days for m in day.markouts]
    eligible = sum(day.eligible_ns for day in days)
    rate = Decimal(len(pooled)) * DAY_NS / eligible if eligible > 0 else None
    checks = (
        _check("pooled_mean_15s_bps", _mean(pooled), hurdle_k * cost),
        _check("signals_per_24_eligible_hours", rate, Decimal(min_signals_per_day)),
        _check("positive_days", Decimal(sum(_positive(day) for day in days)), Decimal(min_positive_days)),
        _check("pooled_mean_without_best_day_bps", _without_best_day(days), cost),
    )
    verdict: Verdict = "pass" if all(c.passed for c in checks) else "fail"
    return LtsVerdict(verdict, cost, checks, APPROXIMATE_LABEL)
