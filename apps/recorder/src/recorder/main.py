"""Recorder entry point: a Nautilus node with data clients only and an in memory cache."""

import logging
import os
import sys
from collections.abc import Mapping
from datetime import time
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs; only Timedelta for StreamingConfig
from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    StreamingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import OrderBookDeltas, TradeTick
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.writer import RotationMode
from pydantic import SecretStr

from recorder.actor import DiskGuardActor, DiskGuardConfig, RecordingActor, RecordingConfig
from recorder.disk import EXIT_DISK_FULL, wait_for_space
from recorder.queue_depth import QueueDepthActor, QueueDepthConfig
from recorder.settings import RecorderSettings
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from trading_core import env_guard
from trading_core.boot import boot
from trading_core.marketdata.hours import GB
from trading_core.naming import node_id
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.kraken import futures_data_client

log = logging.getLogger(__name__)

# What the stream records (spec 0007 AC-1). The data engine publishes book deltas as
# OrderBookDeltas batches; their rows are OrderBookDelta.
RECORDED_TYPES: list[type] = [OrderBookDeltas, TradeTick, CryptoPerpetual]
# The data engine's queue capacity (the Nautilus default), watched by QueueDepthActor (AC-13).
DATA_QUEUE_SIZE = 100_000


def build_config(settings: RecorderSettings) -> TradingNodeConfig:
    env = settings.trading_env
    return TradingNodeConfig(
        trader_id=TraderId(node_id("RECORDER", env, os.environ.get("RECORDER_ID"))),
        logging=LoggingConfig(log_level=settings.log_level, log_colors=False),
        cache=CacheConfig(),  # in memory only: the recorder holds no orders or positions
        data_engine=LiveDataEngineConfig(qsize=DATA_QUEUE_SIZE),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
        # A new feather file per stream after each UTC hour, which the compactor turns into
        # Parquet (spec 0007). The writer rotates on the first write past the boundary.
        streaming=StreamingConfig(
            catalog_path=settings.catalog_path,
            flush_interval_ms=1_000,
            include_types=RECORDED_TYPES,
            rotation_mode=RotationMode.SCHEDULED_DATES,
            rotation_interval=pd.Timedelta(hours=1),
            rotation_time=time(0, 0),
            rotation_timezone="UTC",
        ),
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
    # Below the hard floor, wait here instead of building the node: no crash loop (AC-7).
    floor_bytes = int(settings.catalog_hard_floor_gb * GB)
    wait_for_space(Path(settings.catalog_path), floor_bytes)
    config = build_config(settings)
    node = TradingNode(config=config)
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    node.trader.add_actor(RecordingActor(RecordingConfig(instrument_ids=settings.instrument_list)))
    node.trader.add_actor(
        QueueDepthActor(
            QueueDepthConfig(warn_pct=settings.recorder_queue_warn_pct),
            # Called only once the node is built and running.
            probe=lambda: node.kernel.data_engine.data_qsize(),
            capacity=config.data_engine.qsize,
        )
    )
    disk_guard = DiskGuardActor(DiskGuardConfig(catalog_path=settings.catalog_path, floor_bytes=floor_bytes))
    node.trader.add_actor(disk_guard)
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()
    if disk_guard.guard.tripped:
        sys.exit(EXIT_DISK_FULL)


if __name__ == "__main__":
    main()
