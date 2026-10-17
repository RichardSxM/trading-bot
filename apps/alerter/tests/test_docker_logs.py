"""covers: spec 0013 Build plan 2.1 and *Replay safety*: Docker's framed log stream, timestamps, line
parsing, the Redis cursors (5 min back without one, 7 day TTL), the replay set, the project filter,
the container checks behind AC-4, and a partly failed pass that keeps what it read (AC-3).

The Docker API is an ``httpx.MockTransport`` answering like the socket proxy.
"""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from alerter.sources.containers import container_checks
from alerter.sources.docker_api import (
    Container,
    DockerApi,
    DockerApiError,
    ns_to_rfc3339,
    ns_to_since,
    rfc3339_ns,
)
from alerter.sources.docker_logs import CURSOR_TTL_S, LogReader, demux, observe, split_line
from alerter.sources.docker_watch import DockerWatch

from .conftest import NOW, Clock, FakeRedis

NOW_NS = int(NOW.timestamp()) * 1_000_000_000


def frame(text: str, stream: int = 1) -> bytes:
    payload = text.encode()
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def stamp(seconds_after: float) -> str:
    return ns_to_rfc3339(NOW_NS + int(seconds_after * 1_000_000_000))


# Pure helpers -------------------------------------------------------------------------------


def test_demux_joins_frames_from_both_streams_and_splits_lines():
    raw = frame("a 1\nb 2\n") + frame("c 3\n", stream=2) + frame("d 4\n")

    assert demux(raw) == ["a 1", "b 2", "c 3", "d 4"]


def test_demux_reads_a_plain_stream_as_text():
    assert demux(b"one\ntwo\n") == ["one", "two"]


def test_docker_times_compare_as_numbers_not_text():
    # Docker trims trailing zeros: .5Z is later than .123456789Z although it sorts first as text.
    assert rfc3339_ns("2026-10-16T03:00:00.5Z") > rfc3339_ns("2026-10-16T03:00:00.123456789Z")
    assert rfc3339_ns("2026-10-16T03:00:00Z") == NOW_NS
    assert ns_to_rfc3339(NOW_NS + 5) == "2026-10-16T03:00:00.000000005Z"
    assert ns_to_since(NOW_NS + 5) == f"{NOW_NS // 1_000_000_000}.000000005"


@pytest.mark.parametrize("bad", ["2026-10-16T03:00:00+02:00", "nonsense", "2026-10-16T03:00:00.x5Z"])
def test_a_bad_time_is_refused(bad):
    with pytest.raises(ValueError, match=r"time|fraction"):
        rfc3339_ns(bad)


def test_a_json_line_becomes_an_observation_with_its_code_and_fields():
    line = split_line(f"{stamp(1)} " + json.dumps({"level": "CRITICAL", "code": "kraken_auth_failed", "msg": "x"}))

    assert line is not None
    obs = observe("trader", line)
    assert (obs.source, obs.service, obs.code, obs.level, obs.text) == (
        "log:trader",
        "trader",
        "kraken_auth_failed",
        "CRITICAL",
        None,
    )
    assert obs.at == NOW + timedelta(seconds=1)
    assert obs.fields["msg"] == "x"


def test_a_plain_line_keeps_only_its_text():
    line = split_line(f"{stamp(0)} 2026-10-16T03:00:00Z [ERROR] TRADER-001.ExecEngine: boom")

    assert line is not None
    obs = observe("trader", line)
    assert (obs.code, obs.level) == (None, None)
    assert obs.text is not None
    assert "[ERROR]" in obs.text


def test_a_line_without_a_docker_time_is_skipped():
    assert split_line("no timestamp here") is None


# The reader ------------------------------------------------------------------------------------


class FakeDocker:
    """Answers the socket proxy's GET calls from fixed containers and log lines."""

    def __init__(self) -> None:
        self.containers = [
            {"Id": "t1", "State": "running", "Status": "Up 1 hour", "Labels": _labels("trader")},
            {"Id": "a1", "State": "running", "Status": "Up 1 hour", "Labels": _labels("alerter")},
            {"Id": "m1", "State": "exited", "Status": "Exited (0)", "Labels": _labels("migrate")},
            {"Id": "r1", "State": "running", "Status": "Up", "Labels": _labels("trader", oneoff=True)},
        ]
        self.lines: dict[str, list[str]] = {"t1": []}
        self.started = {"t1": "2026-10-16T02:00:00.5Z", "a1": "2026-10-16T02:00:00Z"}
        self.requests: list[httpx.Request] = []
        self.fail = False
        # Containers removed between the listing and their logs read (a deploy recreates them).
        self.gone: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500)
        path = request.url.path
        if path == "/containers/json":
            return httpx.Response(200, json=self.containers)
        container = path.split("/")[2]
        if container in self.gone:
            return httpx.Response(404)
        if path.endswith("/logs"):
            since = float(request.url.params["since"])
            kept = [line for line in self.lines.get(container, []) if rfc3339_ns(line.split(" ")[0]) / 1e9 >= since - 1]
            return httpx.Response(200, content=b"".join(frame(line + "\n") for line in kept))
        labels = _labels("alerter") if container == "selfhost" else {}
        return httpx.Response(
            200,
            json={
                "Config": {"Labels": labels},
                "State": {"StartedAt": self.started.get(container, "0001-01-01T00:00:00Z")},
            },
        )


def _labels(service: str, *, oneoff: bool = False) -> dict[str, str]:
    return {
        "com.docker.compose.project": "kfs",
        "com.docker.compose.service": service,
        "com.docker.compose.oneoff": "True" if oneoff else "False",
    }


def make(docker: FakeDocker, redis: FakeRedis | None = None, clock: Clock | None = None):
    clock = clock or Clock()
    redis = redis or FakeRedis()
    http = httpx.AsyncClient(transport=httpx.MockTransport(docker))
    api = DockerApi(http, "http://docker-proxy:2375")
    reader = LogReader(api, redis, "demo", clock)
    return DockerWatch(api, reader, "selfhost"), reader, redis, clock


def code_line(seconds_after: float, code: str, **fields) -> str:
    return f"{stamp(seconds_after)} " + json.dumps({"level": "ERROR", "code": code, **fields})


def test_only_the_projects_long_running_services_are_read():
    async def run():
        docker = FakeDocker()
        watch, _, _, _ = make(docker)
        containers = await watch.containers()
        await watch.lines()
        return containers, docker

    containers, docker = asyncio.run(run())

    assert [c.service for c in containers] == ["trader", "alerter", "migrate"]  # no one shot run
    filters = json.loads(next(r for r in docker.requests if r.url.path == "/containers/json").url.params["filters"])
    assert filters == {"label": ["com.docker.compose.project=kfs"]}
    log_reads = {r.url.path for r in docker.requests if r.url.path.endswith("/logs")}
    assert log_reads == {"/containers/t1/logs"}


def test_without_a_cursor_the_read_starts_five_minutes_back_and_stores_one():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(-600, "too_old"), code_line(-100, "recent")]
        watch, reader, redis, _ = make(docker)
        return await watch.lines(), docker, redis, reader

    found, docker, redis, reader = asyncio.run(run())

    since = float(next(r for r in docker.requests if r.url.path.endswith("/logs")).url.params["since"])
    assert since == pytest.approx((NOW_NS / 1e9) - 300)
    assert [o.code for o in found] == ["recent"]
    assert redis.values["demo:alerts:cursor:trader"] == stamp(-100)
    assert redis.ttls["demo:alerts:cursor:trader"] == CURSOR_TTL_S
    assert reader.ok_at == NOW


def test_a_second_pass_never_returns_a_line_twice():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(1, "first"), code_line(1, "same_time_other_line")]
        watch, _, _, clock = make(docker)
        first = await watch.lines()
        docker.lines["t1"].append(code_line(2, "second"))
        clock.advance(5)
        second = await watch.lines()
        return first, second

    first, second = asyncio.run(run())

    assert [o.code for o in first] == ["first", "same_time_other_line"]
    assert [o.code for o in second] == ["second"]


def test_a_restarted_alerter_resumes_from_the_stored_cursor():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(1, "seen_before_restart"), code_line(2, "new")]
        redis = FakeRedis()
        redis.values["demo:alerts:cursor:trader"] = stamp(1)
        watch, _, _, _ = make(docker, redis)
        return await watch.lines()

    found = asyncio.run(run())

    # Docker's ``since`` is inclusive, so the line at the cursor comes back; it is dropped.
    assert [o.code for o in found] == ["new"]


def test_a_lost_cursor_with_a_fresh_replay_set_drops_the_overlap():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(-10, "once")]
        redis = FakeRedis()
        watch, _, _, clock = make(docker, redis)
        first = await watch.lines()
        del redis.values["demo:alerts:cursor:trader"]
        clock.advance(5)
        second = await watch.lines()
        return first, second

    first, second = asyncio.run(run())

    assert [o.code for o in first] == ["once"]
    assert second == []


def test_a_failed_listing_raises_and_is_not_fresh():
    async def run():
        docker = FakeDocker()
        watch, reader, _, _ = make(docker)
        docker.fail = True
        with pytest.raises(DockerApiError):
            await watch.lines()
        return reader

    assert asyncio.run(run()).ok_at is None


def test_redis_down_at_the_first_cursor_read_reads_nothing_and_is_not_fresh():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(1, "stop_not_accepted")]
        redis = FakeRedis()
        redis.fail = True
        watch, reader, _, clock = make(docker, redis)
        first = await watch.lines()
        redis.fail = False
        clock.advance(5)
        return first, await watch.lines(), reader

    first, second, reader = asyncio.run(run())

    assert first == []
    assert [o.code for o in second] == ["stop_not_accepted"]  # read on the next pass, not lost
    assert reader.ok_at == NOW + timedelta(seconds=5)


class CursorWriteFails(FakeRedis):
    """Reads work; cursor writes raise while ``fail_set`` is on."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_set = True

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        if self.fail_set:
            from redis.exceptions import ConnectionError

            raise ConnectionError("redis is down")
        return await super().set(name, value, ex)


def test_a_failed_cursor_write_keeps_the_lines_and_writes_the_cursor_on_the_next_pass():
    async def run():
        docker = FakeDocker()
        docker.lines["t1"] = [code_line(1, "stop_not_accepted")]
        redis = CursorWriteFails()
        watch, reader, _, clock = make(docker, redis)
        first = await watch.lines()
        fresh_after_first = reader.ok_at
        redis.fail_set = False
        clock.advance(5)
        second = await watch.lines()
        return first, fresh_after_first, second, redis, reader

    first, fresh_after_first, second, redis, reader = asyncio.run(run())

    assert [o.code for o in first] == ["stop_not_accepted"]
    assert fresh_after_first is None  # the pass failed, so the watchdog is not fed
    assert second == []  # never returned twice
    assert redis.values["demo:alerts:cursor:trader"] == stamp(1)
    assert reader.ok_at == NOW + timedelta(seconds=5)


def test_one_container_gone_mid_pass_keeps_the_other_containers_lines():
    async def run():
        docker = FakeDocker()
        docker.containers.append({"Id": "c1", "State": "running", "Status": "Up 1 hour", "Labels": _labels("recorder")})
        docker.lines["t1"] = [code_line(1, "stop_not_accepted")]
        docker.gone.add("c1")
        watch, reader, redis, clock = make(docker)
        first = await watch.lines()
        clock.advance(5)
        second = await watch.lines()
        return first, second, redis, reader

    first, second, redis, reader = asyncio.run(run())

    assert [o.code for o in first] == ["stop_not_accepted"]
    assert second == []
    assert redis.values["demo:alerts:cursor:trader"] == stamp(1)
    assert reader.ok_at is None  # the recorder could not be read


def test_states_report_the_start_time_of_running_services():
    async def run():
        watch, _, _, _ = make(FakeDocker())
        return await watch.states()

    containers, starts = asyncio.run(run())

    assert len(containers) == 3
    assert starts == {"trader": NOW - timedelta(hours=1) + timedelta(microseconds=500_000)}


# Container checks (AC-4) -----------------------------------------------------------------------


def c(service: str, state: str = "running", status: str = "Up 1 hour") -> Container:
    return Container(id=service, service=service, state=state, status=status)


def by(results):
    return {(r.rule, r.subject): r for r in results}


def test_a_core_service_without_a_running_container_fails_service_down():
    results = by(container_checks([c("trader"), c("recorder", "exited"), c("postgres")], NOW))

    assert results[("service_down", "trader")].ok
    assert not results[("service_down", "recorder")].ok
    assert results[("service_down", "recorder")].fields == {"state": "exited"}
    assert not results[("service_down", "redis")].ok  # no container at all


def test_clock_is_expected_only_when_its_container_exists():
    vps = by(container_checks([c("trader")], NOW))
    laptop = by(container_checks([c("trader"), c("clock", "exited")], NOW))

    assert vps[("service_down", "clock")].ok
    assert not laptop[("service_down", "clock")].ok


def test_unhealthy_containers_fail_and_removed_ones_pass():
    results = by(
        container_checks([c("backup", status="Up 2 hours (unhealthy)"), c("migrate", "exited")], NOW, ["dashboard"])
    )

    assert not results[("container_unhealthy", "backup")].ok
    assert results[("container_unhealthy", "dashboard")].ok  # gone from the listing
    assert ("container_unhealthy", "migrate") not in results
