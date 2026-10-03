"""Trader entry point. Runs a node with no strategy yet: public Kraken data, and in live the Kraken
execution client with its startup key check (spec 0003; allowed by the env guard from feature 19).

Every env runs the control actor (pause and unpause, spec 0005) and writes the heartbeat and
``trader_state`` snapshots. SIGTERM stops the node gracefully: entry orders are cancelled,
positions and stops are kept.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from pydantic import Field, SecretStr, field_validator, model_validator
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from sqlalchemy import create_engine

from trader.snapshot_writer import log_writer_crash, write_heartbeats
from trading_core import env_guard
from trading_core.boot import boot
from trading_core.control.consumer import ControlConsumer
from trading_core.control.pause import pause_key
from trading_core.control.records import PostgresCommandRecorder
from trading_core.env_guard import ALLOWED_EXEC_CLIENTS, ExecClientAllowlist
from trading_core.naming import node_id
from trading_core.node.control import ControlActor, PauseController, shutdown_counts, trader_state
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.key_actors import KeyAgeActor, KeyAgeConfig
from trading_core.node.key_check import watch_key_check
from trading_core.node.kraken import futures_data_client, futures_exec_client
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env
from trading_core.redis_check import redis_reachable
from trading_core.secrets import (
    KRAKEN_API_KEY,
    KRAKEN_API_SECRET,
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
)
from trading_core.settings import Settings
from trading_core.snapshot.envelope import snapshot_key

log = logging.getLogger("trader")

# A failed live key check waits this long before exiting, so Compose's restart policy retries
# every few minutes instead of hammering Kraken into a rate ban (spec 0003 AC-8).
KEY_CHECK_FAIL_BACKOFF_S: Final = 300.0

# How long a stop waits for entry order cancels to be acknowledged (spec 0005 AC-9).
POST_STOP_S: Final = 10.0
# The control reader blocks 1 s per read, so its socket timeout sits above that.
CONTROL_SOCKET_TIMEOUT_S: Final = 3.0

KrakenUrls = tuple[str, str]


class TraderSettings(Settings):
    # UTC date the live Kraken key was created; required in live (spec 0003 AC-9).
    kraken_key_rotated_on: date | None = None
    # How long a live start waits for an authenticated Kraken account (spec 0003 AC-8).
    kraken_key_check_timeout_s: float = Field(default=60.0, gt=0)

    @field_validator("kraken_key_rotated_on", mode="before")
    @classmethod
    def _empty_rotation_date_is_unset(cls, value: object) -> object:
        # compose.live.yaml passes ${KRAKEN_KEY_ROTATED_ON:-}, so unset arrives as "".
        return None if isinstance(value, str) and not value.strip() else value

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
        # Cancels sent by the strategies on stop get this long to be acknowledged (spec 0005 AC-9).
        timeout_post_stop=POST_STOP_S,
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


def snapshot_client(settings: Settings, password: SecretStr) -> Redis:
    """The redis-py client for snapshot writes, as the trader's ACL user, with short timeouts."""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password.get_secret_value(),
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )


@dataclass(frozen=True)
class ControlParts:
    """What ``run_node`` needs from the control side: the actor, the ``trader_state`` reader, the stop counts."""

    actor: ControlActor
    read_state: Callable[[], Mapping[str, object] | None]
    shutdown_counts: Callable[[], Mapping[str, int]]


def control_parts(node: TradingNode, settings: Settings, secrets: Mapping[str, SecretStr]) -> ControlParts:
    """Build the pause gate, the control actor with its own Redis and Postgres logins, and the readers."""
    env = settings.trading_env
    password = secrets[REDIS_PASSWORD].get_secret_value()
    client = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password,
        socket_timeout=CONTROL_SOCKET_TIMEOUT_S,
        socket_connect_timeout=CONTROL_SOCKET_TIMEOUT_S,
    )
    start_client = SyncRedis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password,
        socket_timeout=CONTROL_SOCKET_TIMEOUT_S,
        socket_connect_timeout=CONTROL_SOCKET_TIMEOUT_S,
    )
    engine = create_engine(settings.database_url(secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True)
    gate = PauseGate()
    risk_engine = node.kernel.risk_engine

    def now() -> datetime:
        return datetime.now(UTC)

    controller = PauseController(gate, risk_engine, client, env, now)
    consumer = ControlConsumer(client, env, controller.execute, PostgresCommandRecorder(engine, Env(env)), now)

    def read_pause_key() -> str | bytes | None:
        value = start_client.get(pause_key(env))
        return value if isinstance(value, str | bytes) else None

    return ControlParts(
        actor=ControlActor(controller, read_pause_key, consumer.run),
        read_state=lambda: trader_state(gate, risk_engine, node.cache, settings.git_commit),
        shutdown_counts=lambda: shutdown_counts(gate, node.cache),
    )


def heartbeat_state(node: TradingNode, settings: Settings) -> dict[str, object]:
    """The heartbeat snapshot's data: the process, its commit, the trader state, and the data link."""
    return {
        "process": "trader",
        "git_commit": settings.git_commit,
        "trading_state": node.trader.state.name,
        "data_connected": bool(node.kernel.data_engine.check_connected()),
    }


def run_node(
    config: TradingNodeConfig,
    settings: TraderSettings,
    key_check_timeout_s: float,
    secrets: Mapping[str, SecretStr],
    fail_backoff_s: float = KEY_CHECK_FAIL_BACKOFF_S,
) -> int:
    """Build and run the node until it stops; return the process exit code.

    In every env the control actor is registered first (before any strategy), and tasks on the
    node's event loop write the heartbeat and ``trader_state`` snapshots for the dashboard
    (specs 0004, 0005). SIGTERM and SIGINT stop the node gracefully; a clean stop logs
    ``shutdown_graceful``. In live the node also runs the key age actor and the startup key
    check. The check is a task on the node's event loop, not an actor: when the Kraken client
    cannot connect, Nautilus never starts the trader, so an actor would never run. A failed
    check stops the node and makes this return 1 after ``fail_backoff_s``.
    """
    node = TradingNode(config=config)
    control = control_parts(node, settings, secrets)
    node.trader.add_actor(control.actor)
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
    loop = node.get_event_loop()
    if loop is None:  # the live node always has one; without it the dashboard would see no heartbeat
        raise RuntimeError("trading node has no event loop for the snapshot writer")
    snapshots = snapshot_client(settings, secrets[REDIS_PASSWORD])
    # Kept in locals so the tasks live as long as the node runs; a crash is logged, trading goes on.
    writers = [
        loop.create_task(write_heartbeats(snapshots, snapshot_key(settings.trading_env, name), read_state))
        for name, read_state in (
            ("heartbeat", lambda: heartbeat_state(node, settings)),
            ("trader_state", control.read_state),
        )
    ]
    for writer in writers:
        writer.add_done_callback(log_writer_crash)
    stop_signals = (signal.SIGTERM, signal.SIGINT)
    for signum in stop_signals:
        loop.add_signal_handler(signum, node.stop)
    try:
        node.run()
        log.info(
            "trader stopped",
            extra={"fields": {"code": "shutdown_graceful", **control.shutdown_counts()}},
        )
    finally:
        for signum in stop_signals:
            loop.remove_signal_handler(signum)
        for writer in writers:
            writer.cancel()
        # Let the cancelled writers finish on the stopped loop, so none is destroyed while pending.
        if not loop.is_closed() and not loop.is_running():
            loop.run_until_complete(asyncio.gather(*writers, return_exceptions=True))
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

    code = run_node(
        build_config(settings, started.secrets),
        settings,
        settings.kraken_key_check_timeout_s,
        started.secrets,
    )
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
