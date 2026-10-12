"""Gap detection over one instrument's book delta stream (spec 0007 AC-4, AC-16).

Four kinds, from the ``ts_init`` of neighbouring rows and the run's holes file:

- ``recorder_down``: from the last row of one run to the first row of the next run.
- ``sequence``: a hole in the run's message counter, from the hole's ``before_ns`` to the first
  feed ``CLEAR`` at or after its ``after_ns`` (or the run's last row). The book is not trusted in
  between.
- ``reconnect``: a feed ``CLEAR`` after the run's first one, from the previous row to the ``CLEAR``.
- ``silence``: two rows of one run ``silence_ns`` or more apart.

Where spans overlap, ``sequence`` wins over ``reconnect``, which wins over ``silence``. The detector
streams across chunks and passes; its state is kept in ``done.json``.

Each gap names its ``stream`` (the catalog table): the book's own gaps are ``order_book_deltas``,
and the market context streams have theirs (spec 0009 AC-7).
"""

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import total_ordering
from typing import Final, Literal, get_args

from trading_core.marketdata.hours import SECOND_NS
from trading_core.marketdata.sequence import UNKNOWN, RunHoles

GapKind = Literal["reconnect", "recorder_down", "silence", "sequence"]
GAP_KINDS: Final[tuple[GapKind, ...]] = get_args(GapKind)
# The book's stream, which every gap had before the context streams (``DELTAS_TABLE``'s name).
BOOK_STREAM: Final = "order_book_deltas"


@total_ordering
@dataclass(frozen=True)
class Gap:
    """One gap. Gaps sort by start, then stream, instrument, and kind, as the gap files keep them.

    ``missing`` is the count of lost messages of a ``sequence`` gap (``UNKNOWN`` for a fail safe
    one), for the log only: the gap files do not store it.
    """

    start_ns: int
    instrument_id: str
    kind: GapKind
    end_ns: int
    missing: int | None = field(default=None, compare=False)
    stream: str = BOOK_STREAM

    def __lt__(self, other: Gap) -> bool:
        return self._key() < other._key()

    def _key(self) -> tuple[int, str, str, str, int]:
        return (self.start_ns, self.stream, self.instrument_id, self.kind, self.end_ns)

    @property
    def duration_s(self) -> float:
        """The gap's length in seconds."""
        return (self.end_ns - self.start_ns) / SECOND_NS


@dataclass(frozen=True)
class PendingSequence:
    """A ``sequence`` gap still waiting for its ``CLEAR``: its start, the time the ``CLEAR`` must be
    at or after, and the lost message count so far."""

    start_ns: int
    after_ns: int
    missing: int

    def merge(self, after_ns: int, missing: int) -> PendingSequence:
        """Another hole fell inside this gap: wait for the later ``CLEAR``, and add its count."""
        total = UNKNOWN if UNKNOWN in (self.missing, missing) else self.missing + missing
        return PendingSequence(self.start_ns, max(self.after_ns, after_ns), total)


@dataclass(frozen=True)
class GapState:
    """Where detection stands for one instrument: the last row seen, its run, whether that run's
    first ``CLEAR`` has passed, and a ``sequence`` gap still open."""

    last_ts: int | None = None
    run: str | None = None
    cleared: bool = False
    sequence: PendingSequence | None = None

    def to_dict(self) -> dict[str, object]:
        """The state as ``done.json`` stores it."""
        raw: dict[str, object] = {"last_ts": self.last_ts, "run": self.run, "cleared": self.cleared}
        if self.sequence is not None:
            raw["sequence"] = {
                "start_ns": self.sequence.start_ns,
                "after_ns": self.sequence.after_ns,
                "missing": self.sequence.missing,
            }
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> GapState:
        """Parse a state written by ``to_dict``."""
        last_ts = raw.get("last_ts")
        run = raw.get("run")
        pending = raw.get("sequence")
        sequence = None
        if isinstance(pending, Mapping):
            start, after, missing = pending.get("start_ns"), pending.get("after_ns"), pending.get("missing")
            if isinstance(start, int) and isinstance(after, int) and isinstance(missing, int):
                sequence = PendingSequence(start, after, missing)
        return cls(
            last_ts=last_ts if isinstance(last_ts, int) else None,
            run=run if isinstance(run, str) else None,
            cleared=raw.get("cleared") is True,
            sequence=sequence,
        )


# Within one stream, a gap of a lower rank wins over an overlapping gap of a higher one (spec 0007
# AC-16, spec 0009 AC-7). The book's own detector never makes overlapping gaps.
_GAP_RANK: Final[dict[GapKind, int]] = {"recorder_down": 0, "sequence": 1, "reconnect": 2, "silence": 3}


def resolve_overlaps(gaps: Sequence[Gap]) -> list[Gap]:
    """The gaps without any that overlap a stronger gap of the same instrument and stream, sorted.

    ``recorder_down`` wins over ``sequence``, which wins over ``reconnect``, which wins over
    ``silence``; the weaker gap is dropped whole.
    """
    kept: list[Gap] = []
    for gap in gaps:
        rank = _GAP_RANK[gap.kind]
        if not any(
            _GAP_RANK[other.kind] < rank
            and other.instrument_id == gap.instrument_id
            and other.stream == gap.stream
            and other.start_ns < gap.end_ns
            and gap.start_ns < other.end_ns
            for other in gaps
        ):
            kept.append(gap)
    return sorted(kept)


@dataclass(frozen=True)
class SilenceState:
    """Where silence detection stands for one context stream: the last row seen and its run."""

    last_ts: int | None = None
    run: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The state as ``done.json`` stores it."""
        return {"last_ts": self.last_ts, "run": self.run}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SilenceState:
        """Parse a state written by ``to_dict``."""
        last_ts, run = raw.get("last_ts"), raw.get("run")
        return cls(last_ts if isinstance(last_ts, int) else None, run if isinstance(run, str) else None)


class SilenceDetector:
    """``silence`` gaps of one context stream (mark, index, open interest): two rows of one run
    ``silence_ns`` or more apart (spec 0009 AC-7). Gaps between runs are the book's ``recorder_down``."""

    def __init__(self, instrument_id: str, stream: str, state: SilenceState, *, silence_ns: int) -> None:
        self.instrument_id = instrument_id
        self.stream = stream
        self._silence_ns = silence_ns
        self._last_ts = state.last_ts
        self._run = state.run

    @property
    def state(self) -> SilenceState:
        """The state to store, so detection resumes in the next pass."""
        return SilenceState(self._last_ts, self._run)

    def scan(self, run: str, ts_init: Sequence[int]) -> list[Gap]:
        """The ``silence`` gaps a chunk of one run's rows (in ``ts_init`` order) closes."""
        gaps: list[Gap] = []
        last = self._last_ts if self._run == run else None
        for ts in ts_init:
            if last is not None and ts - last >= self._silence_ns:
                gaps.append(Gap(last, self.instrument_id, "silence", ts, stream=self.stream))
            last = ts
        if ts_init:
            self._last_ts, self._run = last, run
        return gaps


@dataclass(frozen=True)
class BookScan:
    """The gaps a chunk of rows closed, and where the carried book must stop and start again.

    ``blocked`` says the book was already untrusted at the chunk's first row (a ``sequence`` gap
    open). ``cuts`` are the row indexes where a ``sequence`` gap starts, so the book stops being
    applied there; ``resumes`` are the indexes of the ``CLEAR`` rows that end one, where the book
    starts again. A cut and a resume can share an index; the cut comes first.
    """

    gaps: list[Gap]
    blocked: bool = False
    cuts: list[int] = field(default_factory=list)
    resumes: list[int] = field(default_factory=list)


class GapDetector:
    """Detects the gaps of one instrument, one chunk of rows at a time."""

    def __init__(self, instrument_id: str, state: GapState, *, silence_ns: int) -> None:
        self.instrument_id = instrument_id
        self._silence_ns = silence_ns
        self._last_ts = state.last_ts
        self._run = state.run
        self._cleared = state.cleared
        self._pending = state.sequence

    @property
    def state(self) -> GapState:
        """The state after the rows scanned so far."""
        return GapState(last_ts=self._last_ts, run=self._run, cleared=self._cleared, sequence=self._pending)

    @property
    def pending(self) -> PendingSequence | None:
        """The ``sequence`` gap still waiting for its ``CLEAR``, if any."""
        return self._pending

    def scan(
        self, run: str, ts_init: Sequence[int], is_clear: Sequence[bool], holes: RunHoles | None = None
    ) -> list[Gap]:
        """Scan rows of ``run`` in stream order and return the gaps they close."""
        return self.scan_book(run, ts_init, is_clear, holes).gaps

    def scan_book(
        self, run: str, ts_init: Sequence[int], is_clear: Sequence[bool], holes: RunHoles | None = None
    ) -> BookScan:
        """Scan rows of ``run`` in stream order, with the run's holes read so far.

        A hole starts a ``sequence`` gap at the first row above its ``before_ns`` (at the run's first
        row when the hole lies before it). Holes the rows scanned before already passed are not
        looked at again.
        """
        if not ts_init:
            return BookScan([], blocked=self._pending is not None)
        gaps: list[Gap] = []
        if run != self._run:
            gaps.extend(self.end_run())
            if self._last_ts is not None:
                gaps.append(Gap(self._last_ts, self.instrument_id, "recorder_down", ts_init[0]))
            self._last_ts, self._cleared = None, False  # the new run's first row starts fresh
        last, cleared, pending = self._last_ts, self._cleared, self._pending
        result = BookScan(gaps, blocked=pending is not None)
        found = holes.holes if holes is not None else ()
        befores = [h.before_ns for h in found]
        h = 0 if last is None else bisect.bisect_left(befores, last)
        for i, (ts, clear) in enumerate(zip(ts_init, is_clear, strict=True)):
            while h < len(found) and befores[h] < ts:
                hole = found[h]
                h += 1
                if pending is None:
                    start = hole.before_ns if last is not None else ts
                    pending = PendingSequence(start, hole.after_ns, hole.missing)
                    result.cuts.append(i)
                else:
                    pending = pending.merge(hole.after_ns, hole.missing)
            if pending is not None:
                if clear and ts >= pending.after_ns:
                    gaps.append(Gap(pending.start_ns, self.instrument_id, "sequence", ts, pending.missing))
                    result.resumes.append(i)
                    pending = None
            elif last is not None:
                if clear and cleared:
                    gaps.append(Gap(last, self.instrument_id, "reconnect", ts))
                elif ts - last >= self._silence_ns:
                    gaps.append(Gap(last, self.instrument_id, "silence", ts))
            cleared = cleared or clear
            last = ts
        self._last_ts, self._run, self._cleared, self._pending = last, run, cleared, pending
        return result

    def end_run(self) -> list[Gap]:
        """The run scanned last has ended: a ``sequence`` gap still open ends at its last row."""
        pending, self._pending = self._pending, None
        if pending is None or self._last_ts is None:
            return []
        return [Gap(pending.start_ns, self.instrument_id, "sequence", self._last_ts, pending.missing)]


def gap_kind(value: str) -> GapKind:
    """A gap kind read back from a gap file; raises ``ValueError`` for anything else."""
    if value not in GAP_KINDS:
        raise ValueError(f"unknown gap kind {value!r}")
    return value


def seconds_within(gap: Gap, start_ns: int, end_ns: int) -> float:
    """The seconds of ``gap`` that lie in ``[start_ns, end_ns)``, such as one UTC day."""
    overlap = min(gap.end_ns, end_ns) - max(gap.start_ns, start_ns)
    return max(overlap, 0) / SECOND_NS
