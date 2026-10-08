"""Gap detection over one instrument's book delta stream (spec 0007 AC-4).

Three kinds, each from the ``ts_init`` of neighbouring rows:

- ``recorder_down``: from the last row of one run to the first row of the next run.
- ``reconnect``: a feed ``CLEAR`` after the run's first one, from the previous row to the ``CLEAR``.
- ``silence``: two rows of one run ``silence_ns`` or more apart, unless that span is a ``reconnect``.

The detector streams across chunks and passes; its state is kept in ``done.json``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

from trading_core.marketdata.hours import SECOND_NS

GapKind = Literal["reconnect", "recorder_down", "silence"]
GAP_KINDS: Final[tuple[GapKind, ...]] = get_args(GapKind)


@dataclass(frozen=True, order=True)
class Gap:
    """One gap. Gaps sort by start, then instrument and kind, as the gap files keep them."""

    start_ns: int
    instrument_id: str
    kind: GapKind
    end_ns: int

    @property
    def duration_s(self) -> float:
        """The gap's length in seconds."""
        return (self.end_ns - self.start_ns) / SECOND_NS


@dataclass(frozen=True)
class GapState:
    """Where detection stands for one instrument: the last row seen, its run, and whether that
    run's first ``CLEAR`` has passed."""

    last_ts: int | None = None
    run: str | None = None
    cleared: bool = False

    def to_dict(self) -> dict[str, object]:
        """The state as ``done.json`` stores it."""
        return {"last_ts": self.last_ts, "run": self.run, "cleared": self.cleared}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> GapState:
        """Parse a state written by ``to_dict``."""
        last_ts = raw.get("last_ts")
        run = raw.get("run")
        return cls(
            last_ts=last_ts if isinstance(last_ts, int) else None,
            run=run if isinstance(run, str) else None,
            cleared=raw.get("cleared") is True,
        )


class GapDetector:
    """Detects the gaps of one instrument, one chunk of rows at a time."""

    def __init__(self, instrument_id: str, state: GapState, *, silence_ns: int) -> None:
        self.instrument_id = instrument_id
        self._silence_ns = silence_ns
        self._last_ts = state.last_ts
        self._run = state.run
        self._cleared = state.cleared

    @property
    def state(self) -> GapState:
        """The state after the rows scanned so far."""
        return GapState(last_ts=self._last_ts, run=self._run, cleared=self._cleared)

    def scan(self, run: str, ts_init: Sequence[int], is_clear: Sequence[bool]) -> list[Gap]:
        """Scan rows of ``run`` in stream order and return the gaps they close."""
        if not ts_init:
            return []
        gaps: list[Gap] = []
        last, cleared = self._last_ts, self._cleared
        if run != self._run:
            if last is not None:
                gaps.append(Gap(last, self.instrument_id, "recorder_down", ts_init[0]))
                last = None  # the new run's first row starts fresh
            cleared = False
        for ts, clear in zip(ts_init, is_clear, strict=True):
            if last is not None:
                if clear and cleared:
                    gaps.append(Gap(last, self.instrument_id, "reconnect", ts))
                elif ts - last >= self._silence_ns:
                    gaps.append(Gap(last, self.instrument_id, "silence", ts))
            cleared = cleared or clear
            last = ts
        self._last_ts, self._run, self._cleared = last, run, cleared
        return gaps


def gap_kind(value: str) -> GapKind:
    """A gap kind read back from a gap file; raises ``ValueError`` for anything else."""
    if value not in GAP_KINDS:
        raise ValueError(f"unknown gap kind {value!r}")
    return value


def seconds_within(gap: Gap, start_ns: int, end_ns: int) -> float:
    """The seconds of ``gap`` that lie in ``[start_ns, end_ns)``, such as one UTC day."""
    overlap = min(gap.end_ns, end_ns) - max(gap.start_ns, start_ns)
    return max(overlap, 0) / SECOND_NS
