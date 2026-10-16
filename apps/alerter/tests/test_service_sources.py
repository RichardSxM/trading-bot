"""covers: spec 0013 AC-2 (no ping while the log pass is stale), AC-3 (a critical trader line pushes),
AC-4 (service down, Postgres and Redis unreachable), AC-9 (a repeated line pushes once), and AC-15
(alerts keep flowing with Postgres down)."""

import asyncio
import json

from alerter.service import Alerter
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Tier

from .conftest import Clock, FakeDeliverer, FakeRedis, FakeStore
from .test_docker_logs import FakeDocker, code_line, stamp
from .test_docker_logs import make as make_watch


class FakeWatchdog:
    def __init__(self) -> None:
        self.pings: list[bool] = []

    async def ping(self, *, failing: bool) -> bool:
        self.pings.append(failing)
        return True


def make(tmp_path, docker: FakeDocker, *, watchdog=None):
    clock = Clock()
    engine = AlertEngine(EngineConfig(env="demo"))
    store = FakeStore()
    redis = FakeRedis()
    watch, _, _, _ = make_watch(docker, redis, clock)
    deliverer = FakeDeliverer()
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=StoreWriter(store, engine, clock, retry_s=0),
        redis=redis,
        trader_down_s=45,
        watchdog=watchdog,
        docker=watch,
        clock=clock,
        liveness_file=tmp_path / "alive",
    )
    return alerter, clock, deliverer, store, redis


async def settle(alerter):
    while alerter._pushes:
        await asyncio.gather(*list(alerter._pushes))


def critical(seconds_after: float) -> str:
    body = {"level": "CRITICAL", "code": "order_rejected", "event_key": "stop_not_accepted:S1", "msg": "stop refused"}
    return f"{stamp(seconds_after)} " + json.dumps(body)


def test_a_critical_trader_line_pushes_once_however_often_it_repeats(tmp_path):
    async def run():
        docker = FakeDocker()
        alerter, clock, deliverer, _, _ = make(tmp_path, docker)
        docker.lines["t1"] = [critical(-2)]
        await alerter.read_logs()
        await settle(alerter)
        docker.lines["t1"].append(critical(1))
        clock.advance(5)
        await alerter.read_logs()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    (push,) = deliverer.sent
    assert (push.tier, push.episode.fingerprint) == (Tier.CRITICAL, "stop_not_accepted:S1")


def test_a_stopped_core_service_pushes_after_two_minutes(tmp_path):
    async def run():
        docker = FakeDocker()
        docker.containers.append(
            {"Id": "rec", "State": "exited", "Status": "Exited (1)", "Labels": _labels("recorder")}
        )
        alerter, clock, deliverer, _, _ = make(tmp_path, docker)
        await alerter.check_containers()
        clock.advance(120)
        alerter.tick()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    fingerprints = {p.episode.fingerprint for p in deliverer.sent}
    # postgres and redis have no container in the fake either.
    assert {"service_down:recorder", "service_down:postgres", "service_down:redis"} <= fingerprints
    assert "service_down:clock" not in fingerprints


def test_a_restart_resolves_a_pair_episode(tmp_path):
    async def run():
        docker = FakeDocker()
        docker.started["t1"] = stamp(10)
        alerter, clock, _, _, _ = make(tmp_path, docker)
        docker.lines["t1"] = [code_line(-1, "catalog_disk_full")]
        await alerter.read_logs()
        clock.advance(15)
        await alerter.check_containers()
        return alerter

    alerter = asyncio.run(run())

    assert not [e for e in alerter._engine.open_episodes if e.rule == "catalog_disk_full"]


def test_postgres_down_raises_its_alert_and_alerts_still_push(tmp_path):
    async def run():
        docker = FakeDocker()
        alerter, clock, deliverer, store, _ = make(tmp_path, docker)
        store.down = True
        await alerter.check_postgres()
        docker.lines["t1"] = [critical(-1)]
        await alerter.read_logs()
        clock.advance(60)
        await alerter.check_postgres()
        alerter.tick()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    assert {p.episode.fingerprint for p in deliverer.sent} == {"stop_not_accepted:S1", "postgres_unreachable"}
    assert all(p.tier is Tier.CRITICAL for p in deliverer.sent)


def test_redis_down_raises_redis_unreachable_not_trader_down(tmp_path):
    async def run():
        alerter, clock, deliverer, _, redis = make(tmp_path, FakeDocker())
        redis.fail = True
        clock.advance(50)
        await alerter.check_heartbeat()
        clock.advance(60)
        await alerter.check_heartbeat()
        alerter.tick()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    assert [p.episode.fingerprint for p in deliverer.sent] == ["redis_unreachable"]
    assert deliverer.sent[0].episode.delivery is Delivery.PENDING


def test_no_ping_until_the_log_pass_has_succeeded(tmp_path):
    async def run():
        docker = FakeDocker()
        watchdog = FakeWatchdog()
        alerter, _, _, _, redis = make(tmp_path, docker, watchdog=watchdog)
        redis.values["demo:snapshot:heartbeat"] = "x"
        await alerter.check_heartbeat()
        await alerter.ping()
        before = list(watchdog.pings)
        await alerter.read_logs()
        await alerter.ping()
        return before, watchdog.pings

    before, after = asyncio.run(run())

    assert before == []
    assert after == [False]


def _labels(service: str) -> dict[str, str]:
    return {
        "com.docker.compose.project": "kfs",
        "com.docker.compose.service": service,
        "com.docker.compose.oneoff": "False",
    }
