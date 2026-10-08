"""The compactor service: every ``COMPACTOR_INTERVAL_S`` it converts closed feather hours (spec 0007).

Usage: ``python -m recorder.compactor.main``. A pass that fails logs ``compactor_pass_failed`` and
drops its unfinished hour; the next pass starts again from ``done.json``.
"""

import logging
import os
import time
from pathlib import Path

from pydantic import Field

from recorder.compactor.convert import DeltaStream, FeatherFile
from recorder.compactor.store import LIVE_DELTAS, CatalogStore
from trading_core.boot import boot
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.settings import Settings

log = logging.getLogger(__name__)


class CompactorSettings(Settings):
    catalog_path: str = "/data/catalog"
    # The wait after a file's last write before it is read, so an hour is never finished early.
    compactor_grace_s: int = Field(default=300, ge=0)
    compactor_interval_s: int = Field(default=60, gt=0)


def current_run(store: CatalogStore) -> str | None:
    """The run folder the recorder writes now: the newest by creation time."""
    runs = [p for p in store.live.iterdir() if p.is_dir()] if store.live.exists() else []
    return max(runs, key=lambda p: p.stat().st_ctime).name if runs else None


def delta_files(store: CatalogStore) -> dict[str, list[FeatherFile]]:
    """Every delta feather file per instrument, oldest first, across all runs."""
    found: dict[str, list[FeatherFile]] = {}
    if not store.live.exists():
        return found
    for run in store.live.iterdir():
        for folder in (run / LIVE_DELTAS).glob("*") if run.is_dir() else ():
            for path in folder.glob("*.feather"):
                found.setdefault(folder.name, []).append(FeatherFile.parse(path, run.name))
    return {instrument: sorted(files) for instrument, files in found.items()}


def is_closed(file: FeatherFile, files: list[FeatherFile], current: str | None) -> bool:
    """Closed when its run is no longer the current one, or its run has a newer file (it rotated)."""
    if file.run != current:
        return True
    return any(other.run == file.run and other.created_ns > file.created_ns for other in files)


class Compactor:
    """The conversion state kept in memory between passes; ``done.json`` is the durable part."""

    def __init__(self, store: CatalogStore, grace_ns: int) -> None:
        self._store = store
        self._grace_ns = grace_ns
        self.done = store.load_done()
        self.streams: dict[str, DeltaStream] = {}

    def run_pass(self, now_ns: int) -> None:
        """Feed every closed and settled file in order, then finish hours with nothing left to read."""
        current = current_run(self._store)
        for instrument, files in delta_files(self._store).items():
            stream = self.streams.get(instrument)
            if stream is None:
                stream = self.streams[instrument] = DeltaStream(self._store, instrument, self.done)
            for file in files:
                if not is_closed(file, files, current) or now_ns - file.path.stat().st_mtime_ns < self._grace_ns:
                    break  # files are read strictly in order
                stream.feed(file, self.done)
            open_hour = stream.open
            more_source = open_hour is not None and any(
                str(f.path.relative_to(self._store.root)) not in stream.fed and f.created_ns < open_hour.hour + HOUR_NS
                for f in files
            )
            stream.finish_if_passed(now_ns=now_ns, grace_ns=self._grace_ns, more_source=more_source, done=self.done)

    def abort(self) -> None:
        """Drop every unfinished hour."""
        for stream in self.streams.values():
            stream.abort()


def main() -> None:
    settings = boot("node", settings_cls=CompactorSettings).settings
    store = CatalogStore(Path(settings.catalog_path))
    grace_ns = settings.compactor_grace_s * SECOND_NS
    heartbeat = Path(os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive"))
    compactor = Compactor(store, grace_ns)
    while True:
        try:
            compactor.run_pass(time.time_ns())
        except Exception as exc:
            log.exception(
                "compactor pass failed",
                extra={"fields": {"code": "compactor_pass_failed", "error": type(exc).__name__}},
            )
            compactor.abort()
            compactor = Compactor(store, grace_ns)
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.touch()
        time.sleep(settings.compactor_interval_s)


if __name__ == "__main__":
    main()
