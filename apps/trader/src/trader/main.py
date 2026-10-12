"""Trader entry point. Runs a node on public Kraken data.

In demo it runs the paper loop (spec 0006): the ``placeholder_timed`` strategy on the sandbox
execution client, the recording actor writing to Postgres off the event loop, and the
``position`` and ``account`` snapshots. In live it runs the Kraken execution client with its
startup key check (spec 0003; allowed by the env guard from feature 19) and no strategy yet.

Every env runs the control actor (pause and unpause, spec 0005) and writes the heartbeat and
``trader_state`` snapshots. SIGTERM stops the node gracefully: entry orders are cancelled,
positions and stops are kept, and in demo the recording queue is drained or spilled.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, Protocol, Self
from uuid import UUID

from nautilus_trader.adapters.kraken import (
    KRAKEN,
    KrakenLiveDataClientFactory,
    KrakenLiveExecClientFactory,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveExecClientConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Currency
from pydantic import Field, SecretStr, field_validator, model_validator
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from trader.snapshot_writer import log_writer_crash, write_heartbeats
from trading_core import env_guard
from trading_core.boot import boot
from trading_core.control.consumer import ControlConsumer
from trading_core.control.halt import halt_key
from trading_core.control.pause import pause_key
from trading_core.control.records import PostgresCommandRecorder
from trading_core.env_guard import ALLOWED_EXEC_CLIENTS, SANDBOX, ExecClientAllowlist
from trading_core.naming import node_id
from trading_core.node.context_feed import ContextFeedActor, ContextFeedConfig
from trading_core.node.control import (
    ControlActor,
    KillSwitch,
    PaperStatus,
    PauseController,
    RiskEngine,
    control_execute,
    shutdown_counts,
    trader_state,
)
from trading_core.node.heartbeat import HeartbeatActor, HeartbeatConfig
from trading_core.node.key_actors import KeyAgeActor, KeyAgeConfig
from trading_core.node.key_check import watch_key_check
from trading_core.node.kraken import BTC_PERP, futures_data_client, futures_exec_client
from trading_core.node.loss_limits import LossLimitActor
from trading_core.node.paper_snapshots import account_snapshot, paper_status, position_snapshot
from trading_core.node.placeholder import STRATEGY_NAME, PlaceholderConfig, PlaceholderTimed
from trading_core.node.recording import RecordingActor
from trading_core.node.sandbox import FeeSandboxExecClientFactory, sandbox_exec_client
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording import repository
from trading_core.recording.enums import Env, RunMode, RunStatus, StrategyKind
from trading_core.recording.queries import LossSeed, loss_seed
from trading_core.recording.rows import JsonObject
from trading_core.recording.writer import RecordingWriter, deadletter_path, replay_spool, spool_path
from trading_core.redis_check import redis_reachable
from trading_core.risk.limits import LossLimits
from trading_core.secrets import (
    KRAKEN_API_KEY,
    KRAKEN_API_SECRET,
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
)
from trading_core.settings import ContextSettings, LossLimitSettings, PaperLoopSettings, Settings
from trading_core.snapshot.envelope import snapshot_key
from trading_core.strategy.blocks import EntryBlocks

log = logging.getLogger("trader")

# A failed live key check waits this long before exiting, so Compose's restart policy retries
# every few minutes instead of hammering Kraken into a rate ban (spec 0003 AC-8).
KEY_CHECK_FAIL_BACKOFF_S: Final = 300.0

# How long a stop waits for entry order cancels to be acknowledged (spec 0005 AC-9).
POST_STOP_S: Final = 10.0
# The stop and the recording drain after it must fit inside Compose's 30 s stop grace (spec 0006).
STOP_BUDGET_S: Final = 25
# Keys deleted per UNLINK by the demo cache purge.
PURGE_BATCH: Final = 500
# The control reader blocks 1 s per read, so its socket timeout sits above that.
CONTROL_SOCKET_TIMEOUT_S: Final = 3.0

KrakenUrls = tuple[str, str]


class TraderSettings(PaperLoopSettings, ContextSettings, LossLimitSettings):
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

    @model_validator(mode="after")
    def _stop_fits_the_grace(self) -> Self:
        if POST_STOP_S + float(self.recording_drain_s) > STOP_BUDGET_S:
            raise ValueError(f"POST_STOP_S ({POST_STOP_S}) plus RECORDING_DRAIN_S must be at most {STOP_BUDGET_S}")
        return self


def trader_secrets(settings: Settings) -> tuple[str, ...]:
    """The secret files the trader loads: its database and Redis passwords, plus the Kraken key in live."""
    names = (POSTGRES_PASSWORD, REDIS_PASSWORD)
    return (*names, KRAKEN_API_KEY, KRAKEN_API_SECRET) if settings.trading_env == "live" else names


def build_config(
    settings: TraderSettings, secrets: Mapping[str, SecretStr], kraken_urls: KrakenUrls | None = None
) -> TradingNodeConfig:
    """The node config: the sandbox execution client in demo (spec 0006), Kraken's in live.

    In live it adds the Kraken execution client with the keys from the secret files.
    ``kraken_urls`` (http, ws) overrides the Kraken endpoints; only the canary leak test sets it.
    The RiskEngine refuses any order over ``RISK_MAX_ORDER_NOTIONAL_USD`` in every env.
    """
    env = settings.trading_env
    exec_clients: dict[str, LiveExecClientConfig] = {}
    if env == "demo":
        exec_clients[SANDBOX] = sandbox_exec_client(settings)
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
        risk_engine=LiveRiskEngineConfig(
            # Nautilus parses each value with Decimal(value), so the exact cap goes in as a string.
            max_notional_per_order={BTC_PERP: str(settings.risk_max_order_notional_usd)},  # type: ignore[dict-item]
        ),
        data_clients={KRAKEN: futures_data_client()},
        exec_clients=exec_clients,
        # Cancels sent by the strategies on stop get this long to be acknowledged (spec 0005 AC-9).
        timeout_post_stop=POST_STOP_S,
    )


def guard(
    settings: TraderSettings, secrets: Mapping[str, SecretStr], allowlist: ExecClientAllowlist = ALLOWED_EXEC_CLIENTS
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
    """What ``run_node`` needs from the control side: the actor, the kill switch, the gate, the risk engine, and
    the stop counts."""

    actor: ControlActor
    kill_switch: KillSwitch
    risk_engine: RiskEngine
    shutdown_counts: Callable[[], Mapping[str, int]]
    gate: PauseGate = field(default_factory=PauseGate)


def control_parts(node: TradingNode, settings: LossLimitSettings, secrets: Mapping[str, SecretStr]) -> ControlParts:
    """Build the gate, the kill switch, the control actor with its own Redis and Postgres logins, and the readers."""
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
    kill_switch = KillSwitch(gate, risk_engine, client, env, now, node.cache, settings.kill_switch_flat_timeout_s)
    execute = control_execute(controller, kill_switch)
    consumer = ControlConsumer(client, env, execute, PostgresCommandRecorder(engine, Env(env)), now)

    def read_key(key: str) -> Callable[[], str | bytes | None]:
        def read() -> str | bytes | None:
            value = start_client.get(key)
            return value if isinstance(value, str | bytes) else None

        return read

    return ControlParts(
        actor=ControlActor(controller, kill_switch, read_key(pause_key(env)), read_key(halt_key(env)), consumer.run),
        kill_switch=kill_switch,
        risk_engine=risk_engine,
        shutdown_counts=lambda: shutdown_counts(gate, node.cache),
        gate=gate,
    )


@dataclass(frozen=True)
class PaperLoop:
    """The demo paper loop's recording side (spec 0006): its run, the entry blocks, and the writer."""

    run_id: UUID
    blocks: EntryBlocks
    writer: RecordingWriter
    sessions: Callable[[], Session]


def run_config(settings: PaperLoopSettings) -> JsonObject:
    """``strategy_runs.run_config`` for a paper run: the sandbox balance and fees, and how fills happen."""
    return {
        "starting_balance_usd": settings.sandbox_starting_balance_usd,
        "maker_fee": settings.sandbox_maker_fee,
        "taker_fee": settings.sandbox_taker_fee,
        "oms_type": "NETTING",
        "account_type": "MARGIN",
        "leverage": settings.risk_max_leverage,
        "fill_source": "quotes",
    }


def start_paper_loop(settings: TraderSettings, secrets: Mapping[str, SecretStr], trader_id: str) -> PaperLoop:
    """The start sequence after the purge (spec 0006 AC-15): replay the spool, start the paper run, and
    close what earlier runs left open (``close_abandoned``, in the same transaction), before trading.

    Raises when Postgres is unreachable, so ``main`` exits 1 and Docker restarts the trader.
    """
    engine = create_engine(settings.database_url(secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True)
    sessions = sessionmaker(engine)
    blocks = EntryBlocks()
    env = Env(settings.trading_env)
    replayed = replay_spool(settings.spool_dir, sessions, blocks)
    with sessions.begin() as session:
        version_id = repository.ensure_strategy_version(
            session, STRATEGY_NAME, StrategyKind.SCALPING, None, settings.strategy_config()
        )
        run_id = repository.start_run(
            session,
            env=env,
            mode=RunMode.PAPER,
            version_id=version_id,
            trader_id=trader_id,
            run_config=run_config(settings),
            started_ns=time.time_ns(),
        )
        abandoned = repository.close_abandoned(session, env, trader_id, run_id, time.time_ns())
    writer = RecordingWriter(
        sessions,
        blocks,
        queue_max=settings.recording_queue_max,
        max_backlog_s=float(settings.recording_max_backlog_s),
        deadletter=deadletter_path(settings.spool_dir, run_id),
    )
    writer.start()
    if abandoned:
        log.critical(
            "positions left open by an earlier run closed as abandoned",
            extra={"fields": {"code": "positions_abandoned", "position_ids": [str(p) for p in abandoned]}},
        )
    log.info(
        "paper run started",
        extra={"fields": {"code": "paper_run_started", "run_id": str(run_id), "spool_replayed": replayed}},
    )
    return PaperLoop(run_id=run_id, blocks=blocks, writer=writer, sessions=sessions)


def finish_paper_loop(paper: PaperLoop, settings: PaperLoopSettings, positions_open: int) -> None:
    """After the node stopped: drain the queue, spill what is left, and finish the run (spec 0006 AC-13).

    The run is ``completed`` when no position is left open, else ``aborted``. A failed finish is
    logged; the next start marks a still running run ``aborted`` anyway.
    """
    if not paper.writer.drain(float(settings.recording_drain_s)):
        spilled = paper.writer.spill(spool_path(settings.spool_dir, paper.run_id))
        log.warning(
            "recording queue not drained, spilled to the spool",
            extra={"fields": {"code": "recording_spilled", "run_id": str(paper.run_id), "items": spilled}},
        )
    status = RunStatus.COMPLETED if positions_open == 0 else RunStatus.ABORTED
    try:
        with paper.sessions() as session, session.begin():
            repository.finish_run(session, paper.run_id, status, time.time_ns())
    except Exception:
        log.exception(
            "paper run could not be finished",
            extra={"fields": {"code": "paper_run_unfinished", "run_id": str(paper.run_id)}},
        )


def placeholder_config(settings: TraderSettings, run_id: UUID) -> PlaceholderConfig:
    """The strategy's config from the ``PLACEHOLDER_*``, ``RISK_*``, and ``CONTEXT_*`` settings, under ``run_id``.

    The placeholder reads the market context and logs it once per bar (spec 0009 AC-11).
    """
    return PlaceholderConfig(
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=BTC_PERP,
        interval_min=settings.placeholder_interval_min,
        hold_min=settings.placeholder_hold_min,
        lookback_min=settings.placeholder_lookback_min,
        stop_pct=settings.placeholder_stop_pct,
        notional_usd=settings.placeholder_notional_usd,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        stale_quote_s=settings.risk_stale_quote_s,
        stop_accept_timeout_s=settings.stop_accept_timeout_s,
        use_market_context=True,
        context_max_age_mark_s=settings.context_max_age_mark_s,
        context_max_age_index_s=settings.context_max_age_index_s,
        context_max_age_open_interest_s=settings.context_max_age_open_interest_s,
        context_max_age_funding_s=settings.context_max_age_funding_s,
        context_liquidation_window_s=settings.context_liquidation_window_s,
    )


def loss_limits(settings: LossLimitSettings) -> LossLimits:
    """The pure loss limits from the ``RISK_*`` settings (spec 0010 AC-21)."""
    return LossLimits(
        daily_usd=settings.risk_daily_loss_limit_usd,
        total_usd=settings.risk_total_loss_limit_usd,
        total_since=settings.risk_total_loss_since,
        max_trades_per_day=settings.risk_max_trades_per_day,
        cooldown_s=settings.risk_loss_cooldown_s,
    )


def loss_limit_actor(
    settings: TraderSettings, secrets: Mapping[str, SecretStr], kill_switch: KillSwitch, paper: PaperLoop | None
) -> LossLimitActor:
    """The loss limits (spec 0010 AC-13): seeded from the paper runs in demo, the live runs in live.

    Demo shares the paper loop's sessions, blocks, and recorder. Live has no strategy and no run
    yet, so its blocks guard nothing and its events are only logged (spec 0010 Follow-up).
    """
    env = Env(settings.trading_env)
    sessions: Callable[[], Session]
    if paper is not None:
        sessions, mode, blocks = paper.sessions, RunMode.PAPER, paper.blocks
    else:
        engine = create_engine(settings.database_url(secrets[POSTGRES_PASSWORD]), pool_size=1, pool_pre_ping=True)
        sessions, mode, blocks = sessionmaker(engine), RunMode.LIVE, EntryBlocks()

    def read_seed(day_start: datetime, total_since: datetime) -> LossSeed:
        with sessions() as session:
            return loss_seed(session, env, mode, day_start, total_since)

    actor = LossLimitActor(
        limits=loss_limits(settings),
        env=env,
        read_seed=read_seed,
        kill_switch=kill_switch,
        blocks=blocks,
        record=None if paper is None else paper.writer.put,
        run_id=None if paper is None else paper.run_id,
    )
    kill_switch.watch_losses(actor)
    return actor


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
    paper: PaperLoop | None = None,
) -> int:
    """Build and run the node until it stops; return the process exit code.

    In every env the control actor is registered first (before any strategy), and tasks on the
    node's event loop write the heartbeat and ``trader_state`` snapshots for the dashboard
    (specs 0004, 0005). SIGTERM and SIGINT stop the node gracefully; a clean stop logs
    ``shutdown_graceful``. In live the node also runs the key age actor and the startup key
    check. The check is a task on the node's event loop, not an actor: when the Kraken client
    cannot connect, Nautilus never starts the trader, so an actor would never run. A failed
    check stops the node and makes this return 1 after ``fail_backoff_s``.

    With ``paper`` (demo) it registers the recording actor and the ``placeholder_timed``
    strategy after the control actor, writes the ``position`` and ``account`` snapshots, and
    after the stop drains the recording queue and finishes the run.
    """
    node = TradingNode(config=config)
    control = control_parts(node, settings, secrets)
    node.trader.add_actor(control.actor)
    node.trader.add_actor(
        HeartbeatActor(HeartbeatConfig(path=os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")))
    )
    # Open interest and liquidations for the strategies' market context (spec 0009 AC-12). The trader
    # records none of it, and an outage only logs: it never blocks entries, pauses, or alerts.
    node.trader.add_actor(
        ContextFeedActor(
            ContextFeedConfig(
                ws_url=settings.context_ws_url,
                instrument_ids=[BTC_PERP],
                reconnect_max_s=settings.context_reconnect_max_s,
                idle_s=settings.context_idle_s,
            )
        )
    )
    # After the control actor (the halt is known) and the demo's abandoned close (start_paper_loop).
    losses = loss_limit_actor(settings, secrets, control.kill_switch, paper)
    node.trader.add_actor(losses)
    paper_state: Callable[[], PaperStatus] | None = None
    snapshot_readers: list[tuple[str, Callable[[], Mapping[str, object] | None]]] = [
        ("heartbeat", lambda: heartbeat_state(node, settings)),
        (
            "trader_state",
            lambda: trader_state(control.gate, control.risk_engine, node.cache, settings.git_commit, paper_state),
        ),
    ]
    if paper is not None:
        instrument_id = InstrumentId.from_str(BTC_PERP)
        record = paper.writer.put
        env = Env(settings.trading_env)
        node.trader.add_actor(
            RecordingActor(
                env=env, run_id=paper.run_id, instrument_id=instrument_id, record=record, blocks=paper.blocks
            )
        )
        strategy = PlaceholderTimed(placeholder_config(settings, paper.run_id), control.gate, paper.blocks, record)
        node.trader.add_strategy(strategy)
        control.kill_switch.attach(
            [s for s in node.trader.strategies() if isinstance(s, KfsStrategy)], record, paper.run_id
        )
        writer, blocks = paper.writer, paper.blocks

        def paper_state() -> PaperStatus:
            return paper_status(control.gate, blocks, writer.backlog, strategy)

        usd = Currency.from_str("USD")
        snapshot_readers += [
            ("position", lambda: position_snapshot(node.cache, instrument_id, strategy)),
            ("account", lambda: account_snapshot(node.portfolio, node.cache, instrument_id, usd)),
        ]
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
    if SANDBOX in config.exec_clients:
        node.add_exec_client_factory(SANDBOX, FeeSandboxExecClientFactory)
    if KRAKEN in config.exec_clients:
        node.add_exec_client_factory(KRAKEN, KrakenLiveExecClientFactory)
    node.build()
    loop = node.get_event_loop()
    if loop is None:  # the live node always has one; without it the dashboard would see no heartbeat
        raise RuntimeError("trading node has no event loop for the snapshot writer")
    snapshots = snapshot_client(settings, secrets[REDIS_PASSWORD])
    # Kept in locals so the tasks live as long as the node runs; a crash is logged, trading goes on.
    writers = [
        loop.create_task(write_heartbeats(snapshots, snapshot_key(settings.trading_env, name), read_state))
        for name, read_state in snapshot_readers
    ]
    for task in writers:
        task.add_done_callback(log_writer_crash)
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
        for task in writers:
            task.cancel()
        # Let the cancelled writers finish on the stopped loop, so none is destroyed while pending.
        if not loop.is_closed() and not loop.is_running():
            loop.run_until_complete(asyncio.gather(*writers, return_exceptions=True))
        if paper is not None:
            finish_paper_loop(paper, settings, len(node.cache.positions_open()))
        node.dispose()
    if failed:
        time.sleep(fail_backoff_s)
        return 1
    return 0


def cache_client(settings: Settings, password: SecretStr) -> SyncRedis:
    """A redis-py client as the trader's ACL user, for the demo cache purge before the node exists."""
    return SyncRedis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password.get_secret_value(),
        socket_timeout=CONTROL_SOCKET_TIMEOUT_S,
        socket_connect_timeout=CONTROL_SOCKET_TIMEOUT_S,
    )


class PurgeClient(Protocol):
    """The two Redis calls the demo purge needs (the sync redis-py client satisfies it)."""

    def scan_iter(self, match: str, count: int) -> Iterator[str | bytes]: ...

    def unlink(self, *names: str | bytes) -> int: ...


def purge_demo_cache(client: PurgeClient, trader_id: str) -> int:
    """Delete this trader's own Nautilus cache keys, ``trader-<trader_id>:*``; returns how many (spec 0006 AC-15).

    Demo only: sandbox orders and positions cannot be restored after a restart, so a reload
    would resurrect a simulated position the new sandbox never had. Live never purges. It uses
    SCAN and UNLINK and touches nothing outside the pattern, never a whole database.
    """
    deleted = 0
    batch: list[str | bytes] = []
    for key in client.scan_iter(match=f"trader-{trader_id}:*", count=1000):
        batch.append(key)
        if len(batch) >= PURGE_BATCH:
            deleted += client.unlink(*batch)
            batch.clear()
    if batch:
        deleted += client.unlink(*batch)
    return deleted


def warn_on_settings(settings: PaperLoopSettings) -> None:
    """Log the start warnings for settings that are allowed but unusual (spec 0006)."""
    if settings.placeholder_notional_usd > settings.risk_max_order_notional_usd:
        log.warning(
            "placeholder notional is above the order cap, every entry will be refused",
            extra={
                "fields": {
                    "code": "placeholder_notional_over_cap",
                    "notional": str(settings.placeholder_notional_usd),
                    "cap": str(settings.risk_max_order_notional_usd),
                }
            },
        )
    if settings.sandbox_reject_stop_orders:
        log.warning(
            "the sandbox rejects every stop order, for the AC-7 drill only",
            extra={"fields": {"code": "sandbox_reject_stop_orders"}},
        )
    if settings.sandbox_reject_kill_switch_orders:
        log.warning(
            "the sandbox rejects every kill switch order, for the spec 0010 drill only",
            extra={"fields": {"code": "sandbox_reject_kill_switch_orders"}},
        )


def main() -> None:
    started = boot("node", settings_cls=TraderSettings, guard=guard, secrets=trader_secrets)
    settings = started.settings
    if not redis_reachable(settings.redis_host, settings.redis_port):
        log.critical(
            "redis unreachable, refusing to start",
            extra={"fields": {"host": settings.redis_host, "port": settings.redis_port}},
        )
        sys.exit(1)
    warn_on_settings(settings)
    config = build_config(settings, started.secrets)
    if settings.trading_env == "demo":
        deleted = purge_demo_cache(cache_client(settings, started.secrets[REDIS_PASSWORD]), config.trader_id.value)
        log.info(
            "demo cache purged",
            extra={"fields": {"code": "demo_cache_purged", "trader_id": config.trader_id.value, "deleted": deleted}},
        )

    paper = None
    if settings.trading_env == "demo":
        try:
            paper = start_paper_loop(settings, started.secrets, config.trader_id.value)
        except Exception:
            log.critical(
                "paper run could not start, exiting", exc_info=True, extra={"fields": {"code": "paper_start_failed"}}
            )
            sys.exit(1)

    code = run_node(
        config,
        settings,
        settings.kraken_key_check_timeout_s,
        started.secrets,
        paper=paper,
    )
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
