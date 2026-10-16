"""covers: spec 0013 Security model, the `alerter` Redis user reads snapshots, reads and writes its own alert
keys and the alert command stream, and nothing else (never ``{env}:control``).

Needs the Compose Redis and the alerter's mounted password:
``docker compose run --rm -T alerter pytest -q -m redis``.
"""

import os
from collections.abc import Iterator

import pytest
from redis import Redis
from redis.exceptions import NoPermissionError

from alerter.sources.docker_logs import CURSOR_TTL_S
from trading_core.secrets import REDIS_PASSWORD, read_secret

pytestmark = pytest.mark.redis


@pytest.fixture
def client() -> Iterator[Redis]:
    redis = Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        username="alerter",
        password=read_secret(REDIS_PASSWORD).get_secret_value(),
        decode_responses=True,
    )
    yield redis
    redis.close()


def test_the_alerter_reads_the_heartbeat(client):
    client.get("demo:snapshot:heartbeat")


def test_the_alerter_writes_its_own_keys_and_the_alert_stream(client):
    client.set("demo:alerts:acl-probe", "1", ex=5)
    client.delete("demo:alerts:acl-probe")
    client.xadd("demo:control:alerts:results", {"probe": "1"}, maxlen=1000, approximate=True)
    client.xlen("demo:control:alerts")


def test_the_alerter_reads_the_alert_commands_in_its_group(client):
    stream = "acltest:control:alerts"
    client.delete(stream)
    client.xgroup_create(stream, "alerter", id="0", mkstream=True)
    client.xadd(stream, {"probe": "1"}, maxlen=1000, approximate=True)
    reply = client.xreadgroup("alerter", "alerter", {stream: ">"}, count=10)
    for _, entries in reply or []:
        for entry_id, _ in entries:
            client.xack(stream, "alerter", entry_id)
    client.delete(stream)


def test_the_alerter_keeps_its_log_cursors(client):
    key = "demo:alerts:cursor:acl-probe"
    client.set(key, "2026-10-16T03:00:00.000000000Z", ex=CURSOR_TTL_S)

    assert client.get(key) == "2026-10-16T03:00:00.000000000Z"
    assert 0 < client.ttl(key) <= CURSOR_TTL_S
    client.delete(key)


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.set("demo:snapshot:heartbeat", "x"),
        lambda r: r.xadd("demo:control", {"cmd": "pause"}),
        lambda r: r.xrange("demo:control"),
        lambda r: r.get("demo:halt"),
        lambda r: r.flushdb(),
        lambda r: r.config_get("*"),
    ],
)
def test_the_alerter_is_refused_everything_else(client, call):
    with pytest.raises(NoPermissionError):
        call(client)
