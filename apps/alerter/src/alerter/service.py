"""The alerter's loops (spec 0013): heartbeat, entry blocks, logs, host lines, containers, disk,
Postgres, tick, retry, store, ping, and the liveness file.

Every loop runs supervised: a crash logs ``alerter_task_crashed`` and restarts it. Pushes run as
their own tasks, so a push retrying for two minutes never stalls a check.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from redis.exceptions import RedisError

from alerter.delivery import Deliverer
from alerter.sources.containers import container_checks
from alerter.sources.disk import free_bytes
from alerter.sources.docker_api import DockerApiError
from alerter.sources.docker_watch import DockerWatch
from alerter.sources.host_files import HostFiles
from alerter.sources.redis_state import SnapshotReader, heartbeat_ts, trader_blocks
from alerter.store import StoreWriter
from alerter.watchdog import Watchdog
from trading_core.alerts.checks import disk_check, entry_block_checks, heartbeat_check, reachable
from trading_core.alerts.engine import AlertEngine
from trading_core.alerts.model import Decision, Delivery, Push
from trading_core.alerts.rules import POSTGRES_UNREACHABLE, REDIS_UNREACHABLE
from trading_core.settings import TradingEnv
from trading_core.snapshot.envelope import snapshot_key

log = logging.getLogger(__name__)

CHECK_S: Final = 5.0
BLOCKS_S: Final = 15.0
LOGS_S: Final = 5.0
HOST_S: Final = 5.0
CONTAINERS_S: Final = 15.0
DISK_S: Final = 60.0
POSTGRES_S: Final = 15.0
TICK_S: Final = 5.0
RETRY_CYCLE_S: Final = 60.0
PING_S: Final = 60.0
# The watchdog pings only while the Redis read and the log pass succeeded this recently (AC-2).
SOURCE_FRESH_S: Final = 60.0
LIVENESS_S: Final = 10.0
RESTART_AFTER_S: Final = 5.0
DEFAULT_LIVENESS_FILE: Final = Path("/run/heartbeat/alive")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The current UTC time."""
    return datetime.now(UTC)


class Alerter:
    """One alerter process: its engine, its sources, its delivery, and its store."""

    def __init__(
        self,
        *,
        env: TradingEnv,
        engine: AlertEngine,
        deliverer: Deliverer,
        store: StoreWriter,
        redis: SnapshotReader,
        trader_down_s: int,
        watchdog: Watchdog | None = None,
        docker: DockerWatch | None = None,
        host: HostFiles | None = None,
        disk_path: Path | None = None,
        disk_free_gb: Decimal = Decimal(5),
        clock: Clock = utc_now,
        liveness_file: Path = DEFAULT_LIVENESS_FILE,
    ) -> None:
        self._engine = engine
        self._deliverer = deliverer
        self._store = store
        self._redis = redis
        self._heartbeat_key = snapshot_key(env, "heartbeat")
        self._state_key = snapshot_key(env, "trader_state")
        self._trader_down_s = trader_down_s
        self._watchdog = watchdog
        self._docker = docker
        self._host = host
        self._disk_path = disk_path
        self._disk_free_gb = disk_free_gb
        self._known_services: set[str] = set()
        self._clock = clock
        self._liveness_file = liveness_file
        self._redis_ok_at: datetime | None = None
        self._started_at = clock()
        self._note_failed = False
        self._pushes: set[asyncio.Task[None]] = set()

    # One step of each loop (the tests drive these) --------------------------------------------

    async def check_heartbeat(self) -> None:
        """Read the heartbeat and feed ``trader_down``; nothing while Redis is unreachable (AC-1)."""
        try:
            ts_ns = await heartbeat_ts(self._redis, self._heartbeat_key)
        except RedisError as exc:
            log.warning("redis read failed", extra={"fields": {"code": "alerter_redis_failed", "error": str(exc)}})
            self.apply(self._engine.check(reachable(REDIS_UNREACHABLE.id, False, self._clock())))
            return
        now = self._clock()
        self._redis_ok_at = now
        self.apply(self._engine.check(reachable(REDIS_UNREACHABLE.id, True, now)))
        # A missing key is "older than the threshold" only once the alerter has watched that long
        # itself, so a cold start (the trader not up yet) raises nothing; a trader already dead
        # is still caught within the threshold of the alerter's start.
        if ts_ns is None and (now - self._started_at).total_seconds() < self._trader_down_s:
            return
        self.apply(self._engine.check(heartbeat_check(ts_ns, now, self._trader_down_s)))

    async def check_entry_blocks(self) -> None:
        """``entry_block`` from ``trader_state`` (AC-6).

        Skipped while the snapshot is missing or older than the trader down threshold: a trader
        that is down or restarting has not cleared its reasons, and ``trader_down`` covers it. A
        Redis error is the heartbeat loop's to report.
        """
        try:
            blocks = await trader_blocks(self._redis, self._state_key)
        except RedisError:
            return
        now = self._clock()
        now_ns = int(now.timestamp()) * 1_000_000_000 + now.microsecond * 1_000
        if blocks is None or (now_ns - blocks.ts_ns) // 1_000_000_000 > self._trader_down_s:
            return
        for result in entry_block_checks(blocks.blocked, blocks.since_ns, now):
            self.apply(self._engine.check(result))

    async def read_host(self) -> None:
        """Feed every new ``host.jsonl`` and ``deploys.jsonl`` line to the engine (AC-4, AC-11)."""
        if self._host is None:
            return
        try:
            observations = await asyncio.to_thread(self._host.poll)
        except OSError as exc:
            log.warning("host read failed", extra={"fields": {"code": "alerter_host_failed", "error": str(exc)}})
            return
        for observation in observations:
            self.apply(self._engine.observe(observation))

    async def check_disk(self) -> None:
        """``disk_low`` from the host root filesystem's free space (AC-4)."""
        if self._disk_path is None:
            return
        try:
            free = await asyncio.to_thread(free_bytes, self._disk_path)
        except OSError as exc:
            log.warning("disk check failed", extra={"fields": {"code": "alerter_disk_failed", "error": str(exc)}})
            return
        self.apply(self._engine.check(disk_check(free, self._disk_free_gb, self._clock())))

    async def read_logs(self) -> None:
        """Feed every new container line to the engine (AC-3, AC-7); nothing on a failed pass."""
        if self._docker is None:
            return
        try:
            observations = await self._docker.lines()
        except (DockerApiError, RedisError) as exc:
            log.warning("log pass failed", extra={"fields": {"code": "alerter_logs_failed", "error": str(exc)}})
            return
        for observation in observations:
            self.apply(self._engine.observe(observation))

    async def check_containers(self) -> None:
        """``service_down`` and ``container_unhealthy`` from the listing; restarts resolve pair episodes (AC-4)."""
        if self._docker is None:
            return
        try:
            containers, starts = await self._docker.states()
        except DockerApiError as exc:
            log.warning(
                "container check failed", extra={"fields": {"code": "alerter_containers_failed", "error": str(exc)}}
            )
            return
        now = self._clock()
        for result in container_checks(containers, now, self._known_services):
            self.apply(self._engine.check(result))
        self._known_services |= {c.service for c in containers}
        for service, started_at in starts.items():
            self.apply(self._engine.service_started(service, started_at))

    async def check_postgres(self) -> None:
        """``postgres_unreachable`` from a ``SELECT 1`` (AC-4); alerts keep flowing either way (AC-15)."""
        ok = await self._store.ping()
        self.apply(self._engine.check(reachable(POSTGRES_UNREACHABLE.id, ok, self._clock())))

    def tick(self) -> None:
        """Re-notify, ``min_open_s`` releases, quiet closes."""
        self.apply(self._engine.tick(self._clock()))

    def retry(self) -> None:
        """The retry cycle for failed pushes (AC-14)."""
        self.apply(self._engine.retry(self._clock()))

    async def ping(self) -> None:
        """Ping healthchecks.io while the sources are fresh; ``/fail`` while pushes fail (AC-2, AC-14)."""
        if self._watchdog is None:
            return
        now = self._clock()
        fresh = [self._redis_ok_at]
        if self._docker is not None:
            fresh.append(self._docker.ok_at)
        if self._host is not None:
            fresh.append(self._host.ok_at)
        if any(at is None or (now - at).total_seconds() > SOURCE_FRESH_S for at in fresh):
            log.warning("sources stale, not pinging", extra={"fields": {"code": "watchdog_skipped"}})
            return
        await self._watchdog.ping(failing=self.push_failing)

    @property
    def push_failing(self) -> bool:
        """True while any push has failed and not yet gone through."""
        return self._engine.push_failing or self._note_failed

    def touch_liveness(self) -> None:
        """Touch the file the Compose healthcheck reads."""
        self._liveness_file.parent.mkdir(parents=True, exist_ok=True)
        self._liveness_file.touch()

    # Decisions -------------------------------------------------------------------------------

    def apply(self, decision: Decision) -> list[asyncio.Task[None]]:
        """Queue the decision's episodes for storage and start one task per push."""
        for episode in decision.store:
            self._store.put(episode)
        tasks = []
        for push in decision.pushes:
            task = asyncio.create_task(self.deliver(push))
            self._pushes.add(task)
            task.add_done_callback(self._pushes.discard)
            tasks.append(task)
        return tasks

    async def deliver(self, push: Push) -> None:
        """Send one push and record its outcome on the episode."""
        outcome = await self._deliverer.send(push)
        self._note_failed = outcome.delivery is Delivery.FAILED
        for updated in self._engine.record(push, outcome.delivery, self._clock()):
            self._store.put(updated)

    # The process -----------------------------------------------------------------------------

    async def run(self) -> None:
        """Run every loop until cancelled."""
        loops: list[tuple[str, Callable[[], Awaitable[None]], float]] = [
            ("heartbeat", self.check_heartbeat, CHECK_S),
            ("entry_blocks", self.check_entry_blocks, BLOCKS_S),
            ("logs", self.read_logs, LOGS_S),
            ("host", self.read_host, HOST_S),
            ("containers", self.check_containers, CONTAINERS_S),
            ("disk", self.check_disk, DISK_S),
            ("postgres", self.check_postgres, POSTGRES_S),
            ("tick", _sync(self.tick), TICK_S),
            ("retry", _sync(self.retry), RETRY_CYCLE_S),
            ("ping", self.ping, PING_S),
            ("liveness", _sync(self.touch_liveness), LIVENESS_S),
        ]
        async with asyncio.TaskGroup() as group:
            group.create_task(_supervise("store", self._store.run))
            for name, step, every in loops:
                group.create_task(_supervise(name, _every(step, every)))


def _sync(step: Callable[[], None]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        step()

    return run


def _every(step: Callable[[], Awaitable[None]], seconds: float) -> Callable[[], Awaitable[None]]:
    async def loop() -> None:
        while True:
            await step()
            await asyncio.sleep(seconds)

    return loop


async def _supervise(name: str, run: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "alerter task crashed, restarting", extra={"fields": {"code": "alerter_task_crashed", "task": name}}
            )
            await asyncio.sleep(RESTART_AFTER_S)
