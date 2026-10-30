"""``UntrustedCache``: the instruments whose cached position is wrong (spec 0023 AC-8).

A ``cache_fix: manual`` finding adds its instrument. While it is in the set, the kill switch's
flatten, re-flatten, and halted restart flatten skip the owning strategy's own close (its cached
size is wrong), the ``ReconcileExecutor`` closes what Kraken holds instead, and the wait for flat
asks Kraken. The set lives in memory and in the Redis set ``{env}:reconcile:cache_untrusted``, so
it survives a restart; ``trader.cache reset`` or a clean start check clears it.

The instrument goes into memory first, before any I/O: a Redis failure can only leave the
trader believing an instrument untrusted, never the reverse.
"""

import logging
import threading
from collections.abc import Awaitable, Iterable
from typing import Protocol

from redis.exceptions import RedisError

from trading_core.reconcile.recover import cache_untrusted_key
from trading_core.settings import TradingEnv

log = logging.getLogger(__name__)


class SetRedis(Protocol):
    """The Redis set calls the untrusted cache makes (redis.asyncio.Redis satisfies it)."""

    def sadd(self, name: str, *values: str) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[object]: ...


class UntrustedCache:
    """The untrusted instruments, in memory and in Redis (see the module docstring)."""

    def __init__(self, client: SetRedis, env: TradingEnv) -> None:
        self._client = client
        self._key = cache_untrusted_key(env)
        self._lock = threading.Lock()
        self._instruments: frozenset[str] = frozenset()

    @property
    def instruments(self) -> frozenset[str]:
        """The instruments whose cached position is wrong."""
        with self._lock:
            return self._instruments

    def load(self, members: Iterable[str | bytes]) -> None:
        """Apply the set found in Redis at start."""
        found = frozenset(m.decode() if isinstance(m, bytes) else m for m in members)
        with self._lock:
            self._instruments = self._instruments | found
        if found:
            log.warning(
                "trader starts with an untrusted cache",
                extra={"fields": {"code": "cache_untrusted_loaded", "instrument_ids": sorted(found)}},
            )

    async def add(self, instrument_id: str) -> bool:
        """Mark ``instrument_id`` untrusted, in memory now and then in Redis; False when Redis was not written."""
        with self._lock:
            self._instruments = self._instruments | {instrument_id}
        try:
            await self._client.sadd(self._key, instrument_id)
        except (RedisError, OSError, TimeoutError) as exc:
            # Untrusted in memory; a restart before the next check would trust the cache again.
            log.critical(
                "untrusted cache set not written",
                extra={
                    "fields": {
                        "code": "cache_untrusted_write_failed",
                        "instrument_id": instrument_id,
                        "error": type(exc).__name__,
                    }
                },
            )
            return False
        log.critical(
            "cached position marked untrusted, the cache needs a reset",
            extra={"fields": {"code": "cache_untrusted", "instrument_id": instrument_id}},
        )
        return True

    async def clear(self) -> bool:
        """Trust the cache again (a clean start check): Redis first, then memory; False when Redis failed."""
        if not self.instruments:
            return True
        try:
            await self._client.delete(self._key)
        except (RedisError, OSError, TimeoutError) as exc:
            log.error(
                "untrusted cache set not cleared",
                extra={"fields": {"code": "cache_untrusted_clear_failed", "error": type(exc).__name__}},
            )
            return False
        with self._lock:
            cleared, self._instruments = self._instruments, frozenset()
        log.info(
            "cache trusted again after a clean start check",
            extra={"fields": {"code": "cache_untrusted_cleared", "instrument_ids": sorted(cleared)}},
        )
        return True
