"""The compactor service: every ``COMPACTOR_INTERVAL_S`` it converts closed feather hours (spec 0007).

Usage: ``python -m recorder.compactor.main [--offline]``. A pass that fails logs
``compactor_pass_failed`` and drops its unfinished hours; the next pass starts again from ``done.json``.

``--offline`` is the laptop's one time backlog conversion (AC-9): no recorder writes there, so every
run has ended and every file is closed. It runs passes until one converts nothing more, with no
grace and no retention, then exits 0 only when no feather is left and nothing failed.
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import Field

from recorder.compactor.convert import Beat, BookCarry, HourStream
from recorder.compactor.days import close_days
from recorder.compactor.instruments import InstrumentDefinitions
from recorder.compactor.store import DELTAS_TABLE, INSTRUMENTS_TABLE, STREAM_TABLES, CatalogStore, read_backup_state
from recorder.live_files import Found, current_run, discover, is_closed
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from trading_core.boot import boot
from trading_core.marketdata.errors import BackupStateInvalid
from trading_core.marketdata.gaps import GapDetector, GapState
from trading_core.marketdata.hours import GB, SECOND_NS, backlog_bytes_needed
from trading_core.marketdata.retention import parse_backup_state, plan_retention
from trading_core.marketdata.sequence import RunHoles, parse_holes
from trading_core.settings import CatalogDiskSettings

log = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 10.0
# An offline conversion that logged any of these did not finish the backlog (spec 0007 AC-9).
OFFLINE_FAIL_CODES: Final = frozenset({"catalog_hour_mismatch", "compactor_pass_failed", "backlog_needs_space"})
# A backlog converts in one or two passes; more means something keeps changing without finishing.
MAX_OFFLINE_PASSES: Final = 20


class CompactorSettings(CatalogDiskSettings):
    """The compactor's catalog, conversion, and retention settings, plus the shared disk floors."""

    catalog_path: str = "/data/catalog"
    # True on the VPS (compose.vps.yaml): refuse to start unless the extra disk is mounted.
    catalog_require_marker: bool = False
    # Retention keeps the Parquet catalog (`data/`) under this size (spec 0007 AC-6).
    catalog_cap_gb: Decimal = Field(default=Decimal(30), gt=0)
    # The backup's catalog step writes it; retention deletes only hours older than its cutoff (AC-8).
    backup_state_path: Path = Path("/state/catalog_backup.json")
    # Parquet bytes per feather byte, for the dead run space check (spec 0007 AC-9): measured 0.0275
    # in Milestone 1, times 1.5.
    compactor_parquet_ratio: Decimal = Field(default=Decimal("0.042"), gt=0)
    # The wait after a file's last write before it is read, so an hour is never finished early.
    compactor_grace_s: int = Field(default=300, ge=0)
    compactor_interval_s: int = Field(default=60, gt=0)
    # Two book deltas of one instrument this far apart are a `silence` gap (spec 0007 AC-4).
    book_silence_gap_s: int = Field(default=10, gt=0)
    # How late a row may be and still be put back in place; 0 means no reordering (AC-12).
    compactor_reorder_window_s: int = Field(default=600, ge=0)
    # The most rows one stream's reorder window may hold, which bounds memory in a burst (AC-12).
    compactor_reorder_max_rows: int = Field(default=2_000_000, ge=1)


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
        ratio: Decimal = Decimal("0.042"),
        floor_bytes: int = 8 * GB,
        silence_ns: int = 10 * SECOND_NS,
        window_ns: int = 600 * SECOND_NS,
        max_rows: int = 2_000_000,
        beat: Beat | None = None,
        offline: bool = False,
    ) -> None:
        self._store = store
        # Offline (the laptop backlog, AC-9): no run is the current one, so every run ends.
        self._offline = offline
        self._grace_ns = grace_ns
        self._ratio = ratio
        self._floor_bytes = floor_bytes
        self._silence_ns = silence_ns
        self._window_ns = window_ns
        self._max_rows = max_rows
        self._beat = beat
        store.remove_tmp_files()  # a killed pass may have left some; nothing writes yet
        self.done = store.load_done()
        self.streams: dict[tuple[str, str], HourStream] = {}
        self.definitions = InstrumentDefinitions(store)

    def run_pass(self, now_ns: int) -> None:
        """Feed closed, settled files in time order, finish hours the watermark passed, then delete
        what is converted."""
        found = discover(self._store.live, STREAM_TABLES, INSTRUMENTS_TABLE)
        current = None if self._offline else current_run(found)
        stop_at = self._backlog_gate(found, current)
        holes = self._read_holes(found, current)
        instruments = {instrument for _, instrument in found.streams}
        for file in found.instruments:
            self.definitions.read(file, instruments)
        for (table, instrument), files in found.streams.items():
            stream = self._stream(table, instrument)
            stream.set_holes(holes, current)
            stream.drain(self.done, now_ns)
            for file in files:
                if stop_at is not None and file.created_ns >= stop_at:
                    break
                if not is_closed(file, files, current) or now_ns - file.path.stat().st_mtime_ns < self._grace_ns:
                    break  # files are read strictly in order
                stream.feed(file, self.done, now_ns)
            stream.end_run_if_over(files, current, self.done, now_ns)
        changed = False
        for stream in self.streams.values():
            changed = stream.end_pass(self.done) or changed
        changed = self.definitions.write_hours(instruments, self.done, STREAM_TABLES) or changed
        if close_days(self._store, self.done) or changed:
            self._store.save_done(self.done)
        self._delete_converted(found, current, instruments)

    def abort(self) -> None:
        """Drop every unfinished hour."""
        for stream in self.streams.values():
            stream.abort()

    def _stream(self, table: str, instrument: str) -> HourStream:
        stream = self.streams.get((table, instrument))
        if stream is None:
            deltas = table == DELTAS_TABLE
            gaps = None
            if deltas:
                state = GapState.from_dict(self.done.streams.get(instrument, {}))
                gaps = GapDetector(instrument, state, silence_ns=self._silence_ns)
            stream = HourStream(
                self._store,
                table,
                instrument,
                self.done,
                carry=BookCarry(instrument) if deltas else None,
                gaps=gaps,
                grace_ns=self._grace_ns,
                window_ns=self._window_ns,
                max_rows=self._max_rows,
                beat=self._beat,
            )
            self.streams[(table, instrument)] = stream
        return stream

    def _read_holes(self, found: Found, current: str | None) -> dict[str, RunHoles | None]:
        """Each run's holes file as written so far (AC-16); ``None`` for a run without one.

        A run that ended without ``closed`` gets the fail safe hole over its unchecked tail. A run
        without a file was recorded before the sequence watch shipped: it converts as before, and
        INFO ``sequence_unchecked_run`` is logged once.
        """
        holes: dict[str, RunHoles | None] = {}
        for run in found.runs:
            read = self._store.read_holes_lines(run)
            if read is None:
                holes[run] = None
                if self.done.holes.get(run) != {"unchecked": True}:
                    log.info(
                        "run has no sequence holes file",
                        extra={"fields": {"code": "sequence_unchecked_run", "run": run}},
                    )
                    self.done.holes[run] = {"unchecked": True}
                continue
            lines, read_bytes = read
            parsed = parse_holes(lines)
            if run == current:
                holes[run] = parsed
            else:
                holes[run] = parsed.with_fail_safe()
            self.done.holes[run] = {
                "read_bytes": read_bytes,
                "checked_to_ns": parsed.checked_to_ns,
                "closed": parsed.closed,
            }
        return holes

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
        for rel in sorted(self.done.files):
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
        self._remove_holes_only_runs(found, current)
        for run in [r for r in self.done.holes if not (self._store.live / r).exists()]:
            del self.done.holes[run]
            changed = True
        if changed:
            self._store.save_done(self.done)

    def _remove_holes_only_runs(self, found: Found, current: str | None) -> None:
        """Delete run folders that hold a holes file but no feather (the recorder stopped before
        any data came), once the current run started after the holes file's last write."""
        files = found.runs.get(current) if current is not None else None
        if not files or not self._store.live.exists():
            return
        started = files[0].created_ns
        for folder in self._store.live.iterdir():
            if not folder.is_dir() or folder.name in found.runs:
                continue
            holes = self._store.holes_file(folder.name)
            if holes.exists() and holes.stat().st_mtime_ns < started:
                self._store.remove_run_if_empty(folder.name)


def run_retention(store: CatalogStore, *, state_path: Path, cap_bytes: int, floor_bytes: int, now_ns: int) -> None:
    """AC-6: delete the oldest whole hours, once backed up, while the catalog is over its cap or
    free space under its floor. Without a valid backup state, nothing is deleted."""
    cutoff_ns = None
    try:
        raw = read_backup_state(state_path)
        if raw is not None:
            cutoff_ns = parse_backup_state(raw, now_ns).cutoff_ns
    except BackupStateInvalid as exc:
        log.error(
            "backup state invalid, retention deletes nothing",
            extra={"fields": {"code": "backup_state_invalid", "path": str(state_path), "reason": exc.reason}},
        )
    hours, catalog_bytes = store.hour_inventory()
    sizes = {hour.hour_ns: hour.total_bytes for hour in hours}
    plan = plan_retention(
        hours,
        cutoff_ns,
        catalog_bytes=catalog_bytes,
        free_bytes=store.free_bytes(),
        cap_bytes=cap_bytes,
        floor_bytes=floor_bytes,
    )
    for hour in plan.delete:
        store.delete_hour(hour)
        log.info(
            "catalog hour retired",
            extra={"fields": {"code": "catalog_retention_deleted", "hour_ns": hour, "bytes": sizes[hour]}},
        )
    if plan.blocked:
        log.error(
            "catalog needs space but no hour is backed up yet",
            extra={
                "fields": {
                    "code": "catalog_retention_blocked",
                    "bytes_needed": plan.needed_bytes,
                    "has_backup_state": cutoff_ns is not None,
                }
            },
        )


class CodeWatch(logging.Handler):
    """Remembers which of ``codes`` were logged, from the ``code`` field of each record."""

    def __init__(self, codes: frozenset[str]) -> None:
        super().__init__()
        self._codes = codes
        self.seen: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None)
        code = fields.get("code") if isinstance(fields, dict) else None
        if code in self._codes:
            self.seen.add(code)


def run_offline(compactor: Compactor, store: CatalogStore, clock: Callable[[], int] = time.time_ns) -> int:
    """AC-9: convert every run under ``live/`` until a pass changes nothing. Returns the exit code:
    0 only when no feather is left and no pass failed, mismatched, or lacked space."""
    watch = CodeWatch(OFFLINE_FAIL_CODES)
    logging.getLogger().addHandler(watch)
    try:
        for _ in range(MAX_OFFLINE_PASSES):
            before = store.load_done().to_json()
            try:
                compactor.run_pass(clock())
            except Exception as exc:
                log.exception(
                    "compactor pass failed",
                    extra={"fields": {"code": "compactor_pass_failed", "error": type(exc).__name__}},
                )
                compactor.abort()
                break
            if store.load_done().to_json() == before:
                break
    finally:
        logging.getLogger().removeHandler(watch)
    left = sorted(store.relative(p) for p in store.live.rglob("*.feather")) if store.live.exists() else []
    ok = not left and not watch.seen
    fields: dict[str, object] = {"code": "compactor_offline_done", "ok": ok, "feather_left": len(left)}
    if watch.seen:
        fields["failed_codes"] = sorted(watch.seen)
    if left:
        fields["first_left"] = left[0]
    (log.info if ok else log.error)("offline conversion finished", extra={"fields": fields})
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="recorder.compactor.main")
    parser.add_argument("--offline", action="store_true", help="convert the laptop backlog once, then exit")
    offline = parser.parse_args().offline
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
            # Offline, every file was closed long ago, so there is nothing to wait for.
            grace_ns=0 if offline else settings.compactor_grace_s * SECOND_NS,
            ratio=settings.compactor_parquet_ratio,
            floor_bytes=int(settings.catalog_free_floor_gb * GB),
            silence_ns=settings.book_silence_gap_s * SECOND_NS,
            window_ns=settings.compactor_reorder_window_s * SECOND_NS,
            max_rows=settings.compactor_reorder_max_rows,
            beat=heartbeat,
            offline=offline,
        )

    if offline:
        sys.exit(run_offline(fresh(), store))
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
        # After every pass, also a failed one: retention frees space even while conversion is stuck.
        try:
            run_retention(
                store,
                state_path=settings.backup_state_path,
                cap_bytes=int(settings.catalog_cap_gb * GB),
                floor_bytes=int(settings.catalog_free_floor_gb * GB),
                now_ns=time.time_ns(),
            )
        except Exception as exc:
            log.exception(
                "catalog retention failed",
                extra={"fields": {"code": "compactor_pass_failed", "step": "retention", "error": type(exc).__name__}},
            )
        heartbeat()
        time.sleep(settings.compactor_interval_s)


if __name__ == "__main__":
    main()
