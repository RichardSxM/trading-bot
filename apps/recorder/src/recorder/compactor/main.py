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
from recorder.compactor.days import close_days, rewrite_days, write_gaps
from recorder.compactor.instruments import InstrumentDefinitions
from recorder.compactor.store import (
    DELTAS_TABLE,
    HOURLY_CONTEXT_TABLES,
    INSTRUMENTS_TABLE,
    LIQUIDATION_TABLE,
    OPEN_INTEREST_TABLE,
    RAW_FEED_TABLES,
    SILENCE_TABLES,
    STREAM_TABLES,
    CatalogStore,
    read_backup_state,
    stream_key,
)
from recorder.live_files import Found, current_run, discover, is_closed
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from trading_core.boot import boot
from trading_core.marketdata.context_feed import Outage, pair_outages, parse_outages
from trading_core.marketdata.context_hours import missing_context_hours
from trading_core.marketdata.errors import BackupStateInvalid
from trading_core.marketdata.gaps import Gap, GapDetector, GapState, SilenceDetector, SilenceState
from trading_core.marketdata.hours import (
    DAY_NS,
    GB,
    HOUR_NS,
    SECOND_NS,
    backlog_bytes_needed,
    day_of,
    hours_inside,
)
from trading_core.marketdata.retention import parse_backup_state, plan_retention
from trading_core.marketdata.sequence import RunHoles, parse_holes
from trading_core.marketdata.tails import StreamMark, TailState, resolve_tails
from trading_core.settings import CatalogDiskSettings, ContextSettings

log = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 10.0
# An offline conversion that logged any of these did not finish the backlog (spec 0007 AC-9).
OFFLINE_FAIL_CODES: Final = frozenset({"catalog_hour_mismatch", "compactor_pass_failed", "backlog_needs_space"})
# A backlog converts in one or two passes; more means something keeps changing without finishing.
MAX_OFFLINE_PASSES: Final = 20
# Without the B2 copy, retention never deletes an hour written in the last day, so an hour still
# being converted or rewritten is never touched.
LOCAL_KEEP_NS: Final = 24 * 3600 * 1_000_000_000


class CompactorSettings(CatalogDiskSettings, ContextSettings):
    """The compactor's catalog, conversion, and retention settings, plus the shared disk floors and the
    context silence rule (spec 0009 AC-7)."""

    catalog_path: str = "/data/catalog"
    # True on the VPS (compose.vps.yaml): refuse to start unless the extra disk is mounted.
    catalog_require_marker: bool = False
    # Retention keeps the Parquet catalog (`data/`) under this size (spec 0007 AC-6).
    catalog_cap_gb: Decimal = Field(default=Decimal(30), gt=0)
    # True only while the backup copies the catalog to B2: then retention deletes only hours older
    # than the cutoff in the backup state (AC-8). False since 2026-10-22, when B2's free 10 GB filled:
    # the catalog lives on the VPS disk only, and retention keeps just the newest day.
    catalog_needs_backup: bool = False
    # The backup's catalog step writes it; read only when catalog_needs_backup is true.
    backup_state_path: Path = Path("/state/catalog_backup.json")
    # Parquet bytes per feather byte, for the dead run space check (spec 0007 AC-9): measured 0.0275
    # in Milestone 1, times 1.5.
    compactor_parquet_ratio: Decimal = Field(default=Decimal("0.042"), gt=0)
    # The wait after a file's last write before it is read, so an hour is never finished early.
    compactor_grace_s: int = Field(default=300, ge=0)
    compactor_interval_s: int = Field(default=60, gt=0)
    # The recorder's instruments (the same RECORDER_INSTRUMENTS): only these must cover a day for it
    # to close, so one dropped from recording no longer holds every later day back (AC-5). Unset
    # means every instrument in the catalog.
    recorder_instruments: str | None = None
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
        context_silence_ns: int = 60 * SECOND_NS,
        window_ns: int = 600 * SECOND_NS,
        max_rows: int = 2_000_000,
        beat: Beat | None = None,
        offline: bool = False,
        recorded: frozenset[str] | None = None,
    ) -> None:
        self._store = store
        self._recorded = recorded
        # Offline (the laptop backlog, AC-9): no run is the current one, so every run ends.
        self._offline = offline
        self._grace_ns = grace_ns
        self._ratio = ratio
        self._floor_bytes = floor_bytes
        self._silence_ns = silence_ns
        self._context_silence_ns = context_silence_ns
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
        outages_changed = self._outage_gaps(found, instruments)
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
        changed = outages_changed
        for stream in self.streams.values():
            changed = stream.end_pass(self.done) or changed
        changed = self.definitions.write_hours(instruments, self.done, STREAM_TABLES) or changed
        # After every stream, so the tails come out the same whichever stream converted first.
        changed = self._resolve_tails() or changed
        # After the tails, so a tail reconnect's hours already count as feed down.
        changed = self._check_context_hours(instruments) or changed
        if close_days(self._store, self.done, self._recorded) or changed:
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
            silence = None
            if deltas:
                state = GapState.from_dict(self.done.streams.get(instrument, {}))
                gaps = GapDetector(instrument, state, silence_ns=self._silence_ns)
            elif table in SILENCE_TABLES:
                quiet = SilenceState.from_dict(self.done.context_streams.get(stream_key(table, instrument), {}))
                silence = SilenceDetector(instrument, table, quiet, silence_ns=self._context_silence_ns)
            stream = HourStream(
                self._store,
                table,
                instrument,
                self.done,
                carry=BookCarry(instrument) if deltas else None,
                gaps=gaps,
                silence=silence,
                dedupe_ids=table == LIQUIDATION_TABLE,
                grace_ns=self._grace_ns,
                window_ns=self._window_ns,
                max_rows=self._max_rows,
                beat=self._beat,
            )
            self.streams[(table, instrument)] = stream
        return stream

    def _outage_gaps(self, found: Found, instruments: set[str]) -> bool:
        """The new lines of each run's ``context_outages.jsonl`` as ``reconnect`` gaps of the raw feed's
        streams, for every instrument (spec 0009 AC-7). True if any line was new."""
        changed = False
        for run in found.runs:
            lines = self._store.read_outage_lines(run)
            if lines is None:
                continue
            seen = self.done.outages.get(run, 0)
            if len(lines) <= seen:
                continue
            outages, bad = parse_outages(lines[seen:])
            if bad:
                log.error(
                    "context outages file has unreadable lines",
                    extra={"fields": {"code": "context_outages_bad_lines", "run": run, "bad_lines": bad}},
                )
            # An open line waits in done.json for its closed line, or for the run's end (amended AC-7).
            stored = [Outage(o["start_ns"], None, o["reason"]) for o in self.done.open_outages.get(run, [])]
            closed, still_open = pair_outages(stored, outages)
            if still_open:
                self.done.open_outages[run] = [{"start_ns": o.start_ns, "reason": o.reason} for o in still_open]
            else:
                self.done.open_outages.pop(run, None)
            write_gaps(
                self._store,
                [
                    Gap(o.start_ns, instrument, "reconnect", o.end_ns, stream=table)
                    for o in closed
                    if o.end_ns is not None
                    for instrument in sorted(instruments)
                    for table in RAW_FEED_TABLES
                ],
            )
            self.done.outages[run] = len(lines)
            # The hours the raw feed could not fill, for the missing context hour check.
            self.done.feed_down.update(
                h for o in closed if o.end_ns is not None for h in hours_inside(o.start_ns, o.end_ns)
            )
            changed = True
        return changed

    def _check_context_hours(self, instruments: set[str]) -> bool:
        """Log WARNING ``context_hour_missing`` once per closed hour and table with no context file
        (spec 0013 AC-7), skipping hours the feed could not fill. True if ``done.json`` changed."""
        changed = False
        # A feed still down (an open outage line) counts as down from the first whole hour after it went.
        open_from = [-(-o["start_ns"] // HOUR_NS) * HOUR_NS for lines in self.done.open_outages.values() for o in lines]
        for instrument in sorted(instruments):
            book_hours = self.done.hours.get(stream_key(DELTAS_TABLE, instrument), {}).keys()
            down = self.done.empty.get(instrument, set())
            feed_down = self.done.feed_down | {h for h in book_hours if open_from and h >= min(open_from)}
            skip = {table: down | feed_down if table in RAW_FEED_TABLES else down for table in HOURLY_CONTEXT_TABLES}
            checked_to = self.done.context_checked.get(instrument)
            missing, checked = missing_context_hours(
                book_hours=book_hours,
                context_hours={
                    table: self.done.hours.get(stream_key(table, instrument), {}).keys()
                    for table in HOURLY_CONTEXT_TABLES
                },
                skip=skip,
                checked_to=checked_to,
            )
            for gap in missing:
                log.warning(
                    "context hour missing",
                    extra={
                        "fields": {
                            "code": "context_hour_missing",
                            "hour": gap.label,
                            "hour_ns": gap.hour_ns,
                            "table": gap.table,
                            "instrument_id": instrument,
                        }
                    },
                )
            if checked is not None and checked != checked_to:
                self.done.context_checked[instrument] = checked
                changed = True
        if self.done.context_checked and self.done.feed_down:
            oldest = min(self.done.context_checked.values())
            kept = {h for h in self.done.feed_down if h > oldest}
            if kept != self.done.feed_down:
                self.done.feed_down = kept
                changed = True
        return changed

    def _resolve_tails(self) -> bool:
        """Write the tail ``reconnect`` and tail ``silence`` gaps of every run whose end is now known,
        and drop what is resolved from ``done.json`` (spec 0009 AC-7, amended 2026-10-16). A tail gap in
        a day whose manifest is already written rewrites it. True if ``done`` changed."""
        done = self.done
        state = TailState(
            run_ends={k: dict(v) for k, v in done.run_ends.items()},
            open_outages={
                run: [Outage(o["start_ns"], None, o["reason"]) for o in lines]
                for run, lines in done.open_outages.items()
            },
            pending_tails={k: dict(v) for k, v in done.pending_tails.items()},
        )
        books = {}
        for instrument, raw in done.streams.items():
            book = GapState.from_dict(raw)
            books[instrument] = StreamMark(book.run, book.last_ts)
        silences = {}
        for key, raw in done.context_streams.items():
            quiet = SilenceState.from_dict(raw)
            silences[key] = StreamMark(quiet.run, quiet.last_ts)
        gaps = resolve_tails(
            state,
            books=books,
            silences=silences,
            silence_tables=SILENCE_TABLES,
            raw_tables=RAW_FEED_TABLES,
            stop_table=OPEN_INTEREST_TABLE,
            silence_ns=self._context_silence_ns,
        )
        if gaps:
            write_gaps(self._store, gaps)
            # A tail reconnect's hours could not be filled by the raw feed (spec 0013 AC-7).
            done.feed_down.update(h for g in gaps if g.kind == "reconnect" for h in hours_inside(g.start_ns, g.end_ns))
            for gap in gaps:
                log.info(
                    "context tail gap",
                    extra={
                        "fields": {
                            "code": "context_gap",
                            "tail": True,
                            "stream": gap.stream,
                            "instrument_id": gap.instrument_id,
                            "kind": gap.kind,
                            "start_ns": gap.start_ns,
                            "end_ns": gap.end_ns,
                            "duration_s": round(gap.duration_s, 3),
                        }
                    },
                )
            rewrite_days(
                self._store,
                done,
                {day for g in gaps for day in range(day_of(g.start_ns), day_of(g.end_ns) + 1, DAY_NS)},
            )
        open_outages = {
            run: [{"start_ns": o.start_ns, "reason": o.reason} for o in lines]
            for run, lines in state.open_outages.items()
        }
        changed = (state.run_ends, open_outages, state.pending_tails) != (
            done.run_ends,
            done.open_outages,
            done.pending_tails,
        )
        done.run_ends, done.open_outages, done.pending_tails = state.run_ends, open_outages, state.pending_tails
        return changed or bool(gaps)

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
            seen_bad = self.done.holes.get(run, {}).get("bad_lines", 0)
            if parsed.bad_lines > (seen_bad if isinstance(seen_bad, int) else 0):
                log.error(
                    "sequence holes file has unreadable lines, each covered by a fail safe hole",
                    extra={"fields": {"code": "sequence_file_bad_lines", "run": run, "bad_lines": parsed.bad_lines}},
                )
            if run == current:
                holes[run] = parsed
            else:
                holes[run] = parsed.with_fail_safe()
            self.done.holes[run] = {
                "read_bytes": read_bytes,
                "checked_to_ns": parsed.checked_to_ns,
                "closed": parsed.closed,
                "bad_lines": parsed.bad_lines,
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


def run_retention(
    store: CatalogStore, *, state_path: Path, needs_backup: bool, cap_bytes: int, floor_bytes: int, now_ns: int
) -> None:
    """AC-6: delete the oldest whole hours while the catalog is over its cap or free space under its
    floor. With ``needs_backup``, only hours the backup state says are in B2, and nothing without a
    valid state; without it, any hour last written before the newest ``LOCAL_KEEP_NS``."""
    cutoff_ns = None if needs_backup else now_ns - LOCAL_KEEP_NS
    try:
        raw = read_backup_state(state_path) if needs_backup else None
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

    names = settings.recorder_instruments
    # Offline converts the old backlog, recorded with whatever instruments were set back then.
    recorded = None if offline or names is None else frozenset(n.strip() for n in names.split(",") if n.strip())

    def fresh() -> Compactor:
        return Compactor(
            store,
            # Offline, every file was closed long ago, so there is nothing to wait for.
            grace_ns=0 if offline else settings.compactor_grace_s * SECOND_NS,
            ratio=settings.compactor_parquet_ratio,
            floor_bytes=int(settings.catalog_free_floor_gb * GB),
            silence_ns=settings.book_silence_gap_s * SECOND_NS,
            context_silence_ns=settings.context_silence_s * SECOND_NS,
            window_ns=settings.compactor_reorder_window_s * SECOND_NS,
            max_rows=settings.compactor_reorder_max_rows,
            beat=heartbeat,
            offline=offline,
            recorded=recorded,
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
                needs_backup=settings.catalog_needs_backup,
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
