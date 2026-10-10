"""Which recorded hours a replay may use, grouped into segments (spec 0008 AC-3, AC-4).

A replay only fills against clean hours: no gap row touches them, the compactor wrote them
whole, both streams exist, and the hour opens with the carried book as a snapshot. Clean hours
that follow each other form a segment, cut at every 00:00 UTC so no segment crosses a UTC
midnight. Each segment replays in its own fresh engine and starts flat.

Times are Nautilus nanosecond ints (UTC). Hours are given by their first nanosecond.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Literal

from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.hours import DAY_NS, HOUR_NS

Source = Literal["tardis", "own"]

INCOMPLETE: Final = "incomplete"
MISSING: Final = "missing"
NO_SNAPSHOT: Final = "no_snapshot"


@dataclass(frozen=True)
class HourFiles:
    """What the catalog holds for one hour: both stream files, and whether the deltas open with a ``CLEAR``.

    ``starts_with_clear`` is only read when both files exist.
    """

    has_deltas: bool
    has_trades: bool
    starts_with_clear: bool


@dataclass(frozen=True)
class Segment:
    """Consecutive clean hours inside one UTC day, replayed by one fresh engine.

    ``index`` numbers segments from 1 in time order; it names the strategy's order id tag ``S<index>``.
    ``end_ns`` is the last nanosecond of the last hour.
    """

    index: int
    hours: tuple[int, ...]

    @property
    def start_ns(self) -> int:
        """The first nanosecond of the first hour."""
        return self.hours[0]

    @property
    def end_ns(self) -> int:
        """The last nanosecond of the last hour."""
        return self.hours[-1] + HOUR_NS - 1


@dataclass(frozen=True)
class SkippedHour:
    """An hour left out of every segment, with the first reason that applies."""

    hour_ns: int
    reason: str


@dataclass(frozen=True)
class SegmentPlan:
    """The planner's answer: segments in time order, and every hour it skipped."""

    segments: tuple[Segment, ...]
    skipped: tuple[SkippedHour, ...]

    def skipped_by_reason(self) -> dict[str, int]:
        """How many hours each reason skipped, sorted by reason (for the plan log line)."""
        counts: dict[str, int] = {}
        for hour in self.skipped:
            counts[hour.reason] = counts.get(hour.reason, 0) + 1
        return dict(sorted(counts.items()))


def is_first_of_month(day: date) -> bool:
    """True on the first day of a month, the only days Tardis gives away free."""
    return day.day == 1


def required_days(source: Source, first: date, last: date) -> list[date]:
    """The UTC days a replay of ``first``..``last`` (both inclusive) needs a manifest for (AC-3).

    Every day for ``own``; only the first of month days for ``tardis`` (the others never exist).
    Raises ``ValueError`` when ``first`` is after ``last``.
    """
    if first > last:
        raise ValueError(f"from {first} is after to {last}")
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return days if source == "own" else [d for d in days if is_first_of_month(d)]


def day_start_ns(day: date) -> int:
    """The first nanosecond of a UTC day."""
    return (day - date(1970, 1, 1)).days * DAY_NS


def hours_of(days: Iterable[date]) -> list[int]:
    """Every hour of the given days, in order."""
    return [day_start_ns(day) + n * HOUR_NS for day in sorted(days) for n in range(24)]


def _overlaps(hour_ns: int, gap: Gap) -> bool:
    # Edges count: a gap that ends on the hour's first nanosecond still touches it.
    return gap.start_ns <= hour_ns + HOUR_NS - 1 and gap.end_ns >= hour_ns


def _skip_reason(hour_ns: int, gaps: Sequence[Gap], incomplete: Collection[int], files: HourFiles | None) -> str | None:
    for gap in gaps:
        if _overlaps(hour_ns, gap):
            return f"gap:{gap.kind}"
    if hour_ns in incomplete:
        return INCOMPLETE
    if files is None or not (files.has_deltas and files.has_trades):
        return MISSING
    if not files.starts_with_clear:
        return NO_SNAPSHOT
    return None


def plan_segments(
    hours: Sequence[int],
    gaps: Iterable[Gap],
    incomplete: Collection[int],
    files: Mapping[int, HourFiles],
) -> SegmentPlan:
    """Drop every unclean hour and group the rest into segments cut at UTC midnight (AC-4).

    ``hours`` are the range's hours in order. ``gaps`` are the gap rows of every required day
    and of the day before the first (a gap is filed under its start day). ``incomplete`` holds
    the ``incomplete_hours`` of the ``order_book_deltas`` and ``trade_tick`` streams. ``files``
    says what the catalog holds per hour (an hour absent from it counts as missing). An hour is
    skipped for the first reason that applies: ``gap:<kind>``, ``incomplete``, ``missing``,
    ``no_snapshot``.
    """
    ordered = sorted(hours)
    if len(set(ordered)) != len(ordered) or any(h % HOUR_NS for h in ordered):
        raise ValueError("hours must be distinct whole hours")
    gap_list = sorted(gaps)
    segments: list[Segment] = []
    skipped: list[SkippedHour] = []
    current: list[int] = []

    def close() -> None:
        if current:
            segments.append(Segment(index=len(segments) + 1, hours=tuple(current)))
            current.clear()

    for hour in ordered:
        reason = _skip_reason(hour, gap_list, incomplete, files.get(hour))
        if reason is not None:
            skipped.append(SkippedHour(hour, reason))
            close()
            continue
        if current and (hour != current[-1] + HOUR_NS or hour % DAY_NS == 0):
            close()
        current.append(hour)
    close()
    return SegmentPlan(segments=tuple(segments), skipped=tuple(skipped))
