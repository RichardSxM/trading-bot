"""The recorder's data queue depth rules (spec 0007 AC-13).

When the data engine's queue is full, Nautilus 1.231.0 lets newer messages overtake the waiting
ones, so a deep queue means rows are about to reach the feather out of order. These rules decide
which code to log; the recorder's actor reads the depth and the clock.
"""

from collections.abc import Mapping
from typing import Final

from trading_core.marketdata.hours import SECOND_NS

QUEUE_HIGH: Final = "recorder_queue_high"
QUEUE_FULL: Final = "recorder_queue_full"
STOP_QUEUE_FULL: Final = "recorder_stop_queue_full"
STOP_QUEUE: Final = "recorder_stop_queue"
# Each code is logged at most once per this long.
ALERT_EVERY_NS: Final = 60 * SECOND_NS
# At stop, a queue this full likely had pending puts, which shutdown cancels.
STOP_FULL_PCT: Final = 99


def queue_alert(
    depth: int,
    capacity: int,
    *,
    warn_pct: int,
    now_ns: int,
    last_logged: Mapping[str, int],
    every_ns: int = ALERT_EVERY_NS,
) -> str | None:
    """The code to log now for a queue ``depth`` out of ``capacity``, or ``None``.

    ``recorder_queue_full`` at capacity, else ``recorder_queue_high`` at or above ``warn_pct``
    percent. ``last_logged`` holds when each code was last logged; a code repeats only once
    ``every_ns`` has passed since then.
    """
    if depth >= capacity:
        code = QUEUE_FULL
    elif depth * 100 >= warn_pct * capacity:
        code = QUEUE_HIGH
    else:
        return None
    last = last_logged.get(code)
    if last is not None and now_ns - last < every_ns:
        return None
    return code


def stop_code(depth: int, capacity: int) -> str:
    """The code to log when the recorder stops: ``recorder_stop_queue_full`` at 99% or more."""
    return STOP_QUEUE_FULL if depth * 100 >= STOP_FULL_PCT * capacity else STOP_QUEUE
