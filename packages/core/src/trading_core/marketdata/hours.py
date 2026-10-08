"""Hour math and the ready rule for the hourly catalog (spec 0007, *Lifecycle of one hour*).

Times are Nautilus nanosecond ints since the Unix epoch, UTC. An hour is named by its start.
"""

from collections.abc import Mapping
from typing import Final

SECOND_NS: Final = 1_000_000_000
HOUR_NS: Final = 3_600 * SECOND_NS


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
