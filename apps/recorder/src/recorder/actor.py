"""The recorder's actors: market data subscriptions, and the disk floor guard (spec 0007 AC-7)."""

from datetime import timedelta
from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId

from recorder.disk import DiskGuard


class RecordingConfig(ActorConfig, frozen=True):
    instrument_ids: list[str]


class RecordingActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    def __init__(self, config: RecordingConfig) -> None:
        super().__init__(config)
        self._instrument_ids = [InstrumentId.from_str(i) for i in config.instrument_ids]

    def on_start(self) -> None:
        # The kernel's streaming writer receives everything on the bus; these
        # subscriptions make the data flow. Hourly Parquet conversion is feature 8.
        for instrument_id in self._instrument_ids:
            self.subscribe_order_book_deltas(instrument_id, BookType.L2_MBP)
            self.subscribe_trade_ticks(instrument_id)


class DiskGuardConfig(ActorConfig, frozen=True):
    catalog_path: str
    floor_bytes: int
    interval_secs: int = 10


class DiskGuardActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs ``DiskGuard`` every 10 s on the actor's clock. ``main.py`` exits 75 once it tripped."""

    def __init__(self, config: DiskGuardConfig) -> None:
        super().__init__(config)
        self.guard = DiskGuard(Path(config.catalog_path), config.floor_bytes)
        self._interval = timedelta(seconds=config.interval_secs)

    def on_start(self) -> None:
        self.clock.set_timer("disk_guard", self._interval, callback=self._check)

    def on_stop(self) -> None:
        self.clock.cancel_timer("disk_guard")

    def _check(self, _event: TimeEvent) -> None:
        self.guard.check()
