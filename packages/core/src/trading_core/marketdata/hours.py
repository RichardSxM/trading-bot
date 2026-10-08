"""Hour math and the ready rule for the hourly catalog (spec 0007, *Lifecycle of one hour*).

Times are Nautilus nanosecond ints since the Unix epoch, UTC. An hour is named by its start.
"""

import math
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

SECOND_NS: Final = 1_000_000_000
HOUR_NS: Final = 3_600 * SECOND_NS
DAY_NS: Final = 24 * HOUR_NS
GB: Final = 1_000_000_000


def hour_of(ts_ns: int) -> int:
    """The start of the UTC hour holding ``ts_ns``."""
    return ts_ns - ts_ns % HOUR_NS


def hour_last_ns(hour: int) -> int:
    """The last nanosecond of the hour that starts at ``hour``, as the catalog's interval end."""
    return hour + HOUR_NS - 1


def hour_ready(
    hour: int,
    *,
    now_ns: int,
    grace_ns: int,
    positions: Mapping[str, int],
    run_ended: bool,
) -> bool:
    """Whether the hour that starts at ``hour`` can be finished.

    ``positions`` holds, for each delta and trade stream that had rows in the hour, the newest
    ``ts_init`` read so far from its closed files. The hour is ready once ``now`` is past its end
    plus the grace, and every such stream has passed its end, or the run that wrote it has ended.
    """
    end = hour + HOUR_NS
    if now_ns < end + grace_ns:
        return False
    return run_ended or all(position >= end for position in positions.values())


def day_of(ts_ns: int) -> int:
    """The start of the UTC day holding ``ts_ns``."""
    return ts_ns - ts_ns % DAY_NS


def day_name(day: int) -> str:
    """``YYYY-MM-DD`` of the UTC day that starts at ``day``, as gap and manifest files are named."""
    return datetime.fromtimestamp(day // SECOND_NS, UTC).strftime("%Y-%m-%d")


def hours_inside(start_ns: int, end_ns: int) -> list[int]:
    """The hours that lie wholly inside ``[start_ns, end_ns]``, such as a ``recorder_down`` gap."""
    first = -(-start_ns // HOUR_NS) * HOUR_NS
    return list(range(first, end_ns - HOUR_NS + 1, HOUR_NS))


def day_closed(day: int, covered: Mapping[str, Collection[int]]) -> bool:
    """Whether every hour of the UTC day that starts at ``day`` is accounted for (spec 0007 AC-5).

    ``covered`` holds, per instrument, its converted delta hours plus the empty hours inside a
    ``recorder_down`` gap. Hours before an instrument's first covered hour are before it was
    recorded at all, so they do not count; an instrument that starts after the day is skipped.
    """
    end = day + DAY_NS
    started = False
    for hours in covered.values():
        if not hours or min(hours) >= end:
            continue
        started = True
        first = max(day, min(hours))
        if any(hour not in hours for hour in range(first, end, HOUR_NS)):
            return False
    return started


def backlog_bytes_needed(feather_bytes: int, ratio: Decimal, floor_bytes: int) -> int:
    """Free space a dead run needs before it is converted (spec 0007 AC-9).

    Its Parquet (the feather bytes times ``ratio``, rounded up) plus the free space floor, since
    the feather is freed only after its hours are done.
    """
    return math.ceil(feather_bytes * ratio) + floor_bytes
