"""``MakerProbe``: the random taker's timing and side draw with a post only entry (spec 0008, *maker_probe*).

It has no edge by design. It exists so the maker effects (fill rate and markouts, AC-10 and
AC-18) can be measured before real candidates arrive. In ``maker`` mode (the default) it posts a
post only limit at the best bid (buy) or best ask (sell) of the last quote, and cancels it if no
fill came within ``entry_timeout_s``; the round trip then ends quietly, like a boundary that
drew nothing. In ``taker`` mode it enters at market, exactly like ``RandomTaker``. Either way it
exits at market ``hold_s`` seconds after the entry's first fill.
"""

import logging
from typing import Final, Literal
from uuid import UUID

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders.base import Order

from trading_core.node.placeholder import Record
from trading_core.node.random_taker import Phase, RandomTaker, RandomTakerConfig
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import OrderSide
from trading_core.recording.mappers import DECISION_TAG
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.random_taker import NS_PER_S

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "maker_probe"

_ENTRY_TIMEOUT: Final = "maker_probe_entry_timeout"


class MakerProbeConfig(RandomTakerConfig, frozen=True):
    """The random taker's settings plus how to enter: ``maker`` (post only at the touch) or ``taker``."""

    entry_mode: Literal["maker", "taker"] = "maker"
    entry_timeout_s: int = 10


class MakerProbe(RandomTaker):
    """Posts at the touch (or takes) on a random side every boundary while flat, exits at market after ``hold_s``."""

    _round_trip_timers = (*RandomTaker._round_trip_timers, _ENTRY_TIMEOUT)

    def __init__(self, config: MakerProbeConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self._probe_cfg = config

    def _reset(self) -> None:
        super()._reset()
        # The maker entry this strategy cancelled for timing out, so its cancel ends the round trip quietly.
        self._timed_out: ClientOrderId | None = None

    def _entry_order(self, side: OrderSide, quantity: Quantity, quote: QuoteTick, decision: UUID) -> Order:
        if self._probe_cfg.entry_mode == "taker":
            return super()._entry_order(side, quantity, quote, decision)
        buy = side == OrderSide.BUY
        return self.order_factory.limit(
            self._instrument_id,
            NautilusOrderSide.BUY if buy else NautilusOrderSide.SELL,
            quantity,
            quote.bid_price if buy else quote.ask_price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
            tags=[f"{DECISION_TAG}{decision}"],
        )

    def _entry_submitted(self) -> None:
        if self._probe_cfg.entry_mode == "maker":
            at = self.clock.timestamp_ns() + self._probe_cfg.entry_timeout_s * NS_PER_S
            self._alert(_ENTRY_TIMEOUT, at, self._on_entry_timeout)

    def _on_entry_timeout(self, event: object) -> None:
        if self.phase != Phase.ENTERING or self._entry_id is None:
            return  # filled (holding) or already back to flat
        entry = self.cache.order(self._entry_id)
        if entry is None or not entry.is_open:
            return
        self._timed_out = self._entry_id
        self.cancel_order(entry)

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        timed_out = client_order_id == self._timed_out and why == "canceled"
        if timed_out and self.phase == Phase.ENTERING and self._position() is None:
            log.debug("maker entry timed out unfilled", extra={"fields": {"code": "maker_entry_timeout"}})
            self._back_to_flat()
            return
        super()._refused(client_order_id, why)
