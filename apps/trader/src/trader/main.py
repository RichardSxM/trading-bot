"""Trader entry point. Runs a node with no strategy yet: public Kraken data, and in live the Kraken
execution client with its startup key check (spec 0003; allowed by the env guard from feature 19)."""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Final, Self

from nautilus_trader.adapters.kraken import (
    KRAKEN,
    KrakenExecClientConfig,
    KrakenLiveDataClientFactory,
    KrakenLiveExecClientFactory,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId, Venue
from pydantic import Field, SecretStr, model_validator

from trading_core import env_guard
from trading_core.boot import boot
from trading_core.env_guard import ALLOWED_EXEC_CLIENTS, ExecClientAllowlist
from trading_core.naming import node_id
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.key_actors import KeyAgeActor, KeyAgeConfig
from trading_core.node.key_check import watch_key_check
from trading_core.node.kraken import futures_data_client, futures_exec_client
from trading_core.redis_check import redis_reachable
from trading_core.secrets import (
    KRAKEN_API_KEY,
    KRAKEN_API_SECRET,
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
)
from trading_core.settings import Settings

log = logging.getLogger("trader")

# A failed live key check waits this long before exiting, so Compose's restart policy retries
# every few minutes instead of hammering Kraken into a rate ban (spec 0003 AC-8).
KEY_CHECK_FAIL_BACKOFF_S: Final = 300.0

KrakenUrls = tuple[str, str]


class TraderSettings(Settings):
    # UTC date the live Kraken key was created; required in live (spec 0003 AC-9).
    kraken_key_rotated_on: date | None = None
    # How long a live start waits for an authenticated Kraken account (spec 0003 AC-8).
    kraken_key_check_timeout_s: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _live_needs_a_valid_rotation_date(self) -> Self:
        if self.trading_env != "live":
            return self
        if self.kraken_key_rotated_on is None:
            raise ValueError("KRAKEN_KEY_ROTATED_ON is required in live")
        if self.kraken_key_rotated_on > datetime.now(UTC).date():
            raise ValueError("KRAKEN_KEY_ROTATED_ON is in the future")
        return self


def trader_secrets(settings: Settings) -> tuple[str, ...]:
    """The secret files the trader loads: its database and Redis passwords, plus the Kraken key in live."""
    names = (POSTGRES_PASSWORD, REDIS_PASSWORD)
    return (*names, KRAKEN_API_KEY, KRAKEN_API_SECRET) if settings.trading_env == "live" else names


def build_config(
    settings: Settings, secrets: Mapping[str, SecretStr], kraken_urls: KrakenUrls | None = None
) -> TradingNodeConfig:
    """The node config. In live it adds the Kraken execution client with the keys from the secret files.

    ``kraken_urls`` (http, ws) overrides the Kraken endpoints; only the canary leak test sets it.
    """
    env = settings.trading_env
    exec_clients: dict[str, KrakenExecClientConfig] = {}
    if env == "live":
        http, ws = kraken_urls or (None, None)
        exec_clients[KRAKEN] = futures_exec_client(
            secrets[KRAKEN_API_KEY].get_secret_value(),
            secrets[KRAKEN_API_SECRET].get_secret_value(),
            base_url_http=http,
            base_url_ws=ws,
        )
    return TradingNodeConfig(
        trader_id=TraderId(node_id("TRADER", env, os.environ.get("TRADER_ID"))),
        logging=LoggingConfig(log_level=settings.log_level, log_colors=False),
        # Keys stay stable across restarts (trader-TRADER-<ENV>:...) so the trader
        # reloads its state, then reconciles with Kraken (feature 11).
        cache=CacheConfig(
            # Logs in as its own Redis ACL user (spec 0003).
            database=DatabaseConfig(
                type="redis",
                host=settings.redis_host,
                port=settings.redis_port,
                username=settings.redis_username,
                password=secrets[REDIS_PASSWORD].get_secret_value(),
            ),
            use_trader_prefix=True,
            use_instance_id=False,
            flush_on_start=False,
        ),
        # Reconciliation with Kraken arrives with feature 11.
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
        exec_clients=exec_clients,
    )


def guard(
    settings: Settings, secrets: Mapping[str, SecretStr], allowlist: ExecClientAllowlist = ALLOWED_EXEC_CLIENTS
) -> None:
    env_guard.check_exec_clients(
        build_config(settings, secrets).exec_clients, process="trader", env=settings.trading_env, allowlist=allowlist
    )


def kraken_account_confirmed(cache: Cache, since_ns: int) -> bool:
    """True once the cache holds a Kraken account whose latest state arrived at or after ``since_ns``.

    On a restart Nautilus reloads the last run's account from Redis before Kraken connects, so
    only a state created by this run proves the key works (spec 0003 AC-8).
    """
    account = cache.account_for_venue(Venue(KRAKEN))
    return account is not None and account.last_event.ts_init >= since_ns


def run_node(
    config: TradingNodeConfig,
    settings: TraderSettings,
    key_check_timeout_s: float,
    fail_backoff_s: float = KEY_CHECK_FAIL_BACKOFF_S,
) -> int:
    """Build and run the node until it stops; return the process exit code.

    In live the node also runs the key age actor and the startup key check. The check is a
    task on the node's event loop, not an actor: when the Kraken client cannot connect,
    Nautilus never starts the trader, so an actor would never run. A failed check stops the
    node and makes this return 1 after ``fail_backoff_s``.
    """
    node = TradingNode(config=config)
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    failed: list[bool] = []
    if settings.trading_env == "live" and settings.kraken_key_rotated_on is not None:
        node.trader.add_actor(KeyAgeActor(KeyAgeConfig(rotated_on=settings.kraken_key_rotated_on)))
        started_ns = time.time_ns()

        def stop() -> None:
            failed.append(True)
            node.stop()

        loop = node.get_event_loop()
        if loop is None:  # the live node always has one; without it the check could not run
            raise RuntimeError("trading node has no event loop for the Kraken key check")
        # Kept in a local so the task lives as long as the node runs; a crash fails closed.
        key_check = loop.create_task(
            watch_key_check(
                lambda: kraken_account_confirmed(node.cache, started_ns),
                stop,
                key_check_timeout_s,
                poll_s=min(1.0, key_check_timeout_s / 4),
            )
        )

        def key_check_ended(task: asyncio.Task[object]) -> None:
            if task.cancelled() or task.exception() is None:
                return
            log.critical(
                "kraken key check crashed, stopping the node",
                exc_info=task.exception(),
                extra={"fields": {"code": "kraken_key_check_crashed"}},
            )
            stop()

        key_check.add_done_callback(key_check_ended)
    if config.data_clients:
        node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    if config.exec_clients:
        node.add_exec_client_factory(KRAKEN, KrakenLiveExecClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()
    if failed:
        time.sleep(fail_backoff_s)
        return 1
    return 0


def main() -> None:
    started = boot("node", settings_cls=TraderSettings, guard=guard, secrets=trader_secrets)
    settings = started.settings
    if not redis_reachable(settings.redis_host, settings.redis_port):
        log.critical(
            "redis unreachable, refusing to start",
            extra={"fields": {"host": settings.redis_host, "port": settings.redis_port}},
        )
        sys.exit(1)

    code = run_node(build_config(settings, started.secrets), settings, settings.kraken_key_check_timeout_s)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
