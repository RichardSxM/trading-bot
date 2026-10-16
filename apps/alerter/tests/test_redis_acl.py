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
