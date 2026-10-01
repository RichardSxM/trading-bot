"""Trader entry point. The scaffold runs a node with no strategy and a public data client only."""

import logging
import os
import sys
from collections.abc import Mapping

from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from pydantic import SecretStr

from trading_core import env_guard
from trading_core.boot import boot
from trading_core.naming import node_id
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.kraken import futures_data_client
from trading_core.redis_check import redis_reachable
from trading_core.secrets import POSTGRES_PASSWORD
from trading_core.settings import Settings

log = logging.getLogger("trader")


def build_config(settings: Settings) -> TradingNodeConfig:
    env = settings.trading_env
    return TradingNodeConfig(
        trader_id=TraderId(node_id("TRADER", env, os.environ.get("TRADER_ID"))),
        logging=LoggingConfig(log_level=settings.log_level, log_colors=False),
        # Keys stay stable across restarts (trader-TRADER-<ENV>:...) so the trader
        # reloads its state, then reconciles with Kraken (feature 11).
        cache=CacheConfig(
            database=DatabaseConfig(type="redis", host=settings.redis_host, port=settings.redis_port),
            use_trader_prefix=True,
            use_instance_id=False,
            flush_on_start=False,
        ),
        # No execution client until features 4 and 6, so nothing to reconcile yet.
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
    )


def guard(settings: Settings, secrets: Mapping[str, SecretStr]) -> None:
    env_guard.check_exec_clients(build_config(settings).exec_clients, process="trader", env=settings.trading_env)


def main() -> None:
    settings = boot("node", guard=guard, secrets=(POSTGRES_PASSWORD,)).settings
    if not redis_reachable(settings.redis_host, settings.redis_port):
        log.critical(
            "redis unreachable, refusing to start",
            extra={"fields": {"host": settings.redis_host, "port": settings.redis_port}},
        )
        sys.exit(1)

    node = TradingNode(config=build_config(settings))
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
