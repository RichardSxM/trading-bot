"""The pause gate and the strategy base class every trader strategy subclasses (spec 0005 AC-6, AC-9).

While the trader is paused, ``KfsStrategy`` refuses every order that is not reduce only before
it reaches the engine, also when flat. The NautilusTrader risk engine in ``REDUCING`` is the
second layer. Exits and protective stops must be reduce only, so they still go through.

On stop (SIGTERM) a strategy pauses the gate in memory only and cancels its open entry orders.
It never cancels a reduce only order and never closes a position.
"""

import logging
import threading
from collections.abc import Iterable

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import ClientId, PositionId
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.orders.list import OrderList
from nautilus_trader.trading.strategy import Strategy

from trading_core.control.pause import PauseState

log = logging.getLogger(__name__)


class PauseGate:
    """Whether the trader is paused, and by whom; one per trader process, shared by every strategy.

    ``state`` is the value of the pause key. A pause in memory only (on stop) has no state.
    ``loaded`` stays False until the pause key was read at start (or found unreadable).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False
        self._loaded = False
        self._state: PauseState | None = None
        self._cancelled = 0

    @property
    def loaded(self) -> bool:
        """True once the gate holds the state the pause key gave at start."""
        with self._lock:
            return self._loaded

    @property
    def paused(self) -> bool:
        """True while new entries are refused."""
        with self._lock:
            return self._paused

    @property
    def state(self) -> PauseState | None:
        """The pause key's value while paused through a command, else None."""
        with self._lock:
            return self._state

    @property
    def cancelled(self) -> int:
        """How many entry orders the strategies cancelled on stop."""
        with self._lock:
            return self._cancelled

    def pause(self, state: PauseState) -> None:
        """Pause through a command (or the key found at start)."""
        with self._lock:
            self._paused = True
            self._loaded = True
            self._state = state

    def pause_in_memory(self) -> None:
        """Pause for shutdown without a key, keeping any existing state."""
        with self._lock:
            self._paused = True
            self._loaded = True

    def unpause(self) -> None:
        """Allow entries again."""
        with self._lock:
            self._paused = False
            self._loaded = True
            self._state = None

    def note_cancelled(self, count: int) -> None:
        """Add ``count`` entry orders cancelled on stop."""
        with self._lock:
            self._cancelled += count


def entry_orders(orders: Iterable[Order]) -> list[Order]:
    """The orders that are not reduce only, each once."""
    seen: set[object] = set()
    found: list[Order] = []
    for order in orders:
        if order.is_reduce_only or order.client_order_id in seen:
            continue
        seen.add(order.client_order_id)
        found.append(order)
    return found


class KfsStrategy(Strategy):  # type: ignore[misc]  # Strategy is Cython with no stubs
    """Base class of every trader strategy: honours the pause gate and cancels entries on stop.

    A subclass must mark every exit and protective stop reduce only, and add nothing to
    ``on_stop`` that touches orders or positions (call ``super().on_stop()`` if it overrides it).
    """

    def __init__(self, config: StrategyConfig, gate: PauseGate) -> None:
        super().__init__(config)
        self._gate = gate

    def submit_order(
        self,
        order: Order,
        position_id: PositionId | None = None,
        client_id: ClientId | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Submit ``order``, unless the trader is paused and it is not reduce only."""
        if self._gate.paused and not order.is_reduce_only:
            self._deny(order)
            return
        super().submit_order(order, position_id, client_id, params)

    def submit_order_list(
        self,
        order_list: OrderList,
        position_id: PositionId | None = None,
        client_id: ClientId | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Submit ``order_list``, unless the trader is paused and any order in it is not reduce only."""
        if self._gate.paused:
            entries = entry_orders(order_list.orders)
            if entries:
                for order in entries:
                    self._deny(order)
                return
        super().submit_order_list(order_list, position_id, client_id, params)

    def on_stop(self) -> None:
        """Pause in memory and cancel this strategy's open entry orders; positions and stops stay."""
        self._gate.pause_in_memory()
        self.cancel_entry_orders()

    def cancel_entry_orders(self) -> int:
        """Cancel this strategy's open and in flight orders that are not reduce only; returns how many."""
        orders = entry_orders(
            [*self.cache.orders_open(strategy_id=self.id), *self.cache.orders_inflight(strategy_id=self.id)]
        )
        for order in orders:
            self.cancel_order(order)
        self._gate.note_cancelled(len(orders))
        return len(orders)

    def _deny(self, order: Order) -> None:
        log.warning(
            "entry denied, trader paused",
            extra={
                "fields": {
                    "code": "entry_denied_paused",
                    "strategy_id": str(self.id),
                    "client_order_id": str(order.client_order_id),
                }
            },
        )
