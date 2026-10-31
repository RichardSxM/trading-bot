"""Export ranges (spec 0024 AC-2, AC-14): Amsterdam calendar dates to UTC bounds, validation, month chunks.

A range covers ``[start 00:00 Europe/Amsterdam, (end + 1 day) 00:00 Europe/Amsterdam)``. Daylight
saving comes from ``zoneinfo``, so a day may last 23 or 25 hours. The end must be yesterday
(Amsterdam) or earlier, so every row in range is settled and its rate published.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from trading_core.recording.enums import RunMode

AMSTERDAM: Final = ZoneInfo("Europe/Amsterdam")
TIME_ZONE: Final = "Europe/Amsterdam"
EXPORT_MODES: Final = (RunMode.LIVE, RunMode.PAPER)
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class JournalRangeError(ValueError):
    """A refused export request (reason ``bad_range``); the message says why, for the panel and the 422."""

    reason: Final = "bad_range"


@dataclass(frozen=True)
class JournalRange:
    """A validated request: inclusive Amsterdam dates, one mode, and the half open UTC bounds."""

    start: date
    end: date
    mode: RunMode
    start_utc: datetime
    end_utc: datetime


def amsterdam_today(now: datetime) -> date:
    """Today's calendar date in Europe/Amsterdam for a timezone aware ``now``."""
    if now.utcoffset() is None:
        raise ValueError("naive datetime: now must be timezone aware")
    return now.astimezone(AMSTERDAM).date()


def amsterdam_midnight(day: date) -> datetime:
    """``day`` 00:00 in Europe/Amsterdam, as a UTC datetime."""
    return datetime.combine(day, time(0), AMSTERDAM).astimezone(UTC)


def default_dates(today: date) -> tuple[date, date]:
    """The form's default range: 1 January to yesterday, both in yesterday's year."""
    yesterday = today - timedelta(days=1)
    return date(yesterday.year, 1, 1), yesterday


def default_mode(trading_env: str) -> RunMode:
    """``live`` in the live env, ``paper`` otherwise (AC-1)."""
    return RunMode.LIVE if trading_env == "live" else RunMode.PAPER


def _parse_date(name: str, text: str | None) -> date:
    if text is None or _DATE.fullmatch(text) is None:
        raise JournalRangeError(f"The {name} date must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise JournalRangeError(f"The {name} date {text} does not exist.") from None


def parse_range(start: str | None, end: str | None, mode: str | None, today: date) -> JournalRange:
    """Validate the three request strings against ``today`` (Amsterdam); raises ``JournalRangeError``.

    Refused: a missing or malformed date, ``start > end``, ``end`` today or later, a mode other
    than ``live`` or ``paper``.
    """
    first, last = _parse_date("start", start), _parse_date("end", end)
    if mode not in {m.value for m in EXPORT_MODES}:
        raise JournalRangeError("The mode must be live or paper.")
    if first > last:
        raise JournalRangeError("The start date is after the end date.")
    if last >= today:
        raise JournalRangeError("The end date must be yesterday or earlier, so every row is settled.")
    return JournalRange(
        start=first,
        end=last,
        mode=RunMode(mode),
        start_utc=amsterdam_midnight(first),
        end_utc=amsterdam_midnight(last + timedelta(days=1)),
    )


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def month_chunks(journal_range: JournalRange) -> list[tuple[datetime, datetime]]:
    """The range cut at each Amsterdam month start, as half open UTC bounds in order (AC-14)."""
    chunks = []
    day = journal_range.start
    after = journal_range.end + timedelta(days=1)
    while day < after:
        stop = min(_next_month(day), after)
        chunks.append((amsterdam_midnight(day), amsterdam_midnight(stop)))
        day = stop
    return chunks
