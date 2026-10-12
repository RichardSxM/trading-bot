"""Gap files and the daily manifest (spec 0007 AC-4, AC-5).

Gaps go to ``gaps/<day>.parquet`` by the UTC day of their start, merged so a rerun gives the same
rows. A day is closed once every hour of it is accounted for; its manifest then sums the per hour
stats and late rows in ``done.json`` and the gap seconds that fall inside the day. When late rows
are found later for a closed day, its manifest is written again.
"""

import logging
from collections.abc import Collection
from functools import partial
from typing import Any

from recorder.compactor.store import DELTAS_TABLE, CatalogStore, DoneRecord, atomic_write
from trading_core.catalog.days import (
    GAP_SCHEMA,
    build_manifest,
    gap_days,
    manifest_text,
    read_gaps,
    write_gaps,
)
from trading_core.catalog.days import write_text as _write_text
from trading_core.marketdata.gaps import BOOK_STREAM
from trading_core.marketdata.hours import DAY_NS, day_closed, day_name, day_of

__all__ = ["GAP_SCHEMA", "close_days", "gap_days", "read_gaps", "write_gaps"]

log = logging.getLogger(__name__)


def close_days(store: CatalogStore, done: DoneRecord, recorded: Collection[str] | None = None) -> bool:
    """Write the manifest of every day that has closed since the last pass. Returns whether any did.

    A day already closed is written again when its ``incomplete`` entries changed its manifest.
    Only the ``recorded`` instruments (all when ``None``) must cover a day for it to close.
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
        if name in done.days or not day_closed(day, covered, recorded):
            continue
        manifest = _manifest(store, done, day)
        text = manifest_text(manifest)
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
                    # The book's gaps; the context streams' are in the manifest (spec 0009 AC-7).
                    "gap_seconds": round(sum(g["seconds"] for g in manifest["gaps"] if g["stream"] == BOOK_STREAM), 3),
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
        text = manifest_text(_manifest(store, done, day))
        path = store.manifest_file(name)
        if not path.exists() or path.read_text() != text:
            atomic_write(path, partial(_write_text, text))


def _manifest(store: CatalogStore, done: DoneRecord, day: int) -> dict[str, Any]:
    """The manifest of one closed day (spec 0007 *Catalog layout*)."""
    return build_manifest(store, day, done.hours, done.incomplete)
