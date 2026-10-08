"""The compactor service: every ``COMPACTOR_INTERVAL_S`` it converts closed feather hours (spec 0007).

Usage: ``python -m recorder.compactor.main``. A pass that fails logs ``compactor_pass_failed`` and
drops its unfinished hours; the next pass starts again from ``done.json``.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import Field

from recorder.compactor.convert import Beat, BookCarry, FeatherFile, HourStream
from recorder.compactor.instruments import InstrumentDefinitions
from recorder.compactor.store import DELTAS_TABLE, INSTRUMENTS_TABLE, STREAM_TABLES, CatalogStore
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from trading_core.boot import boot
from trading_core.marketdata.hours import GB, HOUR_NS, SECOND_NS, backlog_bytes_needed
from trading_core.settings import Settings

log = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 10.0


class CompactorSettings(Settings):
    catalog_path: str = "/data/catalog"
    # True on the VPS (compose.vps.yaml): refuse to start unless the extra disk is mounted.
    catalog_require_marker: bool = False
    catalog_free_floor_gb: Decimal = Field(default=Decimal(8), gt=0)
    # Parquet bytes per feather byte, for the dead run space check (spec 0007 AC-9).
    compactor_parquet_ratio: Decimal = Field(default=Decimal("0.05"), gt=0)
    # The wait after a file's last write before it is read, so an hour is never finished early.
    compactor_grace_s: int = Field(default=300, ge=0)
    compactor_interval_s: int = Field(default=60, gt=0)


@dataclass
class Found:
    """The recorder's feather files: per (table, instrument) stream, instrument files, and per run."""

    streams: dict[tuple[str, str], list[FeatherFile]] = field(default_factory=dict)
    instruments: list[FeatherFile] = field(default_factory=list)
    runs: dict[str, list[FeatherFile]] = field(default_factory=dict)


def discover(store: CatalogStore) -> Found:
    """Every feather file under ``live/``, each list oldest first."""
    found = Found()
    if not store.live.exists():
        return found
    for run in sorted(p for p in store.live.iterdir() if p.is_dir()):
        for table in STREAM_TABLES:
            for folder in (run / table).glob("*"):
                for path in folder.glob("*.feather"):
                    file = FeatherFile.parse(path, run.name)
                    found.streams.setdefault((table, folder.name), []).append(file)
                    found.runs.setdefault(run.name, []).append(file)
        for path in run.glob(f"{INSTRUMENTS_TABLE}_*.feather"):
            file = FeatherFile.parse(path, run.name)
            found.instruments.append(file)
            found.runs.setdefault(run.name, []).append(file)
    for files in [*found.streams.values(), found.instruments, *found.runs.values()]:
        files.sort()
    return found


def current_run(found: Found) -> str | None:
    """The run the recorder writes now: the one with the newest file creation time in its names.

    File names come from the recorder's clock and never change, unlike a folder's ctime, which a
    deletion inside a dead run would move forward.
    """
    newest = [(files[-1].created_ns, run) for run, files in found.runs.items() if files]
    return max(newest)[1] if newest else None


def is_closed(file: FeatherFile, siblings: list[FeatherFile], current: str | None) -> bool:
    """Closed when its run is no longer the current one, or its run has a newer file (it rotated)."""
    if file.run != current:
        return True
    return any(other.run == file.run and other.created_ns > file.created_ns for other in siblings)


class Heartbeat:
    """Touches the health check file, at most every ``HEARTBEAT_EVERY_S`` (also during a long pass)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last = 0.0

    def __call__(self) -> None:
        now = time.monotonic()
        if now - self._last >= HEARTBEAT_EVERY_S:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()
            self._last = now


class Compactor:
    """The conversion state kept in memory between passes; ``done.json`` is the durable part."""

    def __init__(
        self,
        store: CatalogStore,
        *,
        grace_ns: int,
        ratio: Decimal = Decimal("0.05"),
        floor_bytes: int = 8 * GB,
        beat: Beat | None = None,
    ) -> None:
        self._store = store
        self._grace_ns = grace_ns
        self._ratio = ratio
        self._floor_bytes = floor_bytes
        self._beat = beat
        store.remove_tmp_files()  # a killed pass may have left some; nothing writes yet
        self.done = store.load_done()
        self.streams: dict[tuple[str, str], HourStream] = {}
        self.definitions = InstrumentDefinitions(store)

    def run_pass(self, now_ns: int) -> None:
        """Feed closed, settled files in time order, finish passed hours, then delete what is converted."""
        found = discover(self._store)
        current = current_run(found)
        stop_at = self._backlog_gate(found, current)
        instruments = {instrument for _, instrument in found.streams}
        for file in found.instruments:
            self.definitions.read(file, instruments)
        for (table, instrument), files in found.streams.items():
            stream = self._stream(table, instrument)
            for file in files:
                if stop_at is not None and file.created_ns >= stop_at:
                    break
                if not is_closed(file, files, current) or now_ns - file.path.stat().st_mtime_ns < self._grace_ns:
                    break  # files are read strictly in order
                stream.feed(file, self.done)
            open_hour = stream.open
            more_source = open_hour is not None and any(
                self._store.relative(f.path) not in stream.fed and f.created_ns < open_hour.hour + HOUR_NS
                for f in files
            )
            stream.finish_if_passed(now_ns=now_ns, grace_ns=self._grace_ns, more_source=more_source, done=self.done)
        if self.definitions.write_hours(instruments, self.done, STREAM_TABLES):
            self._store.save_done(self.done)
        self._delete_converted(found, current, instruments)

    def abort(self) -> None:
        """Drop every unfinished hour."""
        for stream in self.streams.values():
            stream.abort()

    def _stream(self, table: str, instrument: str) -> HourStream:
        stream = self.streams.get((table, instrument))
        if stream is None:
            carry = BookCarry(instrument) if table == DELTAS_TABLE else None
            stream = HourStream(
                self._store, table, instrument, self.done, carry=carry, on_overlap=self._on_overlap, beat=self._beat
            )
            self.streams[(table, instrument)] = stream
        return stream

    def _on_overlap(self, hour: int) -> None:
        """AC-12: every carried book is invalid until each instrument's next feed ``CLEAR``."""
        for stream in self.streams.values():
            if stream.carry is not None:
                stream.carry.invalidate()
        self.done.carry = dict.fromkeys(self.done.carry)
        self._store.save_done(self.done)

    def _backlog_gate(self, found: Found, current: str | None) -> int | None:
        """AC-9: dead runs not yet started need room for their Parquet plus the floor.

        Runs are taken oldest first, each needing room on top of the ones before it. Returns the
        creation time of the first run that does not fit (nothing from it on is read this pass,
        since conversion stays in time order), or ``None``.
        """
        dead = sorted((files[0].created_ns, run) for run, files in found.runs.items() if run != current and files)
        free = None
        planned = 0
        started = False
        for first_ns, run in dead:
            if run in self.done.started_runs:
                continue
            feather = sum(f.path.stat().st_size for f in found.runs[run])
            needed = backlog_bytes_needed(feather, self._ratio, self._floor_bytes + planned)
            free = self._store.free_bytes() if free is None else free
            if free < needed:
                log.error(
                    "a dead run cannot convert yet",
                    extra={
                        "fields": {
                            "code": "backlog_needs_space",
                            "run": run,
                            "bytes_needed": needed,
                            "free_bytes": free,
                        }
                    },
                )
                if started:
                    self._store.save_done(self.done)
                return first_ns
            planned = needed - self._floor_bytes
            self.done.started_runs.add(run)
            started = True
        if started:
            self._store.save_done(self.done)
        return None

    def _delete_converted(self, found: Found, current: str | None, instruments: set[str]) -> None:
        """Delete feather whose rows are all in finished hours, then empty dead run folders."""
        for rel in sorted(self.done.files - self.done.kept):
            (self._store.root / rel).unlink(missing_ok=True)
        changed = bool(self.done.files)
        self.done.files = set()
        for file in found.instruments:
            if is_closed(file, found.instruments, current) and self.definitions.deletable(file, instruments, self.done):
                file.path.unlink(missing_ok=True)
        for run in found.runs:
            if run != current:
                self._store.remove_run_if_empty(run)
                if not (self._store.live / run).exists() and run in self.done.started_runs:
                    self.done.started_runs.discard(run)
                    changed = True
        if changed:
            self._store.save_done(self.done)


def main() -> None:
    settings = boot("worker", settings_cls=CompactorSettings).settings
    root = Path(settings.catalog_path)
    try:
        check_catalog_volume(root, required=settings.catalog_require_marker)
    except CatalogVolumeMissing as exc:
        log.critical(
            "catalog volume missing, refusing to start",
            extra={"fields": {"code": "catalog_volume_missing", "path": str(exc.path)}},
        )
        sys.exit(1)
    store = CatalogStore(root)
    heartbeat = Heartbeat(Path(os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))

    def fresh() -> Compactor:
        return Compactor(
            store,
            grace_ns=settings.compactor_grace_s * SECOND_NS,
            ratio=settings.compactor_parquet_ratio,
            floor_bytes=int(settings.catalog_free_floor_gb * GB),
            beat=heartbeat,
        )

    compactor = fresh()
    while True:
        try:
            compactor.run_pass(time.time_ns())
        except Exception as exc:
            log.exception(
                "compactor pass failed",
                extra={"fields": {"code": "compactor_pass_failed", "error": type(exc).__name__}},
            )
            compactor.abort()
            compactor = fresh()
        heartbeat()
        time.sleep(settings.compactor_interval_s)


if __name__ == "__main__":
    main()
