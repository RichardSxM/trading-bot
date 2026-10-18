"""Pure rules of the candle lab (spec 0014 AC-1 to AC-4): months, candle rows, funding periods, and the merge.

Kraken's 1 minute ``trade`` candles are stored one file per UTC month, stamped at the minute's close.
Funding rows are keyed by their charge time: Kraken stamps a rate with the start of the period it
applies to (gate G-2), so the charge time is the stamp plus the period, 4 h before
``FUNDING_HOURLY_FROM`` and 1 h from it on. A 4 h era rate is charged once per period (gate G-4), so
every stored rate means "charged once at its charge time".
"""

import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from trading_core.lab.errors import CandleInputError
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS

MINUTE_NS: Final = 60 * SECOND_NS
FUNDING_HOURLY_FROM: Final = datetime(2022, 9, 29, 12, tzinfo=UTC)
FUNDING_HOURLY_FROM_NS: Final = int(FUNDING_HOURLY_FROM.timestamp()) * SECOND_NS
FOUR_HOURS_NS: Final = 4 * HOUR_NS
# No backtest range starts before this day: Kraken's first minute is 2022-03-23 10:03 UTC (gate G-1).
CANDLE_RANGE_FLOOR: Final = date(2022, 3, 24)
# The oldest month ``fetch-candles`` takes.
FIRST_MONTH: Final = "2022-03"
# A missing funding row may be charged at the latest earlier rate when that one is at most this old (AC-6).
FUNDING_CARRY_MAX_NS: Final = 3 * HOUR_NS
# How often a candle run's stale quote check runs, in seconds (AC-13).
CANDLE_STALE_CHECK_S: Final = 60
# A volume this close to a multiple of the size step is float noise and is rounded to it (AC-1).
VOLUME_NOISE: Final = Decimal("1e-8")
# Five or more consecutive missing minutes make a gap row (AC-4).
GAP_MIN_MINUTES: Final = 5

FundingSource = Literal["export", "api"]


@dataclass(frozen=True, order=True)
class Month:
    """One UTC calendar month."""

    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> Month:
        """A ``YYYY-MM`` month. Raises ``CandleInputError`` on anything else."""
        match = re.fullmatch(r"(\d{4})-(\d{2})", text)
        if match is None or not 1 <= int(match[2]) <= 12:
            raise CandleInputError(f"{text!r} is not a YYYY-MM month")
        return cls(int(match[1]), int(match[2]))

    @classmethod
    def of_ns(cls, ts_ns: int) -> Month:
        """The UTC month holding ``ts_ns``."""
        moment = datetime.fromtimestamp(ts_ns // SECOND_NS, UTC)
        return cls(moment.year, moment.month)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def next(self) -> Month:
        """The month after this one."""
        return Month(self.year + 1, 1) if self.month == 12 else Month(self.year, self.month + 1)

    def previous(self) -> Month:
        """The month before this one."""
        return Month(self.year - 1, 12) if self.month == 1 else Month(self.year, self.month - 1)

    @property
    def start_ns(self) -> int:
        """The month's first nanosecond."""
        return int(datetime(self.year, self.month, 1, tzinfo=UTC).timestamp()) * SECOND_NS

    @property
    def end_ns(self) -> int:
        """The next month's first nanosecond (exclusive end)."""
        return self.next().start_ns


def fetch_months(first: str, last: str, now_ns: int) -> list[Month]:
    """The months from ``first`` to ``last``, both inclusive, oldest first (AC-1).

    Raises ``CandleInputError`` for a malformed month, one before ``FIRST_MONTH`` or after the
    current UTC month, or ``first`` after ``last``.
    """
    start, end = Month.parse(first), Month.parse(last)
    floor, current = Month.parse(FIRST_MONTH), Month.of_ns(now_ns)
    problems = [f"{m} is before {floor}" for m in dict.fromkeys((start, end)) if m < floor]
    problems += [f"{m} is after the current month {current}" for m in dict.fromkeys((start, end)) if m > current]
    if start > end:
        problems.append(f"{start} is after {end}")
    if problems:
        raise CandleInputError("; ".join(problems))
    months = [start]
    while months[-1] < end:
        months.append(months[-1].next())
    return months


def is_complete(month: Month, fetch_start_ns: int) -> bool:
    """A month is complete once it ended before the fetch started (AC-1)."""
    return month.end_ns <= fetch_start_ns


def funding_period_ns(stamp_ns: int) -> int:
    """The period a funding stamp names: 4 h before ``FUNDING_HOURLY_FROM``, 1 h from it on."""
    return FOUR_HOURS_NS if stamp_ns < FUNDING_HOURLY_FROM_NS else HOUR_NS


def charge_time_ns(stamp_ns: int) -> int:
    """When a stamped rate is charged: the end of the period it names."""
    return stamp_ns + funding_period_ns(stamp_ns)


def expected_charge_times(start_ns: int, end_ns: int) -> list[int]:
    """The funding charge times strictly inside ``start_ns``..``end_ns`` (AC-2).

    Every 4 h UTC boundary before ``FUNDING_HOURLY_FROM``, every whole hour from it on.
    """
    times: list[int] = []
    t = start_ns - start_ns % HOUR_NS + HOUR_NS
    while t < end_ns:
        if t >= FUNDING_HOURLY_FROM_NS or t % FOUR_HOURS_NS == 0:
            times.append(t)
        t += HOUR_NS
    return times


def snap_volume(volume: Decimal, step: Decimal) -> Decimal | None:
    """The volume on the size step: itself when on it, the nearest step (``ROUND_HALF_EVEN``) when
    within ``VOLUME_NOISE``, or ``None`` when further off (a bad row, AC-3)."""
    snapped = (volume / step).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * step
    snapped = snapped.quantize(step)
    if abs(snapped - volume) > VOLUME_NOISE:
        return None
    return snapped


def is_quiet(open_: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> bool:
    """A quiet minute: no volume after rounding and a flat candle (AC-1)."""
    return volume == 0 and open_ == high == low == close


@dataclass(frozen=True)
class CandleRow:
    """One stored 1 minute candle. ``ts_ns`` is the minute's close (its open + 60 s)."""

    ts_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quiet: bool

    @property
    def open_ns(self) -> int:
        """The minute's open."""
        return self.ts_ns - MINUTE_NS


@dataclass(frozen=True)
class RawCandle:
    """One candle as Kraken returns it: open time in ms and decimal text."""

    time_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str


@dataclass
class CandleBuild:
    """The rows built from one month's raw candles, with the counts the manifest keeps and every bad row.

    ``quiet_unchecked`` is true when the month opens with quiet rows and no previous close was given,
    so those rows were only checked flat (AC-3).
    """

    rows: list[CandleRow] = field(default_factory=list)
    volume_rounded_rows: int = 0
    zero_volume_trade_rows: int = 0
    bad: list[tuple[str, str, str]] = field(default_factory=list)
    quiet_unchecked: bool = False


def _quiet_problem(row: CandleRow, prev_close: Decimal) -> tuple[str, str, str] | None:
    """The bad row of a quiet minute that is not flat at the previous close, or ``None``."""
    if row.open == prev_close:
        return None
    return (str(row.open_ns), "quiet", f"flat at {row.open}, previous close {prev_close}")


def build_candles(
    raw: Iterable[RawCandle], month: Month, tick: Decimal, step: Decimal, prev_close: Decimal | None = None
) -> CandleBuild:
    """Turn a month's raw candles into rows, sorted by close time, one per minute (AC-1, AC-3).

    Identical repeats across page edges are kept once; candles outside the month are dropped. A
    volume off the step by more than ``VOLUME_NOISE``, a price off the tick, a time off the whole
    minute, a high below the open or close, a low above them, two different candles for one minute,
    or a quiet minute not flat at the close of the latest earlier candle are bad rows. For the
    month's first rows that close is ``prev_close`` (the previous month's last stored candle); when
    it is ``None`` they are only checked flat and ``quiet_unchecked`` says so.
    """
    build = CandleBuild()
    seen: dict[int, RawCandle] = {}
    for candle in raw:
        open_ns = candle.time_ms * 1_000_000
        if not month.start_ns <= open_ns < month.end_ns:
            continue
        if candle.time_ms % (MINUTE_NS // 1_000_000):
            build.bad.append((str(open_ns), "time", f"{candle.time_ms} ms is not on a whole minute"))
            continue
        if open_ns in seen:
            if seen[open_ns] != candle:
                build.bad.append((str(open_ns), "duplicate", f"{seen[open_ns]} vs {candle}"))
            continue
        seen[open_ns] = candle
    last_close = prev_close
    for open_ns in sorted(seen):
        candle = seen[open_ns]
        prices = [Decimal(p) for p in (candle.open, candle.high, candle.low, candle.close)]
        names = ("open", "high", "low", "close")
        off_tick = [name for name, p in zip(names, prices, strict=True) if p % tick or p <= 0]
        build.bad.extend((str(open_ns), name, getattr(candle, name)) for name in off_tick)
        volume = snap_volume(Decimal(candle.volume), step)
        if volume is None or volume < 0:
            build.bad.append((str(open_ns), "volume", candle.volume))
            continue
        if off_tick:
            continue
        o, h, lo, c = prices
        if h < max(o, c):
            build.bad.append((str(open_ns), "high", f"{candle.high} below open or close"))
        if lo > min(o, c):
            build.bad.append((str(open_ns), "low", f"{candle.low} above open or close"))
        quiet = is_quiet(o, h, lo, c, volume)
        row = CandleRow(open_ns + MINUTE_NS, o, h, lo, c, volume, quiet)
        if quiet and last_close is None:
            build.quiet_unchecked = True
        elif quiet and last_close is not None and (problem := _quiet_problem(row, last_close)):
            build.bad.append(problem)
        last_close = c
        build.volume_rounded_rows += volume != Decimal(candle.volume)
        build.zero_volume_trade_rows += volume == 0 and not quiet
        build.rows.append(row)
    return build


def quiet_prefix_problems(rows: Sequence[CandleRow], prev_close: Decimal) -> list[tuple[str, str, str]]:
    """The bad rows among a month's leading quiet rows (up to its first trade minute) checked against
    ``prev_close``, the previous month's last close (AC-3, the ``--refetch`` check of the next month)."""
    bad: list[tuple[str, str, str]] = []
    for row in rows:
        if not row.quiet:
            break
        if problem := _quiet_problem(row, prev_close):
            bad.append(problem)
    return bad


@dataclass(frozen=True)
class CandleGap:
    """A run of ``GAP_MIN_MINUTES`` or more missing minutes: ``start_ns``..``end_ns`` are the missing
    minutes' first open and last close."""

    start_ns: int
    end_ns: int
    minutes: int


def find_gaps(rows: Sequence[CandleRow], start_ns: int, end_ns: int) -> tuple[int, list[CandleGap]]:
    """The missing minutes between ``start_ns`` and ``end_ns`` (both on whole minutes), and the gap rows (AC-4)."""
    missing = 0
    gaps: list[CandleGap] = []
    expected_open = start_ns
    for open_ns in [*(r.open_ns for r in rows), end_ns]:
        run = (open_ns - expected_open) // MINUTE_NS
        if run > 0:
            missing += run
            if run >= GAP_MIN_MINUTES:
                gaps.append(CandleGap(expected_open, open_ns, run))
        expected_open = open_ns + MINUTE_NS
    return missing, gaps


@dataclass(frozen=True)
class FundingRow:
    """One stored funding rate, keyed by its charge time, with the period it covers and its first source."""

    charge_ns: int
    rate: Decimal
    period_ns: int
    source: FundingSource

    @classmethod
    def from_stamp(cls, stamp_ns: int, rate: Decimal, source: FundingSource) -> FundingRow:
        """The row of a rate Kraken stamped at ``stamp_ns`` (the start of its period)."""
        return cls(charge_time_ns(stamp_ns), rate, funding_period_ns(stamp_ns), source)

    @property
    def next_funding_ns(self) -> int:
        """The charge time plus the period (the ``FundingRateUpdate`` field)."""
        return self.charge_ns + self.period_ns


def check_funding_stamp(stamp_ns: int, rate: Decimal) -> list[tuple[str, str, str]]:
    """The AC-3 checks of one funding row: a whole hour, a 4 h boundary in the 4 h era, a finite rate."""
    bad: list[tuple[str, str, str]] = []
    if stamp_ns % HOUR_NS or (stamp_ns < FUNDING_HOURLY_FROM_NS and stamp_ns % FOUR_HOURS_NS):
        bad.append((str(stamp_ns), "timestamp", "not on a funding boundary"))
    if not rate.is_finite():
        bad.append((str(stamp_ns), "rate", str(rate)))
    return bad


@dataclass(frozen=True)
class FundingMerge:
    """The merged rows by charge time, how many were new and already present, and every disagreement."""

    rows: list[FundingRow]
    added: int
    present: int
    conflicts: list[tuple[str, str, str]]


def merge_funding(stored: Iterable[FundingRow], new: Iterable[FundingRow]) -> FundingMerge:
    """Merge new rows into stored ones (AC-2): never delete, add what is missing, and a row both stored
    and new must have the same rate (otherwise a conflict). An agreeing row keeps its first source."""
    by_charge = {row.charge_ns: row for row in stored}
    added = present = 0
    conflicts: list[tuple[str, str, str]] = []
    for row in new:
        old = by_charge.get(row.charge_ns)
        if old is None:
            by_charge[row.charge_ns] = row
            added += 1
        elif old.rate == row.rate:
            present += 1
        else:
            conflicts.append((str(row.charge_ns), "rate", f"stored {old.rate}, {row.source} {row.rate}"))
    return FundingMerge(sorted(by_charge.values(), key=lambda r: r.charge_ns), added, present, conflicts)


def missing_charges(rows: Sequence[FundingRow], first_ts_ns: int | None, last_ts_ns: int | None) -> int:
    """The expected charge times from ``first_ts_ns`` to ``last_ts_ns`` (both inclusive) with no stored row (AC-4)."""
    if first_ts_ns is None or last_ts_ns is None:
        return 0
    have = {r.charge_ns for r in rows}
    return sum(t not in have for t in expected_charge_times(first_ts_ns - 1, last_ts_ns + 1))


def rows_by_month(rows: Iterable[FundingRow]) -> dict[Month, list[FundingRow]]:
    """Funding rows grouped by the UTC month of their charge time, each group in charge order."""
    months: dict[Month, list[FundingRow]] = {}
    for row in sorted(rows, key=lambda r: r.charge_ns):
        months.setdefault(Month.of_ns(row.charge_ns), []).append(row)
    return months


def carried_rate(rows: Sequence[FundingRow], ts_ns: int, max_age_ns: int = FUNDING_CARRY_MAX_NS) -> FundingRow | None:
    """The row ``rate_at`` would pick at ``ts_ns`` (the latest at or before it), or ``None`` when there is
    none or it is more than ``max_age_ns`` old. ``rows`` must be sorted by charge time."""
    index = bisect_left([r.charge_ns for r in rows], ts_ns + 1)
    if index == 0 or rows[index - 1].charge_ns < ts_ns - max_age_ns:
        return None
    return rows[index - 1]


@dataclass(frozen=True)
class MonthManifest:
    """A month's commit point and its totals (AC-4). ``fetched_at`` is UTC ISO 8601."""

    month: str
    bar_rows: int
    quiet_minutes: int
    volume_rounded_rows: int
    zero_volume_trade_rows: int
    funding_rows: int
    funding_sources: list[str]
    first_ts_ns: int | None
    last_ts_ns: int | None
    missing_minutes: int
    gap_count: int
    missing_funding_charges: int
    complete: bool
    fetched_at: str

    def with_funding(self, rows: Sequence[FundingRow]) -> MonthManifest:
        """The same manifest with its funding fields recomputed from the month's stored rows (AC-2).

        The last candle closes on the next month's first nanosecond, a charge time filed under that
        month, so only charge times inside this month count as missing.
        """
        last = self.last_ts_ns if self.last_ts_ns is None else min(self.last_ts_ns, Month.parse(self.month).end_ns - 1)
        return replace(
            self,
            funding_rows=len(rows),
            funding_sources=sorted({r.source for r in rows}),
            missing_funding_charges=missing_charges(rows, self.first_ts_ns, last),
        )
