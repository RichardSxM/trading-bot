"""Gap files and the daily manifest (spec 0007 AC-4, AC-5).

Gaps go to ``gaps/<day>.parquet`` by the UTC day of their start, merged so a rerun gives the same
rows. A day's manifest sums the per hour stats and late rows of each stream and the gap seconds
that fall inside the day. The manifest is written last, as the day's commit point.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from trading_core.catalog.layout import CatalogLayout
from trading_core.catalog.sink import COMPRESSION, atomic_write
from trading_core.marketdata.gaps import Gap, gap_kind, seconds_within
from trading_core.marketdata.hours import DAY_NS, day_name, day_of

GAP_SCHEMA: Final = pa.schema(
    [
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("start_ns", pa.uint64(), nullable=False),
        pa.field("end_ns", pa.uint64(), nullable=False),
    ]
)

# Per stream key (``<table>/<instrument>``), per hour: that hour's ``rows`` and ``bytes``.
HourStats = Mapping[str, Mapping[int, Mapping[str, int]]]
# Per stream key, per hour that misses rows: the late row count (0 for a spoiled hour).
IncompleteHours = Mapping[str, Mapping[int, int]]


def read_gaps(layout: CatalogLayout, day: str) -> list[Gap]:
    """The gaps that start on ``day``, or none when it has no gap file."""
    path = layout.gap_file(day)
    if not path.exists():
        return []
    return [
        Gap(row["start_ns"], row["instrument_id"], gap_kind(row["kind"]), row["end_ns"])
        for row in pq.read_table(str(path)).to_pylist()
    ]


def gap_days(layout: CatalogLayout, *, until: str) -> list[str]:
    """The days up to ``until`` (inclusive) that have a gap file, oldest first."""
    folder = layout.gap_file(until).parent
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.parquet") if p.stem <= until)


def write_gaps(layout: CatalogLayout, gaps: Iterable[Gap]) -> None:
    """Merge gaps into their day files: union, one row per (instrument, kind, start), sorted."""
    by_day: dict[str, list[Gap]] = defaultdict(list)
    for gap in gaps:
        by_day[day_name(day_of(gap.start_ns))].append(gap)
    for day, new in sorted(by_day.items()):
        merged = {(g.instrument_id, g.kind, g.start_ns): g for g in [*read_gaps(layout, day), *new]}
        rows = sorted(merged.values())
        table = pa.Table.from_pylist(
            [
                {"instrument_id": g.instrument_id, "kind": g.kind, "start_ns": g.start_ns, "end_ns": g.end_ns}
                for g in rows
            ],
            schema=GAP_SCHEMA,
        )
        atomic_write(layout.gap_file(day), partial(pq.write_table, table, compression=COMPRESSION))


def build_manifest(layout: CatalogLayout, day: int, hours: HourStats, incomplete: IncompleteHours) -> dict[str, Any]:
    """The manifest of one closed day (spec 0007 *Catalog layout*)."""
    end = day + DAY_NS
    streams = []
    for key in sorted(hours.keys() | incomplete.keys()):
        table, instrument = key.split("/", 1)
        today = [stats for hour, stats in hours.get(key, {}).items() if day <= hour < end]
        late = {hour: rows for hour, rows in incomplete.get(key, {}).items() if day <= hour < end}
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
    for gap in [g for earlier in gap_days(layout, until=day_name(day)) for g in read_gaps(layout, earlier)]:
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


def manifest_text(manifest: Mapping[str, Any]) -> str:
    """The manifest as the file holds it: indented JSON with sorted keys."""
    return json.dumps(manifest, indent=1, sort_keys=True)


def write_text(text: str, path: Path) -> None:
    """Write ``text`` to ``path`` (the writer ``atomic_write`` calls for a manifest)."""
    path.write_text(text)
