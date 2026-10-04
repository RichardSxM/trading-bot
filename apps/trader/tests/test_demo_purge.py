"""covers: spec 0006 AC-1 and AC-15, the demo cache purge against the Compose Redis as the `trader` user.

Needs the Compose Redis and the trader's mounted password:
``docker compose run --rm -T trader pytest -q -m redis``.
"""

import os
import uuid

import pytest
from redis import Redis

from trader.main import purge_demo_cache
from trading_core.secrets import REDIS_PASSWORD, read_secret

pytestmark = pytest.mark.redis


@pytest.fixture
def redis() -> Redis:
    return Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        username="trader",
        password=read_secret(REDIS_PASSWORD).get_secret_value(),
        decode_responses=True,
    )


def test_purge_deletes_the_traders_keys_and_leaves_the_rest(redis):
    # A throwaway trader id, so the live demo trader's own cache is never touched.
    trader_id = f"TRADER-TEST{uuid.uuid4().hex[:8].upper()}"
    own = [f"trader-{trader_id}:orders:O-{i}" for i in range(3)]
    neighbour = f"trader-{trader_id}X:orders:O-1"
    for key in [*own, neighbour]:
        redis.set(key, "1", ex=60)
    kept = {key: redis.exists(key) for key in ("demo:trader:pause", "demo:control", "demo:snapshot:heartbeat")}

    try:
        assert purge_demo_cache(redis, trader_id) == 3
        assert not any(redis.exists(key) for key in own)
        assert redis.exists(neighbour) == 1
        assert {key: redis.exists(key) for key in kept} == kept
    finally:
        redis.unlink(*own, neighbour)
