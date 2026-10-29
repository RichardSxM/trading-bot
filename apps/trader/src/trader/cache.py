"""``python -m trader.cache reset``: the hand reset of the trader's Nautilus cache (spec 0023 AC-8, AC-26).

After a ``cache_fix: manual`` halt the cache holds a position Kraken does not, and Nautilus cannot
shrink it. Once Kraken is flat and the trader is stopped, this deletes the trader's own cache keys,
``trader-<trader id>:*`` (the same rule as ``purge_demo_cache``), and the untrusted cache set. It
never touches our other ``{env}:*`` keys, so the halt stays latched: the next start reloads nothing,
the start check compares an empty cache with Kraken, and you press Resume once it is clean.

It refuses, exit 1, while the trader looks alive, since a running trader would write the cache
straight back. "Alive" is the trader's heartbeat snapshot, ``{env}:snapshot:heartbeat``, written
every second: a one off container cannot see the heartbeat file inside the trader's own
container. A heartbeat younger than ``HEARTBEAT_TTL_S``, or one that does not parse, refuses. It
prints how many keys it deleted.
"""

import os
import sys
import time
from collections.abc import Callable, Iterator
from typing import Protocol

from trader.main import TraderSettings, cache_client, purge_demo_cache
from trader.snapshot_writer import HEARTBEAT_TTL_S
from trading_core.boot import boot
from trading_core.naming import node_id
from trading_core.reconcile.recover import cache_untrusted_key
from trading_core.secrets import REDIS_PASSWORD
from trading_core.settings import TradingEnv
from trading_core.snapshot.envelope import SnapshotFormatError, decode, snapshot_key

USAGE = "usage: python -m trader.cache reset"


class ResetClient(Protocol):
    """The Redis calls the reset makes (the sync redis-py client satisfies it)."""

    def scan_iter(self, match: str, count: int) -> Iterator[str | bytes]: ...

    def unlink(self, *names: str | bytes) -> int: ...

    def get(self, name: str) -> object: ...


class TraderRunning(Exception):
    """The trader looks alive, so the cache must not be reset."""


def reset(client: ResetClient, trader_id: str, env: TradingEnv, alive: Callable[[], bool]) -> int:
    """Delete ``trader-<trader_id>:*`` and the untrusted set; returns how many keys went.

    Raises ``TraderRunning`` without deleting anything while ``alive()`` is True.
    """
    if alive():
        raise TraderRunning("the trader heartbeat is fresh: stop the trader first (docker compose ps)")
    deleted = purge_demo_cache(client, trader_id)
    deleted += client.unlink(cache_untrusted_key(env))
    return deleted


def heartbeat_alive(client: ResetClient, env: TradingEnv, now_ns: Callable[[], int] = time.time_ns) -> bool:
    """True while the trader's heartbeat snapshot is younger than ``HEARTBEAT_TTL_S``; a value that does not
    parse counts as alive (fail closed). A Redis error propagates, so the reset never runs on a guess."""
    raw = client.get(snapshot_key(env, "heartbeat"))
    if raw is None:
        return False
    if not isinstance(raw, str | bytes):
        return True
    try:
        beat = decode(raw)
    except SnapshotFormatError:
        return True
    return now_ns() - beat.ts_ns < HEARTBEAT_TTL_S * 1_000_000_000


def main(argv: list[str] | None = None) -> None:
    """Run ``reset``; exit 1 on a wrong command or while the trader runs."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["reset"]:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    started = boot("node", settings_cls=TraderSettings, secrets=(REDIS_PASSWORD,))
    settings = started.settings
    env = settings.trading_env
    trader_id = node_id("TRADER", env, os.environ.get("TRADER_ID"))
    client = cache_client(settings, started.secrets[REDIS_PASSWORD])
    try:
        deleted = reset(client, trader_id, env, lambda: heartbeat_alive(client, env))
    except TraderRunning as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"deleted {deleted} keys of trader-{trader_id} and {cache_untrusted_key(env)}")


if __name__ == "__main__":
    main()
