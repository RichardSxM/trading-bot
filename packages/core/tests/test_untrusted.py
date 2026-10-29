"""covers: spec 0023 AC-8 (the untrusted cache set: memory first, then the Redis set
``{env}:reconcile:cache_untrusted``, loaded at start, cleared by a clean start check; a Redis failure only ever
leaves an instrument untrusted)."""

import asyncio

from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.node.untrusted import UntrustedCache

BTC = "PF_XBTUSD.KRAKEN"
KEY = "live:reconcile:cache_untrusted"


class FakeRedis:
    def __init__(self, down=False):
        self.sets, self.down = {}, down

    async def sadd(self, name, *values):
        if self.down:
            raise RedisConnectionError("redis down")
        self.sets.setdefault(name, set()).update(values)

    async def delete(self, *names):
        if self.down:
            raise RedisConnectionError("redis down")
        for name in names:
            self.sets.pop(name, None)


def test_add_writes_memory_and_the_redis_set():
    redis = FakeRedis()
    untrusted = UntrustedCache(redis, "live")

    assert asyncio.run(untrusted.add(BTC))

    assert untrusted.instruments == {BTC}
    assert redis.sets == {KEY: {BTC}}


def test_a_redis_failure_still_marks_it_untrusted_in_memory():
    untrusted = UntrustedCache(FakeRedis(down=True), "live")

    assert not asyncio.run(untrusted.add(BTC))

    assert untrusted.instruments == {BTC}


def test_load_takes_the_set_found_at_start():
    untrusted = UntrustedCache(FakeRedis(), "live")

    untrusted.load([BTC.encode()])

    assert untrusted.instruments == {BTC}


def test_clear_deletes_the_set_then_trusts_the_cache():
    redis = FakeRedis()
    untrusted = UntrustedCache(redis, "live")
    asyncio.run(untrusted.add(BTC))

    assert asyncio.run(untrusted.clear())

    assert untrusted.instruments == frozenset()
    assert redis.sets == {}


def test_a_clear_redis_refused_keeps_it_untrusted():
    redis = FakeRedis()
    untrusted = UntrustedCache(redis, "live")
    asyncio.run(untrusted.add(BTC))
    redis.down = True

    assert not asyncio.run(untrusted.clear())

    assert untrusted.instruments == {BTC}
