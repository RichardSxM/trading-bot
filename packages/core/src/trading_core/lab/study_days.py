"""Which free Tardis days a replay study replays (spec 0015 AC-24).

A replay study uses the 1st of each month only, never our own recorder days, so development and
holdout are captured the same way. Development is every 1st from the study's ``from`` to the day
before ``holdout_start``; the holdout is every 1st from ``holdout_start`` up to the latest one before
the current UTC day, which the caller passes in (no wall clock here). A planned day without a
manifest is never skipped (the command exits 2); a day whose gaps cover all 24 hours is dropped by
that fixed rule when the plan is made.
"""

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Final

from trading_core.lab.errors import StudyDaysError
from trading_core.lab.segments import SkippedHour, day_start_ns, is_first_of_month
from trading_core.marketdata.hours import DAY_NS

# PF_XBTUSD is on Tardis since 2022-03-22, so its first whole free day is 2022-04-01.
TARDIS_FIRST_DAY: Final = date(2022, 4, 1)
GAP_REASON_PREFIX: Final = "gap:"
_HOURS_PER_DAY: Final = 24


def firsts_of_month(first: date, last: date) -> list[date]:
    """Every 1st of the month from ``first`` to ``last``, both inclusive, oldest first (empty when none)."""
    day = first if is_first_of_month(first) else _next_first(first)
    days: list[date] = []
    while day <= last:
        days.append(day)
        day = _next_first(day)
    return days


def _next_first(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def development_days(start: date, holdout_start: date) -> list[date]:
    """The development days: every 1st of the month from ``start`` to the day before ``holdout_start``.

    Raises ``StudyDaysError`` when ``start`` is not a 1st of the month or is before ``TARDIS_FIRST_DAY``.
    """
    problems: list[str] = []
    if not is_first_of_month(start):
        problems.append(f"from {start} is not a 1st of the month (a replay study uses free Tardis days only)")
    if start < TARDIS_FIRST_DAY:
        problems.append(f"from {start} is before {TARDIS_FIRST_DAY}, the first whole Tardis day of PF_XBTUSD")
    if problems:
        raise StudyDaysError("; ".join(problems))
    return firsts_of_month(start, holdout_start - timedelta(days=1))


def holdout_days(holdout_start: date, today: date) -> list[date]:
    """The holdout days: every 1st of the month from ``holdout_start`` up to the latest one before ``today``.

    ``today`` is the current UTC date, passed in by the caller. The day ``today`` itself is never
    included, because it is not complete yet.
    """
    return firsts_of_month(holdout_start, today - timedelta(days=1))


def import_command(missing: Sequence[date]) -> str:
    """The ``lab import-tardis`` command that fetches every missing day (its oldest to its newest)."""
    if not missing:
        raise ValueError("no missing day to fetch")
    return f"lab import-tardis {min(missing).isoformat()} {max(missing).isoformat()}"


def dropped_days(days: Iterable[date], skipped: Iterable[SkippedHour]) -> list[date]:
    """The days whose every hour was skipped for a gap, so they have no clean segment, oldest first.

    ``skipped`` is the segment plan's skipped hours. A partly gapped day is kept and replays its
    clean hours.
    """
    gapped: dict[int, int] = {}
    for hour in skipped:
        if hour.reason.startswith(GAP_REASON_PREFIX):
            day_ns = hour.hour_ns - hour.hour_ns % DAY_NS
            gapped[day_ns] = gapped.get(day_ns, 0) + 1
    return sorted(d for d in set(days) if gapped.get(day_start_ns(d), 0) == _HOURS_PER_DAY)
