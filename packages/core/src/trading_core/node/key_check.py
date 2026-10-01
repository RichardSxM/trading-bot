"""The live startup key check (spec 0003 AC-8): pure rules plus a watcher for the node's event loop.

No NautilusTrader import: the trader passes in the predicate that reads its cache.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

log = logging.getLogger(__name__)


KeyCheckState = Literal["confirmed", "waiting", "failed"]


def key_check_state(account_confirmed: bool, elapsed_s: float, timeout_s: float) -> KeyCheckState:
    """Where the live startup key check stands (spec 0003 AC-8).

    ``confirmed`` once an authenticated Kraken account is in the cache, ``failed`` when
    ``timeout_s`` passed without one, else ``waiting``. A confirmation always wins, even late.
    """
    if account_confirmed:
        return "confirmed"
    return "failed" if elapsed_s >= timeout_s else "waiting"


async def watch_key_check(
    is_confirmed: Callable[[], bool],
    on_failed: Callable[[], None],
    timeout_s: float,
    poll_s: float = 1.0,
    now: Callable[[], float] = time.monotonic,
) -> KeyCheckState:
    """Poll ``is_confirmed`` until the key check decides (spec 0003 AC-8).

    Runs as a task on the node's event loop rather than as an actor: when the Kraken client
    cannot connect, Nautilus never starts the trader, so an actor would never run. On
    failure it logs ``kraken_auth_failed`` and calls ``on_failed`` (which stops the node).
    """
    started = now()
    while True:
        elapsed_s = now() - started
        state = key_check_state(is_confirmed(), elapsed_s, timeout_s)
        if state == "confirmed":
            log.info("kraken account confirmed", extra={"fields": {"elapsed_s": round(elapsed_s, 1)}})
            return state
        if state == "failed":
            log.critical(
                "no authenticated Kraken account, stopping",
                extra={"fields": {"code": "kraken_auth_failed", "timeout_s": timeout_s}},
            )
            on_failed()
            return state
        await asyncio.sleep(poll_s)
