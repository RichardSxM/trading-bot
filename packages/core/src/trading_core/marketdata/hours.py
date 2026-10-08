"""Hour math, the ready rule, and the reorder window rule for the hourly catalog (spec 0007).

Times are Nautilus nanosecond ints since the Unix epoch, UTC. An hour is named by its start.
"""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal

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
    watermark: int | None,
    run_ended: bool,
) -> bool:
    """Whether one stream's hour that starts at ``hour`` can be finished (spec 0007 AC-12).

    Ready once ``now`` is past its end plus the grace, and the stream's ``watermark`` has passed
    the end (so no row of the hour inside the reorder window can still arrive), or the run that
    wrote it has ended.
    """
    end = hour + HOUR_NS
    if now_ns < end + grace_ns:
        return False
    return run_ended or (watermark is not None and watermark >= end)


LateCause = Literal["window", "run_overlap"]


@dataclass(frozen=True)
class WatermarkState:
    """Where one stream's reorder window stands (spec 0007 AC-12).

    ``watermark`` is the newest ``ts_init`` read minus the window, and never goes down; rows at or
    below it are written, rows read below it are late. ``newest_ns`` and ``newest_run`` are the
    newest ``ts_init`` read and the run it came from, which sets a late row's cause.
    """

    watermark: int | None = None
    newest_ns: int | None = None
    newest_run: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The state as ``done.json`` stores it."""
        return {"watermark": self.watermark, "newest_ns": self.newest_ns, "newest_run": self.newest_run}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> WatermarkState:
        """Parse a state written by ``to_dict``."""
        watermark, newest, run = raw.get("watermark"), raw.get("newest_ns"), raw.get("newest_run")
        return cls(
            watermark=watermark if isinstance(watermark, int) else None,
            newest_ns=newest if isinstance(newest, int) else None,
            newest_run=run if isinstance(run, str) else None,
        )


@dataclass(frozen=True)
class RowScan:
    """The result of ``scan_rows``, one entry per row in file order.

    ``causes`` is ``None`` for a row in the window. ``lateness_ns`` is the newest ``ts_init`` read
    before the row minus its own, or 0 when the row is not behind.
    """

    causes: list[LateCause | None]
    lateness_ns: list[int]
    state: WatermarkState


def scan_rows(ts_init: Sequence[int], run: str, state: WatermarkState, *, window_ns: int) -> RowScan:
    """Classify rows of ``run``, read in file order, as in window or late (spec 0007 AC-12).

    A row is late when its ``ts_init`` is below the watermark as it stood before the row was read.
    Its cause is ``window`` when the newest row read came from the same run, else ``run_overlap``.
    The result depends only on the rows and the starting state, never on how they are chunked.
    """
    watermark, newest, newest_run = state.watermark, state.newest_ns, state.newest_run
    causes: list[LateCause | None] = []
    lateness: list[int] = []
    for ts in ts_init:
        if watermark is not None and ts < watermark:
            causes.append("window" if newest_run == run else "run_overlap")
        else:
            causes.append(None)
        lateness.append(newest - ts if newest is not None and ts < newest else 0)
        if newest is None or ts > newest:
            newest, newest_run = ts, run
            floor = newest - window_ns
            watermark = floor if watermark is None else max(watermark, floor)
    return RowScan(causes, lateness, WatermarkState(watermark, newest, newest_run))


def end_run(state: WatermarkState) -> WatermarkState:
    """The state once a run has ended and every held row is written (spec 0007 AC-12).

    The watermark rises to the newest row read, so nothing written later goes below the rows
    just written; rows of a newer run that go back past them are ``run_overlap`` late rows.
    """
    if state.newest_ns is None:
        return state
    watermark = state.newest_ns if state.watermark is None else max(state.watermark, state.newest_ns)
    return WatermarkState(watermark, state.newest_ns, state.newest_run)


def cap_watermark(oldest_held_ts: Sequence[int], held_rows: int, watermark: int | None, max_rows: int) -> int | None:
    """The watermark after the row cap (spec 0007 AC-12).

    While more than ``max_rows`` of the ``held_rows`` are held, the watermark moves up to the
    oldest held row's ``ts_init``; that ends with at most ``max_rows`` held (rows sharing a
    ``ts_init`` go together). ``oldest_held_ts`` holds, ascending, at least the oldest
    ``held_rows - max_rows`` held rows' ``ts_init``.
    """
    excess = held_rows - max_rows
    if excess <= 0:
        return watermark
    raised = oldest_held_ts[excess - 1]
    return raised if watermark is None else max(watermark, raised)


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
