"""Gap files and the daily manifest (spec 0007 AC-4, AC-5).

Gaps go to ``gaps/<day>.parquet`` by the UTC day of their start, merged so a rerun gives the same
rows. A day is closed once every hour of it is accounted for; its manifest then sums the per hour
stats and late rows in ``done.json`` and the gap seconds that fall inside the day. When late rows
are found later for a closed day, its manifest is written again.
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


def gap_days(store: CatalogStore, *, until: str) -> list[str]:
    """The days up to ``until`` (inclusive) that have a gap file, oldest first."""
    folder = store.gap_file(until).parent
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.parquet") if p.stem <= until)


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
    """Write the manifest of every day that has closed since the last pass. Returns whether any did.

    A day already closed is written again when its ``incomplete`` entries changed its manifest.
    """
    covered: dict[str, set[int]] = {}
    prefix = f"{DELTAS_TABLE}/"
    for key, hours in done.hours.items():
        if key.startswith(prefix):
            instrument = key.removeprefix(prefix)
            covered[instrument] = set(hours) | done.empty.get(instrument, set())
    every_hour = [hour for hours in done.hours.values() for hour in hours]
    if not every_hour:
        return False
    # Up to the last covered hour, empty ones too: a day wholly inside a gap closes as well.
    last = max([*every_hour, *(hour for hours in done.empty.values() for hour in hours)])
    _rewrite_incomplete_days(store, done)
    closed = False
    for day in range(day_of(min(every_hour)), day_of(last) + 1, DAY_NS):
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


def _rewrite_incomplete_days(store: CatalogStore, done: DoneRecord) -> None:
    """Write a closed day's manifest again when its late counts changed (rare, so cheap)."""
    days = {day_of(hour) for hours in done.incomplete.values() for hour in hours}
    for day in sorted(days):
        name = day_name(day)
        if name not in done.days:
            continue
        text = json.dumps(_manifest(store, done, day), indent=1, sort_keys=True)
        path = store.manifest_file(name)
        if not path.exists() or path.read_text() != text:
            atomic_write(path, partial(_write_text, text))


def _write_text(text: str, path: Path) -> None:
    path.write_text(text)


def _manifest(store: CatalogStore, done: DoneRecord, day: int) -> dict[str, Any]:
    """The manifest of one closed day (spec 0007 *Catalog layout*)."""
    end = day + DAY_NS
    streams = []
    for key in sorted(done.hours.keys() | done.incomplete.keys()):
        table, instrument = key.split("/", 1)
        today = [stats for hour, stats in done.hours.get(key, {}).items() if day <= hour < end]
        late = {hour: rows for hour, rows in done.incomplete.get(key, {}).items() if day <= hour < end}
        if today or late:
            streams.append(
                {
                    "instrument_id": instrument,
                    "type": table,
                    "rows": sum(s["rows"] for s in today),
                    "bytes": sum(s["bytes"] for s in today),
                    "hours": len(today),
                    "late_rows": sum(late.values()),
                    "incomplete_hours": sorted(late),
                }
            )
    seconds: dict[tuple[str, str], float] = defaultdict(float)
    # A gap lies whole in its start day's file and may span several days, so every earlier file
    # is read too (one small file per day).
    for gap in [g for earlier in gap_days(store, until=day_name(day)) for g in read_gaps(store, earlier)]:
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
