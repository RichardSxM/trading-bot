"""``BookImbalance`` (spec 0017 child 0004): feeds the L2 book's depth and trade ticks to the pure rule.

It runs on a 15 second clock timer aligned to the epoch instead of bars (AC-22): the firing at
``k * 15 s`` closes bucket ``k - 1``. The scalping base trades it. Replay only, since it reads the book.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Final, Literal

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.book import BookLevel
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide, BookType

from trading_core.node.scalp_base import Record, ScalpConfig, ScalpStrategy
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.book_imbalance import BUCKET_NS, BookImbalanceState
from trading_core.strategy.scalp import ScalpSignal, mid_of

STRATEGY_NAME: Final = "book_imbalance"
_AGGRESSORS: Final = {AggressorSide.BUYER: "buy", AggressorSide.SELLER: "sell"}
_FIRING: Final = "book_imbalance_firing"


class BookImbalanceConfig(ScalpConfig, frozen=True, kw_only=True):
    """``book_imbalance``'s ``[strategy.config]`` settings (child 0004, *Config and grid*)."""

    time_stop_min: int = 5
    entry_timeout_s: int = 10
    exit_mode: Literal["taker", "maker"] = "maker"
    imb_min: Decimal = Decimal("0.3")
    persist_bars: int = 2
    levels: int = 5
    stop_bps: Decimal = Decimal(9)
    target_bps: Decimal = Decimal(6)


def _sizes(levels: list[BookLevel], n: int) -> list[Decimal]:
    return [sum((order.size.as_decimal() for order in level.orders()), Decimal(0)) for level in levels[:n]]


class BookImbalance(ScalpStrategy):
    """Joins a lopsided book its trade flow agrees with (``trading_core.strategy.book_imbalance``)."""

    def __init__(self, config: BookImbalanceConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self._levels = config.levels
        self.rule = BookImbalanceState(
            imb_min=config.imb_min,
            persist_bars=config.persist_bars,
            levels=config.levels,
            stop_bps=config.stop_bps,
            target_bps=config.target_bps,
        )

    @property
    def entry_reason(self) -> str:
        """``book_imbalance_entry``."""
        return f"{STRATEGY_NAME}_entry"

    def _start_signal_source(self) -> None:
        """The L2 book, the trade ticks, and the 15 second timer from the next multiple of 15 s."""
        self.subscribe_order_book_deltas(self._instrument_id, book_type=BookType.L2_MBP)
        self.subscribe_trade_ticks(self._instrument_id)
        start_ns = (self.clock.timestamp_ns() // BUCKET_NS + 1) * BUCKET_NS
        self.clock.set_timer(
            _FIRING,
            timedelta(microseconds=BUCKET_NS // 1_000),
            start_time=unix_nanos_to_dt(start_ns),
            callback=self._on_firing,
            fire_immediately=True,
        )

    def on_stop(self) -> None:
        """The base's stop, then the timer."""
        super().on_stop()
        if _FIRING in self.clock.timer_names:
            self.clock.cancel_timer(_FIRING)

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Fold the trade into its ``ts_event`` bucket."""
        if tick.instrument_id == self._instrument_id:
            self.rule.on_trade(tick.ts_event, tick.size.as_decimal(), _AGGRESSORS.get(tick.aggressor_side, "none"))

    def _on_firing(self, event: TimeEvent) -> None:
        self._on_signal_source(lambda: self._signal_at(event.ts_event // BUCKET_NS - 1), event.ts_event)

    def _signal_at(self, bucket: int) -> ScalpSignal | None:
        """The rule's signal on the firing that closes ``bucket``: no book yet reads as empty sides."""
        book = self.cache.order_book(self._instrument_id)
        bids = _sizes(book.bids(), self._levels) if book is not None else []
        asks = _sizes(book.asks(), self._levels) if book is not None else []
        quote = self.cache.quote_tick(self._instrument_id)
        mid = mid_of(quote.bid_price.as_decimal(), quote.ask_price.as_decimal()) if quote is not None else None
        return self.rule.on_firing(bucket, bids, asks, mid)
