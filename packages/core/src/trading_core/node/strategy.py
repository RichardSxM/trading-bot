"""The pause gate and the strategy base class every trader strategy subclasses (spec 0005 AC-6, AC-9, spec 0010).

While the trader is paused or halted, ``KfsStrategy`` refuses every order that is not reduce only before
it reaches the engine, also when flat. The NautilusTrader risk engine in ``REDUCING`` is the
second layer. Exits and protective stops must be reduce only, so they still go through.

On stop (SIGTERM) a strategy pauses the gate in memory only and cancels its open entry orders.
It never cancels a reduce only order and never closes a position.

The kill switch calls ``flatten(halt_id)`` on every strategy (spec 0010 AC-1): the base class
cancels the entry orders; a strategy that holds positions overrides it to close them through
its own exit loop, tagging those orders ``origin:kill_switch`` and ``halt:<halt_id>``.

With ``use_market_context`` on, the base class also subscribes the instrument's market context
and folds every update into a ``ContextBook``; ``market_context()`` reads it (spec 0009 AC-11).
"""

import logging
import threading
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from nautilus_trader.common.component import LiveClock
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import FundingRateUpdate, IndexPriceUpdate, MarkPriceUpdate
from nautilus_trader.model.identifiers import ClientId, InstrumentId, PositionId
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.orders.list import OrderList
from nautilus_trader.trading.strategy import Strategy

from trading_core.control.halt import HaltState
from trading_core.control.pause import PauseState
from trading_core.marketdata.context import ContextBook, ContextLookAheadError, Liquidation, MarketContext, MaxAges
from trading_core.marketdata.hours import SECOND_NS
from trading_core.node.context_types import LiquidationTick, OpenInterestUpdate, context_data_type

log = logging.getLogger(__name__)

# ``context_clock_behind`` is logged at most once per this long (AC-11).
CLOCK_BEHIND_EVERY_NS: Final = 60 * SECOND_NS


class KfsStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """What every trader strategy's config holds: its instrument, and the market context it may read.

    The ``context_*`` values default to ``ContextSettings``'; the trader passes its settings, the lab
    the run file's ``[context]`` table (spec 0009 AC-11, AC-13).
    """

    instrument_id: str
    use_market_context: bool = False
    context_max_age_mark_s: int = 30
    context_max_age_index_s: int = 30
    context_max_age_open_interest_s: int = 30
    context_max_age_funding_s: int = 7_200
    context_liquidation_window_s: int = 60
    # False for a source with no liquidation data (Tardis), so the window is never known (AC-10).
    context_liquidations_available: bool = True


class PauseGate:
    """Whether the trader is paused or halted, and by whom; one per trader process, shared by every strategy.

    ``state`` is the value of the pause key. A pause in memory only (on stop) has no state.
    ``halt_state`` is the value of the halt key (spec 0010 AC-5), or the halt applied in memory.
    ``loaded`` stays False until the pause and halt keys were read at start (or found unreadable).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False
        self._loaded = False
        self._state: PauseState | None = None
        self._halt: HaltState | None = None
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
    def halted(self) -> bool:
        """True while halted: entries are refused until a successful ``resume``."""
        with self._lock:
            return self._halt is not None

    @property
    def halt_state(self) -> HaltState | None:
        """Why, by whom, and when the trader halted, else None."""
        with self._lock:
            return self._halt

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

    def halt(self, state: HaltState) -> bool:
        """Halt with ``state``; True when newly halted, False (keeping the first state) when already halted."""
        with self._lock:
            if self._halt is not None:
                return False
            self._halt = state
            return True

    def unhalt(self) -> None:
        """Lift the halt (a successful ``resume``)."""
        with self._lock:
            self._halt = None

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
    A subclass that overrides ``on_start`` or a market context handler calls ``super()`` first.
    """

    def __init__(self, config: KfsStrategyConfig, gate: PauseGate) -> None:
        super().__init__(config)
        self._gate = gate
        self._context_instrument = InstrumentId.from_str(config.instrument_id)
        self._context: ContextBook | None = None
        if config.use_market_context:
            ages = MaxAges.from_seconds(
                mark=config.context_max_age_mark_s,
                index=config.context_max_age_index_s,
                open_interest=config.context_max_age_open_interest_s,
                funding=config.context_max_age_funding_s,
            )
            self._context = ContextBook(
                ages,
                config.context_liquidation_window_s * SECOND_NS,
                liquidations_available=config.context_liquidations_available,
            )
        self._clock_behind_logged_ns: int | None = None

    def on_start(self) -> None:
        """Subscribe the instrument's market context when ``use_market_context`` is on."""
        if self._context is None:
            return
        instrument_id = self._context_instrument
        self.subscribe_mark_prices(instrument_id)
        self.subscribe_index_prices(instrument_id)
        self.subscribe_funding_rates(instrument_id)
        for cls in (OpenInterestUpdate, LiquidationTick):
            # The message bus only: ContextFeedActor publishes these. ``subscribe_data`` would also
            # send a SubscribeData to the Kraken data client, which has no handler for it and logs
            # an error on every start. The topic is the one ``publish_data`` uses.
            data_type = context_data_type(cls, instrument_id)
            self.msgbus.subscribe(topic=f"data.{data_type.topic}", handler=self.handle_data)

    def on_mark_price(self, update: MarkPriceUpdate) -> None:
        """Fold a mark price into the market context."""
        if self._context is not None and update.instrument_id == self._context_instrument:
            self._context.apply_mark(update.value.as_decimal(), update.ts_init)

    def on_index_price(self, update: IndexPriceUpdate) -> None:
        """Fold an index price into the market context."""
        if self._context is not None and update.instrument_id == self._context_instrument:
            self._context.apply_index(update.value.as_decimal(), update.ts_init)

    def on_data(self, data: Data) -> None:
        """Fold open interest and liquidations into the market context; other data is ignored."""
        if self._context is None or getattr(data, "instrument_id", None) != self._context_instrument:
            return
        if isinstance(data, OpenInterestUpdate):
            self._context.apply_open_interest(data.value.as_decimal(), data.ts_init)
        elif isinstance(data, LiquidationTick):
            self._context.apply_liquidation(
                Liquidation(
                    trade_id=data.trade_id,
                    kind=data.kind,
                    aggressor_side=data.aggressor_side,
                    price=data.price.as_decimal(),
                    size=data.size.as_decimal(),
                    ts_init=data.ts_init,
                )
            )

    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        """Fold a funding rate into the market context."""
        if self._context is not None and update.instrument_id == self._context_instrument:
            self._context.apply_funding(Decimal(str(update.rate)), update.next_funding_ns, update.ts_init)

    def warm_market_context(self, updates: Iterable[Data]) -> None:
        """Fold context rows known before a replay starts, through the same handlers as live.

        The lab calls this with each segment's warmup rows (spec 0009 AC-13): the backtest clock
        cannot go back before the segment start, so the engine cannot deliver them itself.
        """
        if self._context is None:
            return
        for update in updates:
            if isinstance(update, MarkPriceUpdate):
                self.on_mark_price(update)
            elif isinstance(update, IndexPriceUpdate):
                self.on_index_price(update)
            elif isinstance(update, FundingRateUpdate):
                self.on_funding_rate(update)
            else:
                self.on_data(update)

    def market_context(self) -> MarketContext:
        """What was known now, by this strategy's clock (spec 0009 AC-11).

        Raises ``RuntimeError`` when ``use_market_context`` is off. In a replay a look ahead raises;
        live, where two clocks may sit a hair apart, the snapshot is taken at the latest update instead.
        """
        if self._context is None:
            raise RuntimeError("market_context() needs use_market_context = true in the strategy config")
        now_ns = self.clock.timestamp_ns()
        try:
            return self._context.snapshot(now_ns)
        except ContextLookAheadError as exc:
            if not isinstance(self.clock, LiveClock):
                raise
            last = self._clock_behind_logged_ns
            if last is None or now_ns - last >= CLOCK_BEHIND_EVERY_NS:
                self._clock_behind_logged_ns = now_ns
                log.warning(
                    "strategy clock behind the latest context update",
                    extra={
                        "fields": {
                            "code": "context_clock_behind",
                            "strategy_id": str(self.id),
                            "behind_ns": exc.latest_ns - exc.now_ns,
                        }
                    },
                )
            return self._context.snapshot(exc.latest_ns)

    def submit_order(
        self,
        order: Order,
        position_id: PositionId | None = None,
        client_id: ClientId | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Submit ``order``, unless the trader is paused or halted and it is not reduce only."""
        if (self._gate.paused or self._gate.halted) and not order.is_reduce_only:
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
        """Submit ``order_list``, unless the trader is paused or halted and any order in it is not reduce only."""
        if self._gate.paused or self._gate.halted:
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

    def flatten(self, halt_id: uuid.UUID) -> None:
        """Close this strategy's positions and cancel its entry orders for the kill switch (spec 0010 AC-1).

        Called again on every poll while the kill switch waits for flat, so it must be idempotent.
        The base class only cancels entries; a strategy that opens positions overrides it.
        """
        self._cancel_entries()

    def has_open_positions(self) -> bool:
        """True while a position this strategy must close on a flatten is open (the kill switch re-flattens it)."""
        return bool(self.cache.positions_open(strategy_id=self.id))

    def cancel_entry_orders(self) -> int:
        """Cancel this strategy's open and in flight orders that are not reduce only; returns how many."""
        count = self._cancel_entries()
        self._gate.note_cancelled(count)
        return count

    def _cancel_entries(self) -> int:
        orders = entry_orders(
            [*self.cache.orders_open(strategy_id=self.id), *self.cache.orders_inflight(strategy_id=self.id)]
        )
        for order in orders:
            if not order.is_pending_cancel:
                self.cancel_order(order)
        return len(orders)

    def _deny(self, order: Order) -> None:
        halted = self._gate.halted
        log.warning(
            "entry denied, trader halted" if halted else "entry denied, trader paused",
            extra={
                "fields": {
                    "code": "entry_denied_halted" if halted else "entry_denied_paused",
                    "strategy_id": str(self.id),
                    "client_order_id": str(order.client_order_id),
                }
            },
        )
