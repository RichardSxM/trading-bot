"""Recorder entry point: a Nautilus node with data clients only and an in memory cache."""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
from nautilus_trader.config import (
    CacheConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    StreamingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from pydantic import SecretStr

from recorder.actor import RecordingActor, RecordingConfig
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from trading_core import env_guard
from trading_core.boot import boot
from trading_core.naming import node_id
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.kraken import futures_data_client
from trading_core.settings import Settings

log = logging.getLogger(__name__)


class RecorderSettings(Settings):
    # Pinned from the adapter's instrument provider (Kraken's live public data).
    recorder_instruments: str = "PF_XBTUSD.KRAKEN,PF_ETHUSD.KRAKEN"
    catalog_path: str = "/data/catalog"
    # True on the VPS (compose.vps.yaml): refuse to start unless the extra disk is mounted.
    catalog_require_marker: bool = False

    @property
    def instrument_list(self) -> list[str]:
        return [i.strip() for i in self.recorder_instruments.split(",") if i.strip()]


def build_config(settings: RecorderSettings) -> TradingNodeConfig:
    env = settings.trading_env
    return TradingNodeConfig(
        trader_id=TraderId(node_id("RECORDER", env, os.environ.get("RECORDER_ID"))),
        logging=LoggingConfig(log_level=settings.log_level, log_colors=False),
        cache=CacheConfig(),  # in memory only: the recorder holds no orders or positions
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
        streaming=StreamingConfig(catalog_path=settings.catalog_path, flush_interval_ms=1_000),
    )


def guard(settings: RecorderSettings, secrets: Mapping[str, SecretStr]) -> None:
    env_guard.check_exec_clients(build_config(settings).exec_clients, process="recorder", env=settings.trading_env)


def main() -> None:
    # The recorder holds no secret at all (spec 0003).
    settings = boot("node", settings_cls=RecorderSettings, guard=guard).settings
    # Before the node exists, so a missing disk gets nothing written (spec 0005 AC-18).
    try:
        check_catalog_volume(Path(settings.catalog_path), required=settings.catalog_require_marker)
    except CatalogVolumeMissing as exc:
        log.critical(
            "catalog volume missing, refusing to start",
            extra={"fields": {"code": "catalog_volume_missing", "path": str(exc.path)}},
        )
        sys.exit(1)
    node = TradingNode(config=build_config(settings))
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    node.trader.add_actor(RecordingActor(RecordingConfig(instrument_ids=settings.instrument_list)))
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
