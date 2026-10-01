"""Heartbeat actor for Nautilus nodes: touches a file every 10 s for the container health check."""

from datetime import timedelta
from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent

DEFAULT_HEARTBEAT_FILE = "/run/heartbeat/alive"


class HeartbeatConfig(ActorConfig, frozen=True):
    path: str = DEFAULT_HEARTBEAT_FILE
    interval_secs: int = 10


class HeartbeatActor(Actor):
    def __init__(self, config: HeartbeatConfig | None = None) -> None:
        config = config or HeartbeatConfig()
        super().__init__(config)
        self._path = Path(config.path)
        self._interval = timedelta(seconds=config.interval_secs)

    def on_start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.clock.set_timer("heartbeat", self._interval, callback=self._beat, fire_immediately=True)

    def on_stop(self) -> None:
        self.clock.cancel_timer("heartbeat")

    def _beat(self, _event: TimeEvent) -> None:
        self._path.touch()
