"""The bridge gap between the laptop backlog and the VPS catalog (spec 0007 AC-9).

Usage: ``python -m recorder.compactor.bridge --vps <dir>``, on the laptop, after
``recorder.compactor.main --offline`` converted the backlog. ``<dir>`` holds the VPS catalog's first
day, fetched with ``scripts/fetch-catalog.sh --into``. For each instrument on both sides it writes one
``recorder_down`` gap from the backlog's last written row to the VPS's first delta row, marks the
hours wholly inside it as empty, and closes the days that are now whole. A rerun changes nothing.
"""

import argparse
import logging
import sys
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
from nautilus_trader.persistence.catalog.parquet import _parse_filename_timestamps
from nautilus_trader.persistence.funcs import urisafe_identifier

from recorder.compactor.days import close_days, write_gaps
from recorder.compactor.main import CompactorSettings
from recorder.compactor.store import DELTAS_TABLE, CatalogStore
from trading_core.boot import boot
from trading_core.marketdata.errors import BridgeRefused
from trading_core.marketdata.gaps import Gap, GapState
from trading_core.marketdata.hours import hours_inside

log = logging.getLogger(__name__)


def first_delta_ns(vps_root: Path, instrument_id: str) -> int | None:
    """The first ``ts_init`` of the earliest delta hour of an instrument under ``vps_root``."""
    folder = vps_root / "data" / DELTAS_TABLE / urisafe_identifier(instrument_id)
    hours = []
    for path in folder.glob("*.parquet") if folder.exists() else ():
        interval = _parse_filename_timestamps(path.name)
        if interval is not None:
            hours.append((interval[0], path))
    if not hours:
        return None
    table = pq.read_table(str(min(hours)[1]), columns=["ts_init"])
    if table.num_rows == 0:
        return None
    first: int = pc.min(table.column("ts_init")).as_py()
    return first


def vps_instruments(vps_root: Path) -> set[str]:
    """The urisafe instrument folders with delta hours under ``vps_root``."""
    folder = vps_root / "data" / DELTAS_TABLE
    return {p.name for p in folder.iterdir() if p.is_dir()} if folder.exists() else set()


def bridge(store: CatalogStore, vps_root: Path) -> list[Gap]:
    """Write the bridge gaps and commit them to ``done.json``. Returns the gaps.

    Raises ``BridgeRefused`` while feather is left under ``live/``, when an instrument has no VPS
    delta hour, or when a gap would not end after it starts.
    """
    left = sorted(store.live.rglob("*.feather")) if store.live.exists() else []
    if left:
        raise BridgeRefused(f"{len(left)} feather files are left under live/; run the offline conversion first")
    done = store.load_done()
    backlog = {
        instrument: state.last_ts
        for instrument, raw in done.streams.items()
        if (state := GapState.from_dict(raw)).last_ts is not None
    }
    on_vps = vps_instruments(vps_root)
    for instrument in sorted(backlog):
        if urisafe_identifier(instrument) not in on_vps:
            _missing(instrument, "vps")
    for folder in sorted(on_vps - {urisafe_identifier(i) for i in backlog}):
        _missing(folder, "backlog")

    gaps = []
    for instrument, start in sorted(backlog.items()):
        if urisafe_identifier(instrument) not in on_vps:
            continue
        end = first_delta_ns(vps_root, instrument)
        if end is None:
            raise BridgeRefused(f"no VPS delta row for {instrument} under {vps_root}")
        if end <= start:
            raise BridgeRefused(
                f"the VPS's first row of {instrument} ({end}) is not after the backlog's last ({start})"
            )
        gaps.append(Gap(start, instrument, "recorder_down", end))
    if not gaps:
        raise BridgeRefused("no instrument is on both sides")

    write_gaps(store, gaps)
    for gap in gaps:
        done.empty.setdefault(gap.instrument_id, set()).update(hours_inside(gap.start_ns, gap.end_ns))
        log.warning(
            "book gap",
            extra={
                "fields": {
                    "code": "book_gap",
                    "instrument_id": gap.instrument_id,
                    "kind": gap.kind,
                    "start_ns": gap.start_ns,
                    "end_ns": gap.end_ns,
                    "duration_s": round(gap.duration_s, 3),
                    "bridge": True,
                }
            },
        )
    close_days(store, done)
    store.save_done(done)
    return gaps


def _missing(instrument: str, absent_from: str) -> None:
    log.warning(
        "instrument on one side only, no bridge gap",
        extra={
            "fields": {"code": "bridge_instrument_missing", "instrument_id": instrument, "absent_from": absent_from}
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="recorder.compactor.bridge")
    parser.add_argument("--vps", type=Path, required=True, help="the fetched VPS catalog's first day")
    vps_root: Path = parser.parse_args().vps
    settings = boot("worker", settings_cls=CompactorSettings).settings
    store = CatalogStore(Path(settings.catalog_path))
    try:
        gaps = bridge(store, vps_root)
    except BridgeRefused as exc:
        log.error("bridge refused", extra={"fields": {"code": "bridge_refused", "reason": exc.reason}})
        sys.exit(1)
    log.info("bridge written", extra={"fields": {"code": "bridge_written", "gaps": len(gaps)}})


if __name__ == "__main__":
    main()
