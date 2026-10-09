"""The reorder window that puts out of order rows back in ``ts_init`` order (spec 0007 AC-12)."""

import logging
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from trading_core.marketdata.hours import (
    HOUR_NS,
    LateCause,
    WatermarkState,
    cap_watermark,
    end_run,
    hour_of,
    scan_rows,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LateRows:
    """Rows read below the watermark, with each row's cause and lateness in ns (AC-12)."""

    rows: Any
    causes: list[LateCause]
    lateness_ns: list[int]


class ReorderWindow:
    """One stream's rows read but not yet written, in stable ``ts_init`` order (spec 0007 AC-12).

    ``push`` splits off late rows, holds the rest, and releases the rows at or below the
    watermark. ``moved`` and ``max_late_ns`` collect, per hour, the rows put back in place and the
    largest lateness of a row that was still in the window.
    """

    def __init__(self, key: str, *, window_ns: int, max_rows: int) -> None:
        self.key = key
        self._window_ns = window_ns
        self._max_rows = max_rows
        self.state = WatermarkState()
        self._held: Any | None = None
        self._held_max = 0
        self.moved: dict[int, int] = {}
        self.max_late_ns: dict[int, int] = {}
        self.capped_logged = False
        # The newest ts_init that may be released now, from the run's holes file (AC-16).
        self.limit: int | None = None

    @property
    def held_rows(self) -> int:
        """Rows held in the window now."""
        return 0 if self._held is None else int(self._held.num_rows)

    def push(self, chunk: Any, run: str) -> tuple[Any | None, LateRows | None]:
        """Take a chunk of ``run`` in file order. Returns the released rows and the late rows."""
        ts = chunk.column("ts_init").to_pylist()
        scan = scan_rows(ts, run, self.state, window_ns=self._window_ns)
        self.state = scan.state
        late = None
        good = chunk
        flags = [cause is not None for cause in scan.causes]
        if any(flags):
            mask = pa.array(flags)
            pairs = [(c, n) for c, n in zip(scan.causes, scan.lateness_ns, strict=True) if c is not None]
            late = LateRows(chunk.filter(mask), [c for c, _ in pairs], [n for _, n in pairs])
            good = chunk.filter(pc.invert(mask))
        if any(scan.lateness_ns):
            for t, n, cause in zip(ts, scan.lateness_ns, scan.causes, strict=True):
                if n and cause is None:
                    hour = hour_of(t)
                    self.max_late_ns[hour] = max(self.max_late_ns.get(hour, 0), n)
        if good.num_rows:
            self._hold(good)
        return self._release(), late

    def release(self) -> Any | None:
        """The rows the watermark and the holes file's limit let go now, without new rows read."""
        return self._release()

    def end_run(self) -> Any | None:
        """The run has ended: release every held row and raise the watermark past them."""
        self.state = end_run(self.state)
        held, self._held = self._held, None
        return held if held is not None and held.num_rows else None

    def _hold(self, good: Any) -> None:
        ts = good.column("ts_init").to_numpy()
        ordered = bool((ts[1:] >= ts[:-1]).all())
        if self._held is None or self._held.num_rows == 0:
            if ordered:
                self._held, self._held_max = good, int(ts[-1])
                return
            table = good
        elif ordered and int(ts[0]) >= self._held_max:
            self._held, self._held_max = pa.concat_tables([self._held, good]), int(ts[-1])
            return
        else:
            table = pa.concat_tables([self._held, good])  # [held, new]: file order breaks ties
        indices = pc.sort_indices(table, sort_keys=[("ts_init", "ascending")])  # stable
        order = indices.to_numpy()
        moved = order != np.arange(order.size)
        if moved.any():
            table = table.take(indices)
            sorted_ts = table.column("ts_init").to_numpy()[moved]
            hours, counts = np.unique(sorted_ts - sorted_ts % HOUR_NS, return_counts=True)
            for hour, count in zip(hours.tolist(), counts.tolist(), strict=True):
                self.moved[hour] = self.moved.get(hour, 0) + count
        self._held = table
        self._held_max = int(table.column("ts_init")[table.num_rows - 1].as_py())

    def _release(self) -> Any | None:
        held = self._held
        watermark = self.state.watermark
        if held is None or held.num_rows == 0 or watermark is None:
            return None
        ts = held.column("ts_init").to_numpy()
        if held.num_rows > self._max_rows:
            excess = held.num_rows - self._max_rows
            watermark = cap_watermark(ts[:excess].tolist(), held.num_rows, watermark, self._max_rows)
            self.state = replace(self.state, watermark=watermark)
            if not self.capped_logged:
                self.capped_logged = True
                log.warning(
                    "catalog window capped",
                    extra={"fields": {"code": "catalog_window_capped", "stream": self.key, "held_rows": held.num_rows}},
                )
        assert watermark is not None
        if self.limit is not None and held.num_rows <= self._max_rows:
            # Rows past the holes file's checked time wait (AC-16). The row cap still wins: memory
            # first, then a rerun may see a hole the first pass missed (logged as capped above).
            watermark = min(watermark, self.limit)
        n = int(np.searchsorted(ts, watermark, side="right"))
        if n == 0:
            return None
        self._held = held.slice(n)
        return held.slice(0, n)
