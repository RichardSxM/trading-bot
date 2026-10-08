"""Gap files and the daily manifest (spec 0007 AC-4, AC-5).

Gaps go to ``gaps/<day>.parquet`` by the UTC day of their start, merged so a rerun gives the same
rows. A day is closed once every hour of it is accounted for; its manifest then sums the per hour
stats in ``done.json`` and the gap seconds that fall inside the day.
"""

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from functools import partial
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from recorder.compactor.store import COMPRESSION, DELTAS_TABLE, CatalogStore, DoneRecord, atomic_write
from trading_core.marketdata.gaps import Gap, gap_kind, seconds_within
from trading_core.marketdata.hours import DAY_NS, day_closed, day_name, day_of

log = logging.getLogger(__name__)

GAP_SCHEMA: Final = pa.schema(
    [
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("start_ns", pa.uint64(), nullable=False),
        pa.field("end_ns", pa.uint64(), nullable=False),
    ]
)


def read_gaps(store: CatalogStore, day: str) -> list[Gap]:
    """The gaps that start on ``day``, or none when it has no gap file."""
    path = store.gap_file(day)
    if not path.exists():
        return []
    return [
        Gap(row["start_ns"], row["instrument_id"], gap_kind(row["kind"]), row["end_ns"])
        for row in pq.read_table(str(path)).to_pylist()
    ]


def write_gaps(store: CatalogStore, gaps: Iterable[Gap]) -> None:
    """Merge gaps into their day files: union, one row per (instrument, kind, start), sorted."""
    by_day: dict[str, list[Gap]] = defaultdict(list)
    for gap in gaps:
        by_day[day_name(day_of(gap.start_ns))].append(gap)
    for day, new in sorted(by_day.items()):
        merged = {(g.instrument_id, g.kind, g.start_ns): g for g in [*read_gaps(store, day), *new]}
        rows = sorted(merged.values())
        table = pa.Table.from_pylist(
            [
                {"instrument_id": g.instrument_id, "kind": g.kind, "start_ns": g.start_ns, "end_ns": g.end_ns}
                for g in rows
            ],
            schema=GAP_SCHEMA,
        )
        atomic_write(store.gap_file(day), partial(pq.write_table, table, compression=COMPRESSION))


def close_days(store: CatalogStore, done: DoneRecord) -> bool:
    """Write the manifest of every day that has closed since the last pass. Returns whether any did."""
    covered: dict[str, set[int]] = {}
    prefix = f"{DELTAS_TABLE}/"
    for key, hours in done.hours.items():
        if key.startswith(prefix):
            instrument = key.removeprefix(prefix)
            covered[instrument] = set(hours) | done.empty.get(instrument, set())
    every_hour = [hour for hours in done.hours.values() for hour in hours]
    if not every_hour:
        return False
    closed = False
    for day in range(day_of(min(every_hour)), day_of(max(every_hour)) + 1, DAY_NS):
        name = day_name(day)
        if name in done.days or not day_closed(day, covered):
            continue
        manifest = _manifest(store, done, day)
        text = json.dumps(manifest, indent=1, sort_keys=True)
        atomic_write(store.manifest_file(name), partial(_write_text, text))
        done.days.add(name)
        closed = True
        log.info(
            "catalog day closed",
            extra={
                "fields": {
                    "code": "catalog_day_closed",
                    "date": name,
                    "total_bytes": manifest["total_bytes"],
                    "rows": sum(s["rows"] for s in manifest["streams"]),
                    "gap_seconds": round(sum(g["seconds"] for g in manifest["gaps"]), 3),
                }
            },
        )
    return closed


def _write_text(text: str, path: Path) -> None:
    path.write_text(text)


def _manifest(store: CatalogStore, done: DoneRecord, day: int) -> dict[str, Any]:
    """The manifest of one closed day (spec 0007 *Catalog layout*)."""
    end = day + DAY_NS
    streams = []
    for key, hours in sorted(done.hours.items()):
        table, instrument = key.split("/", 1)
        today = [stats for hour, stats in hours.items() if day <= hour < end]
        if today:
            streams.append(
                {
                    "instrument_id": instrument,
                    "type": table,
                    "rows": sum(s["rows"] for s in today),
                    "bytes": sum(s["bytes"] for s in today),
                    "hours": len(today),
                }
            )
    seconds: dict[tuple[str, str], float] = defaultdict(float)
    # A gap that crosses midnight lies in its start day's file, so the day before is read too.
    for gap in [*read_gaps(store, day_name(day - DAY_NS)), *read_gaps(store, day_name(day))]:
        inside = seconds_within(gap, day, end)
        if inside > 0:
            seconds[(gap.instrument_id, gap.kind)] += inside
    return {
        "date": day_name(day),
        "total_bytes": sum(s["bytes"] for s in streams),
        "streams": streams,
        "gaps": [
            {"instrument_id": instrument, "kind": kind, "seconds": round(value, 9)}
            for (instrument, kind), value in sorted(seconds.items())
        ],
    }
