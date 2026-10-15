"""Recorder entry point: a Nautilus node with data clients only and an in memory cache."""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import (
    FundingRateUpdate,
    IndexPriceUpdate,
    MarkPriceUpdate,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.instruments import CryptoPerpetual
from pydantic import SecretStr

from recorder.actor import (
    DiskGuardActor,
    DiskGuardConfig,
    RecordingActor,
    RecordingConfig,
    WriterFlushActor,
    WriterFlushConfig,
)
from recorder.disk import EXIT_DISK_FULL, wait_for_space
from recorder.queue_depth import QueueDepthActor, QueueDepthConfig
from recorder.settings import RecorderSettings
from recorder.volume import CatalogVolumeMissing, check_catalog_volume
from recorder.writer import BufferedFeatherWriter
from trading_core import env_guard
from trading_core.boot import boot
from trading_core.marketdata.context_feed import OUTAGES_FILE
from trading_core.marketdata.hours import GB
from trading_core.naming import node_id
from trading_core.node.context_feed import ContextFeedActor, ContextFeedConfig
from trading_core.node.context_types import LiquidationTick, OpenInterestUpdate
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.kraken import futures_data_client

log = logging.getLogger(__name__)

# What the stream records (spec 0007 AC-1, spec 0009 AC-1). The data engine publishes book deltas
# as OrderBookDeltas batches; their rows are OrderBookDelta.
RECORDED_TYPES: list[type] = [
    OrderBookDeltas,
    TradeTick,
    CryptoPerpetual,
    MarkPriceUpdate,
    IndexPriceUpdate,
    FundingRateUpdate,
    OpenInterestUpdate,
    LiquidationTick,
]
# The data engine's queue capacity (the Nautilus default), watched by QueueDepthActor (AC-13).
DATA_QUEUE_SIZE = 100_000
# The kernel sleeps this long after the actors stop, with the data clients still connected. The
# recorder has no orders to wait for, and data in that sleep is only held until close (spec 0012
# AC-5), so the stop reaches writer.close well inside the compose stop_grace_period.
POST_STOP_S = 1.0


def build_config(settings: RecorderSettings) -> TradingNodeConfig:
    env = settings.trading_env
    return TradingNodeConfig(
        trader_id=TraderId(node_id("RECORDER", env, os.environ.get("RECORDER_ID"))),
        logging=LoggingConfig(log_level=settings.log_level, log_colors=False),
        cache=CacheConfig(),  # in memory only: the recorder holds no orders or positions
        data_engine=LiveDataEngineConfig(qsize=DATA_QUEUE_SIZE),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
        # No streaming: main.py records through BufferedFeatherWriter (spec 0012).
        timeout_post_stop=POST_STOP_S,
    )


def run_path(catalog_path: str, run: str) -> Path:
    """The run folder ``live/<instance id>``, where the writer puts a run's feather files (spec 0012 AC-1)."""
    return Path(catalog_path) / "live" / run


def outages_path(catalog_path: str, run: str) -> Path:
    """The run's ``context_outages.jsonl``, in the run folder where the compactor reads it (spec 0009 AC-6)."""
    return run_path(catalog_path, run) / OUTAGES_FILE


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
    # A new feather file per stream after each UTC hour, which the compactor turns into Parquet
    # (spec 0007), held and written in batches (spec 0012). Subscribed as the kernel would.
    writer = BufferedFeatherWriter(
        run_path(settings.catalog_path, str(node.kernel.instance_id)),
        config_json=config.json(),
        cache=node.kernel.cache,
        clock=node.kernel.clock,
        include_types=RECORDED_TYPES,
        flush_max_messages=settings.recorder_flush_max_messages,
    )
    node.trader.subscribe("*", writer.write)
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    recording = RecordingActor(
        RecordingConfig(
            instrument_ids=settings.instrument_list,
            catalog_path=settings.catalog_path,
            sequence_confirm_s=settings.recorder_sequence_confirm_s,
        ),
        # Called in on_start, once the node is built: the run folder is named after the instance id
        # (spec 0007 Milestone 7 gate).
        run_id=lambda: str(node.kernel.instance_id),
    )
    node.trader.add_actor(recording)

    # Called only once the node is built and running.
    def queue_depth() -> int:
        return int(node.kernel.data_engine.data_qsize())

    node.trader.add_actor(
        QueueDepthActor(
            QueueDepthConfig(warn_pct=settings.recorder_queue_warn_pct),
            probe=queue_depth,
            capacity=config.data_engine.qsize,
        )
    )
    node.trader.add_actor(
        WriterFlushActor(WriterFlushConfig(flush_ms=settings.recorder_flush_ms), writer=writer, probe=queue_depth)
    )
    # Open interest and liquidations from Kraken's public websocket (spec 0009 AC-3); its outages go
    # to the run folder for the compactor's reconnect gaps (AC-6, AC-7). It never stops the recorder.
    node.trader.add_actor(
        ContextFeedActor(
            ContextFeedConfig(
                ws_url=settings.context_ws_url,
                instrument_ids=settings.instrument_list,
                reconnect_max_s=settings.context_reconnect_max_s,
                idle_s=settings.context_idle_s,
            ),
            outages_path=lambda: outages_path(settings.catalog_path, str(node.kernel.instance_id)),
        )
    )
    disk_guard = DiskGuardActor(DiskGuardConfig(catalog_path=settings.catalog_path, floor_bytes=floor_bytes))
    node.trader.add_actor(disk_guard)
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    try:
        node.run()
    finally:
        try:
            node.dispose()
        finally:
            # Writes what is still held, even when dispose raised (AC-5).
            writer.close()
    if disk_guard.guard.tripped or recording.watch.tripped or writer.tripped:
        sys.exit(EXIT_DISK_FULL)


if __name__ == "__main__":
    main()
