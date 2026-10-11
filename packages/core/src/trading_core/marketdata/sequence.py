"""Holes in the recorder run's message counter (spec 0007 AC-15, AC-16).

Every book delta of a recorder run carries ``sequence``: one counter per run, starting at 1, shared
by every instrument, counting through a websocket reconnect. A number that never reaches the
recorder is a lost message. The recorder's ``SequenceTracker`` finds those holes and writes them to
the run's ``sequence_holes.jsonl``; the compactor parses that file with ``parse_holes`` and turns
each hole into a ``sequence`` gap (``gaps.py``).

Times are Nautilus nanosecond ints. ``ts_init`` and the recorder actor's clock are the same live
node clock, so they compare directly.
"""

import bisect
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from trading_core.marketdata.hours import HOUR_NS

# `missing`, `first_missing`, and `last_missing` of a fail safe hole: the count is unknown.
UNKNOWN: Final = -1
# `after_ns` of a fail safe hole: no CLEAR can end it, so it runs to each instrument's last row.
NEVER_NS: Final = 2**63 - 1


@dataclass(frozen=True)
class SequenceHole:
    """Numbers ``first_missing`` to ``last_missing`` never arrived (AC-15).

    ``before_ns`` is the ``ts_init`` of the delta numbered just below the hole, ``after_ns`` that of
    the delta just above it. A fail safe hole (AC-16) has ``UNKNOWN`` counts and ``after_ns``
    ``NEVER_NS``; ``before_ns`` 0 means the start of the run.
    """

    first_missing: int
    last_missing: int
    missing: int
    before_ns: int
    after_ns: int

    def to_dict(self) -> dict[str, int]:
        """The hole as one line of the holes file."""
        return {
            "first_missing": self.first_missing,
            "last_missing": self.last_missing,
            "missing": self.missing,
            "before_ns": self.before_ns,
            "after_ns": self.after_ns,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SequenceHole:
        """Parse a hole written by ``to_dict``; raises ``ValueError`` on a missing or wrong field."""
        values: dict[str, int] = {}
        for name in ("first_missing", "last_missing", "missing", "before_ns", "after_ns"):
            value = raw.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"hole field {name!r} is not an int: {value!r}")
            values[name] = value
        return cls(**values)


@dataclass(frozen=True)
class TickResult:
    """What one tick of the tracker found.

    ``holes`` were confirmed since the last tick. Every hole with ``after_ns`` below
    ``checked_to_ns`` has been confirmed by this tick or an earlier one.
    """

    holes: list[SequenceHole]
    checked_to_ns: int


class SequenceTracker:
    """Finds holes in one recorder run's message counter (AC-15).

    Feed every delta in arrival order with ``observe``, and call ``tick`` on a timer. A number
    counts as missing once ``confirm_ns`` has passed since the first higher number arrived; one
    that arrives sooner was only overtaken. The tracker starts at the first number it sees, and
    ignores numbers below that. Seen numbers above the lowest unseen one are kept as runs of
    consecutive numbers, so a long hole costs no memory.
    """

    def __init__(self, *, confirm_ns: int) -> None:
        self._confirm_ns = confirm_ns
        # Every number up to `_floor` is seen, ignored, or in a confirmed hole; `_floor_ts` is the
        # ts_init of number `_floor`.
        self._floor: int | None = None
        self._floor_ts = 0
        # Runs of seen numbers above the floor, sorted, with a suspected hole below each one.
        # `_since[i]` is when the first number above the hole below run i arrived.
        self._lo: list[int] = []
        self._hi: list[int] = []
        self._lo_ts: list[int] = []
        self._hi_ts: list[int] = []
        self._since: list[int] = []
        # Confirmed holes as (first_missing, last_missing), to recognise a late arrival.
        self._confirmed_first: list[int] = []
        self._confirmed_last: list[int] = []

    def observe(self, sequence: int, ts_init: int, now_ns: int) -> bool:
        """Take one delta's number. Returns True when it arrived after its hole was confirmed."""
        floor = self._floor
        if floor is None:
            self._floor, self._floor_ts = sequence, ts_init
            return False
        if not self._lo:
            if sequence == floor + 1:  # the common case: the next number, nothing suspected
                self._floor, self._floor_ts = sequence, ts_init
                return False
            if sequence <= floor:
                return self._is_confirmed(sequence)
            self._append(sequence, ts_init, now_ns)
            return False
        if sequence <= floor:
            return self._is_confirmed(sequence)
        last = len(self._hi) - 1
        if sequence == self._hi[last] + 1:
            self._hi[last], self._hi_ts[last] = sequence, ts_init
        elif sequence > self._hi[last]:
            self._append(sequence, ts_init, now_ns)
        else:
            self._fill(sequence, ts_init)
        return False

    def tick(self, now_ns: int) -> TickResult:
        """Confirm the suspicions old enough, and say how far the holes are all known."""
        holes: list[SequenceHole] = []
        cutoff = now_ns - self._confirm_ns
        while self._lo and self._since[0] <= cutoff:
            holes.append(self._confirm_first())
        checked = cutoff if not self._lo else min(cutoff, self._floor_ts)
        return TickResult(holes, checked)

    def close(self) -> list[SequenceHole]:
        """At stop, every open suspicion becomes a hole (fail safe)."""
        return [self._confirm_first() for _ in range(len(self._lo))]

    def _append(self, sequence: int, ts_init: int, now_ns: int) -> None:
        self._lo.append(sequence)
        self._hi.append(sequence)
        self._lo_ts.append(ts_init)
        self._hi_ts.append(ts_init)
        self._since.append(now_ns)

    def _fill(self, sequence: int, ts_init: int) -> None:
        """A number inside a suspected hole arrived (it was overtaken)."""
        assert self._floor is not None
        i = bisect.bisect_right(self._lo, sequence)
        below = self._hi[i - 1] if i > 0 else self._floor
        if sequence <= below:
            return  # seen already
        if i < len(self._lo) and sequence >= self._lo[i]:
            return  # seen already: inside run i
        touches_below = sequence == below + 1
        touches_above = sequence == self._lo[i] - 1
        if touches_below and touches_above:  # the hole closes: join the runs on either side
            if i == 0:
                self._floor, self._floor_ts = self._hi[0], self._hi_ts[0]
            else:
                self._hi[i - 1], self._hi_ts[i - 1] = self._hi[i], self._hi_ts[i]
            self._pop(i)
        elif touches_below:
            if i == 0:
                self._floor, self._floor_ts = sequence, ts_init
            else:
                self._hi[i - 1], self._hi_ts[i - 1] = sequence, ts_init
        elif touches_above:
            self._lo[i], self._lo_ts[i] = sequence, ts_init
        else:  # splits the hole; both parts keep its start time
            self._lo.insert(i, sequence)
            self._hi.insert(i, sequence)
            self._lo_ts.insert(i, ts_init)
            self._hi_ts.insert(i, ts_init)
            self._since.insert(i, self._since[i])

    def _confirm_first(self) -> SequenceHole:
        assert self._floor is not None
        first, last = self._floor + 1, self._lo[0] - 1
        hole = SequenceHole(first, last, last - first + 1, self._floor_ts, self._lo_ts[0])
        self._confirmed_first.append(first)
        self._confirmed_last.append(last)
        self._floor, self._floor_ts = self._hi[0], self._hi_ts[0]
        self._pop(0)
        return hole

    def _pop(self, i: int) -> None:
        for column in (self._lo, self._hi, self._lo_ts, self._hi_ts, self._since):
            del column[i]

    def _is_confirmed(self, sequence: int) -> bool:
        i = bisect.bisect_right(self._confirmed_first, sequence) - 1
        return i >= 0 and sequence <= self._confirmed_last[i]


@dataclass(frozen=True)
class RunHoles:
    """A run's holes file as read so far (AC-16).

    ``checked_to_ns`` is the last one written, ``None`` before the first tick. ``complete_lines``
    counts the lines read; 0 means the file holds no complete line.
    """

    holes: tuple[SequenceHole, ...] = ()
    checked_to_ns: int | None = None
    closed: bool = False
    complete_lines: int = 0
    bad_lines: int = 0

    def with_fail_safe(self) -> RunHoles:
        """The holes of a run that ended without ``closed``: one more hole over its unchecked tail.

        The tail starts at the last ``checked_to_ns``, or at the start of the run when no tick was
        written. An ended run with ``closed`` gets nothing more.
        """
        if self.closed:
            return self
        start = self.checked_to_ns if self.checked_to_ns is not None else 0
        tail = SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, start, NEVER_NS)
        return RunHoles(
            holes=(*self.holes, tail),
            checked_to_ns=self.checked_to_ns,
            closed=self.closed,
            complete_lines=self.complete_lines,
            bad_lines=self.bad_lines,
        )


def parse_holes(lines: Iterable[str]) -> RunHoles:
    """Parse the complete lines of a holes file.

    A line that is not valid JSON of a known shape is counted in ``bad_lines``, and since it may
    have held a hole, a fail safe hole replaces it: from the last ``checked_to_ns`` before it (the
    start of the run when none) to the first one after it. With no ``checked_to_ns`` after it, the
    hole reaches ``NEVER_NS`` once the file says ``closed``; until then rows above the last
    ``checked_to_ns`` wait anyway, and the next tick bounds it.
    """
    holes: list[SequenceHole] = []
    checked: int | None = None
    closed = False
    count = bad = 0
    bad_from: int | None = None  # where the fail safe hole of unresolved bad lines starts

    def mark_bad() -> None:
        nonlocal bad, bad_from
        bad += 1
        if bad_from is None:
            bad_from = checked if checked is not None else 0

    for line in lines:
        count += 1
        try:
            raw = json.loads(line)
        except ValueError:
            mark_bad()
            continue
        if not isinstance(raw, dict):
            mark_bad()
        elif "checked_to_ns" in raw and isinstance(raw["checked_to_ns"], int):
            checked = raw["checked_to_ns"]
            if bad_from is not None:
                holes.append(SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, bad_from, checked))
                bad_from = None
        elif raw.get("closed") is True:
            closed = True
        elif "started_ns" in raw:
            pass
        else:
            try:
                holes.append(SequenceHole.from_dict(raw))
            except ValueError:
                mark_bad()
    if bad_from is not None and closed:
        holes.append(SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, bad_from, NEVER_NS))
    holes.sort(key=lambda h: h.before_ns)
    return RunHoles(tuple(holes), checked, closed, count, bad)


def holes_ready(hour: int, holes: RunHoles | None, *, run_ended: bool) -> bool:
    """Whether a run's holes file covers the hour that starts at ``hour`` (AC-16).

    Covered once ``checked_to_ns`` reaches the hour's end, or the file says ``closed``, or the run
    has ended (its fail safe hole covers the rest). A run without a holes file was recorded before
    the sequence watch shipped, so it never waits.
    """
    if holes is None or run_ended or holes.closed:
        return True
    return holes.checked_to_ns is not None and holes.checked_to_ns >= hour + HOUR_NS


def release_limit(holes: RunHoles | None, *, run_ended: bool) -> int | None:
    """The newest ``ts_init`` the compactor may write for a run now, or ``None`` for no limit.

    Rows above ``checked_to_ns`` might sit after a hole the file does not hold yet, so they wait in
    the reorder window; this keeps a rerun's gaps and book the same as the first pass's (AC-11).
    """
    if holes is None or run_ended or holes.closed:
        return None
    return holes.checked_to_ns if holes.checked_to_ns is not None else -1
