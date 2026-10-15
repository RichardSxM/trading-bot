"""The recorder's buffered feather writer rules (spec 0012).

The recorder holds each stream's messages and writes them as one batch. These rules decide when a
stream flushes, when its file rotates, and what the writer's stats say; the recorder's writer
does the I/O. Times are Nautilus nanosecond ints since the Unix epoch, UTC.
"""

from typing import Final

from trading_core.marketdata.hours import HOUR_NS, SECOND_NS, hour_of

# `busy_pct` at or above this logs `recorder_writer_busy` (AC-6).
WRITER_BUSY_WARN_PCT: Final = 25
# A single flush longer than this logs `recorder_flush_slow` (AC-6).
FLUSH_SLOW_WARN_MS: Final = 250
# `recorder_flush_slow` is logged at most once per this long.
FLUSH_SLOW_EVERY_NS: Final = 60 * SECOND_NS
MS_NS: Final = 1_000_000


def next_boundary_ns(now_ns: int) -> int:
    """The rotation boundary of a file opened at ``now_ns``: the next whole UTC hour strictly after it
    (AC-3, fix 1), so a late rotating write never pushes later boundaries off the hour."""
    return hour_of(now_ns) + HOUR_NS


def rotation_due(now_ns: int, boundary_ns: int) -> bool:
    """Whether a write at ``now_ns`` is the first at or after its file's boundary (AC-3)."""
    return now_ns >= boundary_ns


def should_flush(held: int, cap: int) -> bool:
    """Whether a stream holding ``held`` messages flushes now, without waiting for the tick (AC-2)."""
    return held >= cap


def busy_pct(busy_ns: int, span_ns: int) -> int:
    """Time spent writing and flushing, in whole percent of the stats span (AC-6).

    A span of zero or less (no clock time passed) reports 0.
    """
    if span_ns <= 0:
        return 0
    return busy_ns * 100 // span_ns


def busy_warn(pct: int) -> bool:
    """Whether ``pct`` logs ``recorder_writer_busy`` (AC-6)."""
    return pct >= WRITER_BUSY_WARN_PCT


def slow_flush_due(flush_ns: int, *, now_ns: int, last_logged_ns: int | None) -> bool:
    """Whether a flush that took ``flush_ns`` logs ``recorder_flush_slow`` now (AC-6).

    Only a flush over ``FLUSH_SLOW_WARN_MS``, and at most once per ``FLUSH_SLOW_EVERY_NS`` since
    ``last_logged_ns`` on the node clock.
    """
    if flush_ns <= FLUSH_SLOW_WARN_MS * MS_NS:
        return False
    return last_logged_ns is None or now_ns - last_logged_ns >= FLUSH_SLOW_EVERY_NS
