"""The recorder's actors: market data subscriptions with the sequence watch (spec 0007 AC-15), the
market context subscriptions (spec 0009 AC-1), the buffered writer's ticks (spec 0012), and the disk
floor guard (spec 0007 AC-7)."""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId

from recorder.disk import DiskGuard, stop_process
from recorder.sequence_holes import HOLES_FILE, SequenceWatch
from recorder.writer import BufferedFeatherWriter
from trading_core.marketdata.hours import SECOND_NS


class RecordingConfig(ActorConfig, frozen=True):
    instrument_ids: list[str]
    catalog_path: str
    sequence_confirm_s: int = 600
    holes_every_s: int = 60


class RecordingActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Subscribes the recorded data, and watches the run's message counter for lost book messages.

    ``run_id`` names the run folder under ``live/`` (the node's instance id, known once the node is
    built). ``main.py`` exits 75 once ``watch.tripped``.
    """

    def __init__(
        self, config: RecordingConfig, run_id: Callable[[], str], *, stop: Callable[[], None] = stop_process
    ) -> None:
        super().__init__(config)
        self._instrument_ids = [InstrumentId.from_str(i) for i in config.instrument_ids]
        live = Path(config.catalog_path) / "live"
        self.watch = SequenceWatch(
            lambda: live / run_id() / HOLES_FILE, confirm_ns=config.sequence_confirm_s * SECOND_NS, stop=stop
        )
        self._holes_every = timedelta(seconds=config.holes_every_s)

    def on_start(self) -> None:
        # The tracker and the holes file come first, so it sees the run's first delta.
        self.watch.start(self.clock.timestamp_ns())
        self.clock.set_timer("sequence_holes", self._holes_every, callback=self._tick)
        # The buffered writer receives everything on the bus; these
        # subscriptions make the data flow.
        for instrument_id in self._instrument_ids:
            self.subscribe_order_book_deltas(instrument_id, BookType.L2_MBP)
            self.subscribe_trade_ticks(instrument_id)
            # The perp's own context (spec 0009 AC-1).
            self.subscribe_mark_prices(instrument_id)
            self.subscribe_index_prices(instrument_id)
            self.subscribe_funding_rates(instrument_id)

    def on_stop(self) -> None:
        self.clock.cancel_timer("sequence_holes")
        self.watch.stop()

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        now = self.clock.timestamp_ns()
        observe = self.watch.observe
        for delta in deltas.deltas:
            observe(delta.sequence, delta.ts_init, now)

    def _tick(self, _event: TimeEvent) -> None:
        self.watch.tick(self.clock.timestamp_ns())


class WriterFlushConfig(ActorConfig, frozen=True):
    flush_ms: int = 1_000
    stats_every_s: int = 60


class WriterFlushActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs the buffered writer's flush tick and its 60 s stats tick on the node clock, and flushes
    every stream at stop (spec 0012 AC-2, AC-5, AC-6).

    ``probe`` reads the data engine's queue depth (``data_qsize()`` of the built node), from ``main.py``.
    """

    def __init__(self, config: WriterFlushConfig, *, writer: BufferedFeatherWriter, probe: Callable[[], int]) -> None:
        super().__init__(config)
        self._writer = writer
        self._probe = probe
        self._flush_every = timedelta(milliseconds=config.flush_ms)
        self._stats_every = timedelta(seconds=config.stats_every_s)

    def on_start(self) -> None:
        self._writer.start_stats(self.clock.timestamp_ns())
        self.clock.set_timer("writer_flush", self._flush_every, callback=self._flush)
        self.clock.set_timer("writer_stats", self._stats_every, callback=self._stats)

    def on_stop(self) -> None:
        self.clock.cancel_timer("writer_flush")
        self.clock.cancel_timer("writer_stats")
        self._writer.flush_on_stop()

    def _flush(self, event: TimeEvent) -> None:
        # ts_init is when the timer fired, ts_event when it was due: the event loop's lag.
        self._writer.flush_all(queue_depth=self._probe(), tick_lag_ns=max(0, event.ts_init - event.ts_event))

    def _stats(self, _event: TimeEvent) -> None:
        self._writer.take_stats(self.clock.timestamp_ns())


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
