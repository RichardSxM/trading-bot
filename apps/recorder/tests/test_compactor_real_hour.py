"""covers: spec 0007 AC-17 (offline, on real data): the BTC 15:00 hour of 2026-10-08, replayed with the holes
its own counter shows, has no crossed or one sided book outside gaps; with the time only gap rules it does.
Sampled every second rather than every minute: in this hour the minute samples land on the end rows of
`silence` gaps, so only a denser sampling shows the time only rules failing.

Opt in: ``KFS_REAL_HOUR_DIR`` names a folder holding that hour's converted ``PF_XBTUSD.KRAKEN.parquet``
and ``PF_ETHUSD.KRAKEN.parquet`` (copied from the VPS catalog; too big to commit). Recorded with BTC and
ETH on one counter, so the holes come from both files merged.
"""

import bisect
import os
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from recorder.compactor.convert import CLEAR, F_SNAPSHOT, BookCarry
from trading_core.marketdata.gaps import Gap, GapDetector, GapState
from trading_core.marketdata.hours import SECOND_NS
from trading_core.marketdata.sequence import RunHoles, SequenceHole

HOUR = 1_791_471_600 * SECOND_NS  # 2026-10-08 15:00 UTC
BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
# One sample per second (AC-17 asks for one per minute; this is stricter).
SAMPLE_EVERY_NS = SECOND_NS
# A recorder restart starts its counter again near 0.
FRESH_COUNTER = 1_000

FOLDER = os.environ.get("KFS_REAL_HOUR_DIR")
pytestmark = pytest.mark.skipif(
    not FOLDER or not (Path(FOLDER) / f"{BTC}.parquet").exists(), reason="KFS_REAL_HOUR_DIR not set"
)


def _feed_rows(path):
    """The hour's rows without the old compactor's synthetic snapshot at the hour start."""
    table = pq.read_table(str(path))
    flags = table.column("flags").to_numpy()
    ts = table.column("ts_init").to_numpy()
    synthetic = (ts == HOUR) & ((flags & F_SNAPSHOT) != 0)
    return table.filter(pa.array(~synthetic))


def _runs_and_holes(btc, eth):
    """Run start times and each run's holes, from the BTC and ETH counters merged."""
    seq = np.concatenate([btc.column("sequence").to_numpy(), eth.column("sequence").to_numpy()]).astype(np.int64)
    ts = np.concatenate([btc.column("ts_init").to_numpy(), eth.column("ts_init").to_numpy()]).astype(np.int64)
    order = np.lexsort((seq, ts))
    seq, ts = seq[order], ts[order]
    starts = [0] + [i for i in range(1, len(seq)) if seq[i] < seq[i - 1] and seq[i] < FRESH_COUNTER]
    run_starts = [int(ts[i]) for i in starts]
    holes = []
    for a, b in zip(starts, [*starts[1:], len(seq)], strict=True):
        by_seq = np.argsort(seq[a:b], kind="stable") + a
        s, t = seq[by_seq], ts[by_seq]
        jumps = np.flatnonzero(np.diff(s) > 1)
        holes.append(
            RunHoles(
                holes=tuple(
                    sorted(
                        (
                            SequenceHole(
                                int(s[j] + 1), int(s[j + 1] - 1), int(s[j + 1] - s[j] - 1), int(t[j]), int(t[j + 1])
                            )
                            for j in jumps
                        ),
                        key=lambda h: h.before_ns,
                    )
                ),
                closed=True,
            )
        )
    return run_starts, holes


def _replay(table, run_starts, holes):
    """Gap detection and the carried book over BTC's rows, as the compactor runs them; the book
    state at the first row at or after each sample time, and the gaps."""
    ts = table.column("ts_init").to_numpy().astype(np.int64)
    clear = table.column("action").to_numpy() == CLEAR
    runs = np.array([bisect.bisect_right(run_starts, int(t)) - 1 for t in ts])
    times = range(HOUR, HOUR + 3_600 * SECOND_NS, SAMPLE_EVERY_NS)
    sample_rows = {int(i) for i in np.searchsorted(ts, list(times)) if i < len(ts)}
    run_changes = {int(i) for i in np.flatnonzero(np.diff(runs)) + 1}
    ends = sorted({i + 1 for i in sample_rows} | run_changes | {len(ts)})
    detector = GapDetector(BTC, GapState(), silence_ns=10 * SECOND_NS)
    carry = BookCarry(BTC)
    gaps: list[Gap] = []
    samples = []
    pos = 0
    for end in ends:
        run = int(runs[pos])
        if pos > 0 and runs[pos - 1] != run:
            carry.invalidate()  # a restart, as HourStream.feed does
        scan = detector.scan_book(f"run{run}", ts[pos:end].tolist(), clear[pos:end].tolist(), holes[run])
        gaps.extend(scan.gaps)
        carry.apply(table.slice(pos, end - pos), scan)
        pos = end
        if pos - 1 in sample_rows:
            samples.append((int(ts[pos - 1]), _state(carry)))
    gaps.extend(detector.end_run())
    return samples, gaps, ts[clear].tolist()


def _state(carry):
    if not carry.valid:
        return "invalid"
    bids, asks = carry.book.bids(), carry.book.asks()
    if not bids or not asks:
        return "one_sided"
    return "crossed" if Decimal(str(bids[0].price)) >= Decimal(str(asks[0].price)) else "ok"


def _bad(samples, gaps, clears):
    """Crossed or one sided samples outside gaps, the first 1 s after a feed CLEAR, and before the
    first valid book."""
    bad = []
    for ts, state in samples:
        if state in ("ok", "invalid"):
            continue
        if any(g.start_ns <= ts <= g.end_ns for g in gaps):
            continue
        i = bisect.bisect_right(clears, ts)
        if i and ts - clears[i - 1] <= SECOND_NS:
            continue
        bad.append((ts, state))
    return bad


def test_the_real_hour_has_no_bad_book_outside_gaps_once_holes_are_gaps():
    folder = Path(FOLDER or "")
    btc = _feed_rows(folder / f"{BTC}.parquet")
    eth = _feed_rows(folder / f"{ETH}.parquet")
    run_starts, holes = _runs_and_holes(btc, eth)
    assert sum(len(h.holes) for h in holes) > 0  # the hour is known to hold holes

    without = _bad(*_replay(btc, run_starts, [None] * len(holes)))
    samples, gaps, clears = _replay(btc, run_starts, holes)
    with_holes = _bad(samples, gaps, clears)

    print(f"runs {len(run_starts)}, holes {sum(len(h.holes) for h in holes)}, samples {len(samples)}")
    print(f"bad without holes {len(without)}, first {without[:3]}")
    assert without, "the time only rules should let a bad book through in this hour"
    assert with_holes == []
    assert any(g.kind == "sequence" for g in gaps)
