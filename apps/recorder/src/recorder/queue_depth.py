"""Watches the data engine's queue depth, so recorder lag shows while it happens (spec 0007 AC-13).

A full queue makes Nautilus 1.231.0 reorder messages, which the compactor repairs within its
window. The kernel stops actors before the data engine cancels its pending puts, so the depth
logged at stop shows whether rows were lost.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent

from trading_core.marketdata.queue_depth import STOP_QUEUE_FULL, queue_alert, stop_code

log = logging.getLogger(__name__)

Probe = Callable[[], int]


class QueueWatch:
    """The queue checks without Nautilus: a depth probe, the capacity, and when each code was logged."""

    def __init__(self, probe: Probe, capacity: int, warn_pct: int) -> None:
        self._probe = probe
        self._capacity = capacity
        self._warn_pct = warn_pct
        self._last_logged: dict[str, int] = {}

    def tick(self, now_ns: int) -> None:
        """Log ``recorder_queue_high`` or ``recorder_queue_full`` when due."""
        depth = self._probe()
        code = queue_alert(depth, self._capacity, warn_pct=self._warn_pct, now_ns=now_ns, last_logged=self._last_logged)
        if code is None:
            return
        self._last_logged[code] = now_ns
        level = logging.ERROR if depth >= self._capacity else logging.WARNING
        log.log(level, "recorder data queue deep", extra={"fields": self._fields(code, depth)})

    def stop(self) -> None:
        """Log the depth at stop: rows still queued are lost when shutdown cancels the pending puts."""
        depth = self._probe()
        code = stop_code(depth, self._capacity)
        level = logging.ERROR if code == STOP_QUEUE_FULL else logging.INFO
        log.log(level, "recorder data queue at stop", extra={"fields": self._fields(code, depth)})

    def _fields(self, code: str, depth: int) -> dict[str, object]:
        return {"code": code, "depth": depth, "capacity": self._capacity}


class QueueDepthConfig(ActorConfig, frozen=True):
    warn_pct: int = 50
    interval_secs: int = 1


class QueueDepthActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs ``QueueWatch`` every second on the actor's clock, and once more at stop.

    ``probe`` reads the depth (``data_qsize()`` of the built node) and ``capacity`` is the data
    engine's ``qsize``; both come from ``main.py``, since a config holds only plain values.
    """

    def __init__(self, config: QueueDepthConfig, *, probe: Probe, capacity: int) -> None:
        super().__init__(config)
        self._watch = QueueWatch(probe, capacity, config.warn_pct)
        self._interval = timedelta(seconds=config.interval_secs)

    def on_start(self) -> None:
        self.clock.set_timer("queue_depth", self._interval, callback=self._check)

    def on_stop(self) -> None:
        self.clock.cancel_timer("queue_depth")
        self._watch.stop()

    def _check(self, _event: TimeEvent) -> None:
        self._watch.tick(self.clock.timestamp_ns())
