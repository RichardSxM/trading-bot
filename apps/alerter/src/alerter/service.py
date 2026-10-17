"""The alerter's loops (spec 0013): heartbeat, entry blocks, logs, host lines, containers, disk,
Postgres, tick, retry, store, ping, the daily note, the liveness file, and the alert commands.

Every loop runs supervised: a crash logs ``alerter_task_crashed`` and restarts it. Pushes run as
their own tasks, so a push retrying for two minutes never stalls a check.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from redis.exceptions import RedisError

from alerter.commands import AlertCommands, MuteKey
from alerter.daily import DailyNote
from alerter.delivery import Deliverer, Outcome
from alerter.sources.containers import backup_health, container_checks
from alerter.sources.disk import free_bytes
from alerter.sources.docker_api import DockerApiError
from alerter.sources.docker_watch import DockerWatch
from alerter.sources.host_files import HostFiles
from alerter.sources.redis_state import SnapshotReader, heartbeat_ts, trader_blocks
from alerter.store import StoreWriter
from alerter.watchdog import Watchdog
from trading_core.alerts.checks import disk_check, entry_block_checks, heartbeat_check, reachable
from trading_core.alerts.engine import LIVE, AlertEngine
from trading_core.alerts.model import Decision, Delivery, Hold, HoldKind, Observation, Push, Tier
from trading_core.alerts.rules import FILL, POSTGRES_UNREACHABLE, REDIS_UNREACHABLE, text_field
from trading_core.control.alerts import AlertCommand, AlertResult
from trading_core.control.consumer import ControlRedis
from trading_core.recording.enums import AlertCmd, ControlOutcome
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
DAILY_S: Final = 60.0
# The watchdog pings only while the Redis read and the log pass succeeded this recently (AC-2).
SOURCE_FRESH_S: Final = 60.0
LIVENESS_S: Final = 10.0
# A live fill's row is looked up every 2 s for up to 10 s (AC-8): six reads.
FILL_LOOKUP_S: Final = 2.0
FILL_LOOKUPS: Final = 6
# Test pushes, loudest first, so the critical one is on the lock screen by the time you look.
ALL_TIERS: Final = (Tier.CRITICAL, Tier.WARNING, Tier.INFO)
# A test pushes three times and the critical one repeats until acknowledged, so a second test
# this soon after the last (a double tap, a retried request, a replayed entry) is refused.
TEST_COOLDOWN_S: Final = 30.0
RESTART_AFTER_S: Final = 5.0
# How long the checks wait for the stored episodes before starting from memory alone (AC-9, AC-15).
LOAD_WAIT_S: Final = 15.0
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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        control: ControlRedis | None = None,
        mute: MuteKey | None = None,
        daily_at: str = "08:00",
        daily_tz: str = "Europe/Amsterdam",
        load_wait_s: float = LOAD_WAIT_S,
    ) -> None:
        self._env = env
        self._sleep = sleep
        self._engine = engine
        self._deliverer = deliverer
        self._store = store
        self._redis = redis
        self._heartbeat_key = snapshot_key(env, "heartbeat")
        self._state_key = snapshot_key(env, "trader_state")
        self._trader_down_s = trader_down_s
        self._load_wait_s = load_wait_s
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
        self._pushes: set[asyncio.Task[object]] = set()
        self._last_test_at: datetime | None = None
        self._mute = mute
        self._commands = None if control is None else AlertCommands(control, env, self.execute, clock)
        self.daily = DailyNote(
            engine=engine,
            store=store,
            deliver=self.deliver,
            backup=self.backup_health,
            at=daily_at,
            tz=daily_tz,
            clock=clock,
        )

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
        """Feed every new container line to the engine (AC-3, AC-7); nothing when the listing fails."""
        if self._docker is None:
            return
        try:
            observations = await self._docker.lines()
        except DockerApiError as exc:
            log.warning("log pass failed", extra={"fields": {"code": "alerter_logs_failed", "error": str(exc)}})
            return
        for observation in observations:
            if self._env == LIVE and FILL.matches is not None and FILL.matches(observation):
                self._spawn(self.observe_fill(observation))
            else:
                self.apply(self._engine.observe(observation))

    async def observe_fill(self, obs: Observation) -> None:
        """A live ``fill_seen`` line with its ``fills`` row (AC-8): looked up every 2 s for up to 10 s.

        The trader logs the line before its writer stores the row. Without the row in time, the
        push names only the trade id.
        """
        trade_id = text_field(obs, "trade_id")
        row = None
        for attempt in range(FILL_LOOKUPS):
            if attempt:
                await self._sleep(FILL_LOOKUP_S)
            row = await self._store.fill_fields(trade_id)
            if row is not None:
                break
        if row is None:
            log.warning("fill row not found", extra={"fields": {"code": "alert_fill_missing", "trade_id": trade_id}})
        fields = obs.fields if row is None else {**obs.fields, **row}
        self.apply(self._engine.observe(replace(obs, fields=fields)))

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

    async def backup_health(self) -> str:
        """The backup container's Docker health for the daily note (AC-18); "unknown" without Docker."""
        if self._docker is None:
            return "unknown"
        try:
            return backup_health(await self._docker.containers())
        except DockerApiError as exc:
            log.warning(
                "backup health read failed", extra={"fields": {"code": "alerter_containers_failed", "error": str(exc)}}
            )
            return "unknown"

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

    def apply(self, decision: Decision) -> list[asyncio.Task[object]]:
        """Queue the decision's episodes for storage and start one task per push."""
        for episode in decision.store:
            self._store.put(episode)
        return [self._spawn(self.deliver(push)) for push in decision.pushes]

    def _spawn(self, work: Coroutine[object, object, object]) -> asyncio.Task[object]:
        """Run ``work`` as its own task, kept until it finishes."""
        task = asyncio.create_task(work)
        self._pushes.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._pushes.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        log.error(
            "alerter task crashed",
            exc_info=task.exception(),
            extra={"fields": {"code": "alerter_task_crashed", "task": task.get_name()}},
        )

    async def deliver(self, push: Push) -> Outcome:
        """Send one push and record its outcome on the episode.

        A push that raises instead of answering is logged and recorded ``failed``, so it leaves the
        in flight set and the retry cycle picks it up (AC-14).
        """
        try:
            outcome = await self._deliverer.send(push)
        except Exception as exc:
            log.exception(
                "push crashed",
                extra={
                    "fields": {"code": "alerter_task_crashed", "task": "push", "fingerprint": push.episode.fingerprint}
                },
            )
            outcome = Outcome(Delivery.FAILED, f"crashed: {type(exc).__name__}")
        self._note_failed = outcome.delivery is Delivery.FAILED
        for updated in self._engine.record(push, outcome.delivery, self._clock()):
            self._store.put(updated)
        return outcome

    # Alert commands (AC-12, AC-16) -----------------------------------------------------------

    async def execute(self, msg: AlertCommand) -> AlertResult:
        """Run one alert command.

        A mute holds warning and info pushes until ``issued_at`` plus its length, replacing any
        mute before it; an unmute ends it, and the warnings it held that are still open go out as
        one summary. A test sends one marked push per tier through the delivery path, all at once,
        and fails with ``push_failed`` when any of them failed, or with ``too_soon`` within
        ``TEST_COOLDOWN_S`` of the last test. A mute without its length fails
        ``invalid``.
        """
        now = self._clock()
        if msg.cmd is AlertCmd.MUTE:
            if msg.mute_s is None:
                return AlertResult(msg.id, ControlOutcome.FAILED, now, msg.cmd, "invalid")
            until = msg.issued_at + timedelta(seconds=msg.mute_s)
            if self._mute is not None:
                await self._mute.set(until, msg.id, now)
            self.apply(self._engine.open_hold(Hold(HoldKind.MUTE, f"mute:{msg.id}", now, until)))
            return AlertResult(msg.id, ControlOutcome.EXECUTED, self._clock(), msg.cmd, until=until)
        if msg.cmd is AlertCmd.UNMUTE:
            if self._mute is not None:
                await self._mute.clear()
            self.apply(self._engine.close_hold(HoldKind.MUTE, now))
            return AlertResult(msg.id, ControlOutcome.EXECUTED, self._clock(), msg.cmd)
        if self._last_test_at is not None and (now - self._last_test_at).total_seconds() < TEST_COOLDOWN_S:
            return AlertResult(msg.id, ControlOutcome.FAILED, now, msg.cmd, "too_soon")
        self._last_test_at = now
        decision = self._engine.test_episodes(str(msg.id), ALL_TIERS, now)
        for episode in decision.store:
            self._store.put(episode)
        outcomes = await asyncio.gather(*(self.deliver(push) for push in decision.pushes))
        if any(o.delivery is Delivery.FAILED for o in outcomes):
            return AlertResult(msg.id, ControlOutcome.FAILED, self._clock(), msg.cmd, "push_failed")
        return AlertResult(msg.id, ControlOutcome.EXECUTED, self._clock(), msg.cmd)

    async def restore_mute(self) -> None:
        """Hold again under a mute stored before a restart, while it lasts and no mute is open."""
        if self._mute is None:
            return
        stored = await self._mute.load()
        now = self._clock()
        if stored is None or stored[0] <= now or any(h.kind is HoldKind.MUTE for h in self._engine.holds):
            return
        until, command_id = stored
        self.apply(self._engine.open_hold(Hold(HoldKind.MUTE, f"mute:{command_id}", now, until)))
        log.info(
            "mute restored",
            extra={"fields": {"code": "alert_mute_restored", "until": until.isoformat(), "id": str(command_id)}},
        )

    async def serve_commands(self) -> None:
        """Restore a stored mute, then read the alert commands until cancelled."""
        if self._commands is None:
            return
        await self.restore_mute()
        await self._commands.run()

    # The process -----------------------------------------------------------------------------

    async def run(self) -> None:
        """Run every loop until cancelled.

        The checks wait up to ``load_wait_s`` for the stored episodes, so a condition still open
        across a restart joins its stored episode instead of pushing again (AC-9); with Postgres
        still away after that, they start from memory and alerts keep flowing (AC-15). The host
        files are read next, so a deploy still under way holds from the first pass of the other
        loops (AC-11).
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(_supervise("store", self._store.run))
            if not await self._store.wait_loaded(self._load_wait_s):
                log.warning(
                    "alerts not loaded, checking from memory",
                    extra={"fields": {"code": "alerts_load_waited", "waited_s": self._load_wait_s}},
                )
            await self.read_host()
            if self._commands is not None:
                group.create_task(_supervise("commands", self.serve_commands))
            for name, step, every in self._loops():
                group.create_task(_supervise(name, _every(step, every)))

    def _loops(self) -> list[tuple[str, Callable[[], Awaitable[None]], float]]:
        return [
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
            ("daily", self.daily.step, DAILY_S),
            ("liveness", _sync(self.touch_liveness), LIVENESS_S),
        ]


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
