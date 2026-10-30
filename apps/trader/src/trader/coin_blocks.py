"""Entry blocks per coin over the shared ones, aware of each coin's stale feed (spec 0025 AC-19).

Every strategy runs its own frozen stale check (``trading_core.node.stale``), which adds
``STALE_FEED`` when its coin's quotes go stale and clears it when they are fresh. With one shared
``EntryBlocks``, a fresh BTC would clear a stale PEPE's block every second. So each coin's strategy
gets a ``CoinBlocks``: every reason goes straight to the shared blocks, except ``STALE_FEED``, which
it keeps per coin. The shared block goes on when the first coin goes stale and comes off only when
no coin is stale.
"""

import threading
from collections.abc import Callable, Sequence

from trading_core.strategy.blocks import BlockReason, EntryBlocks


class _StaleCoins:
    """The coins whose stale check last found them stale, shared by every coin's ``CoinBlocks``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.coins: set[str] = set()


class CoinBlocks(EntryBlocks):
    """One coin's view of the shared ``EntryBlocks``, with the same methods.

    ``add`` and ``clear`` return what they return for this coin: ``add(STALE_FEED)`` is True when
    this coin was not stale yet (so its stale check records one risk event), ``clear(STALE_FEED)``
    is True when it was.
    """

    def __init__(self, shared: EntryBlocks, coin: str, stale: _StaleCoins) -> None:
        super().__init__()
        self._shared = shared
        self._coin = coin
        self._stale = stale

    def subscribe(self, listener: Callable[[BlockReason, bool], None]) -> None:
        """Listen on the shared blocks."""
        self._shared.subscribe(listener)

    def add(self, reason: BlockReason) -> bool:
        """Block entries on every coin; ``STALE_FEED`` also marks this coin stale."""
        if reason != BlockReason.STALE_FEED:
            return self._shared.add(reason)
        with self._stale.lock:
            if self._coin in self._stale.coins:
                return False
            self._stale.coins.add(self._coin)
            self._shared.add(reason)
            return True

    def clear(self, reason: BlockReason) -> bool:
        """Lift ``reason``; ``STALE_FEED`` comes off the shared blocks only once no coin is stale."""
        if reason != BlockReason.STALE_FEED:
            return self._shared.clear(reason)
        with self._stale.lock:
            if self._coin not in self._stale.coins:
                return False
            self._stale.coins.discard(self._coin)
            if not self._stale.coins:
                self._shared.clear(reason)
            return True

    def active(self) -> tuple[BlockReason, ...]:
        """The shared active reasons."""
        return self._shared.active()

    def since(self) -> dict[str, int]:
        """The shared ``entries_blocked_since`` field."""
        return self._shared.since()


def coin_blocks(shared: EntryBlocks, coins: Sequence[str]) -> dict[str, CoinBlocks]:
    """One ``CoinBlocks`` per coin, all over ``shared`` and one set of stale coins."""
    stale = _StaleCoins()
    return {coin: CoinBlocks(shared, coin, stale) for coin in coins}
