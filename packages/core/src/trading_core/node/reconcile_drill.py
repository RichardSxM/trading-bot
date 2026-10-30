"""``reconcile_drill``: the only strategy a live trader loads in ``LIVE_MODE=drill`` (spec 0023 AC-22).

It never trades on a signal. It sends orders only for a ``drill_step`` control command from ``cli``
(``run_step``), always of the drill size (``trading_core.reconcile.drill``), and never while a
position or an order of its own is open, so it holds at most one position of the minimum size:

- ``rest_order``: a post only limit buy at the mid x (1 - ``DRILL_REST_DISTANCE_PCT``), far from the
  market, so it rests.
- ``rest_order_and_die``: the same, then ``os._exit(1)`` once Nautilus hands it to Kraken
  (``OrderSubmitted``) and before any acknowledgement can arrive (drill D3).
- ``open_position``: a market buy. When it fills, a reduce only sell stop at the fill price x
  (1 - ``DRILL_STOP_PCT``), tagged ``parent:<entry>``. A stop Kraken refuses closes the position at
  market. The step answers once the stop is accepted.
- ``flatten``: closes the position at market (reduce only) and cancels its entries; its stops are
  cancelled once the position is closed, never before.

Entry steps are refused while the trader is paused or halted, or while any entry block is active
(``reconciling``, ``exchange_unknown``, ``order_unresolved`` and the rest). After a restart it keeps its
position and stop (``resume_position``) and cancels any resting entry when entries first resume (D2).
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Final

from nautilus_trader.model.enums import OrderSide, OrderType, PositionSide, TimeInForce, TriggerType
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderDenied,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
    PositionClosed,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.control.consumer import Outcome
from trading_core.control.messages import ControlMessage
from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate, entry_orders
from trading_core.reconcile.drill import (
    ENTRY_STEPS,
    DrillRefusal,
    DrillStep,
    drill_size,
    parse_step,
    rest_price,
    step_refusal,
    stop_trigger,
)
from trading_core.recording.enums import ControlOutcome, OrderOrigin
from trading_core.recording.mappers import HALT_TAG, ORIGIN_TAG, PARENT_TAG
from trading_core.strategy.blocks import EntryBlocks

log = logging.getLogger(__name__)

# How long ``open_position`` waits for the fill and the stop's acceptance before it answers ``failed``.
CONFIRM_TIMEOUT_S: Final = 15.0
# ``rest_order_and_die`` exits this long after ``OrderSubmitted``: the request is on its way, the answer is not.
DIE_AFTER_SUBMIT_S: Final = 0.1


def _die() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(1)


class ReconcileDrillConfig(KfsStrategyConfig, frozen=True, kw_only=True):
    """The drill's distances (``DRILL_REST_DISTANCE_PCT``, ``DRILL_STOP_PCT``) and its waits."""

    rest_distance_pct: Decimal = Decimal(20)
    stop_pct: Decimal = Decimal(2)
    confirm_timeout_s: float = CONFIRM_TIMEOUT_S
    die_after_submit_s: float = DIE_AFTER_SUBMIT_S


class ReconcileDrill(KfsStrategy):
    """Runs the live drill's steps (see the module docstring). ``die`` ends the process (tests replace it)."""

    def __init__(
        self, config: ReconcileDrillConfig, gate: PauseGate, blocks: EntryBlocks, die: Callable[[], None] = _die
    ) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._die = die
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        # Entries whose ``OrderSubmitted`` ends the process.
        self._dying: set[ClientOrderId] = set()
        # ``open_position`` waits on these, keyed by the entry: True once its stop is accepted.
        self._confirms: dict[ClientOrderId, asyncio.Future[bool]] = {}
        # Each stop and the entry it protects.
        self._stops: dict[ClientOrderId, ClientOrderId] = {}
        self._entries_cancelled = False

    def on_start(self) -> None:
        """Subscribe to quotes: ``rest_order`` prices off the last mid."""
        super().on_start()
        self.subscribe_quote_ticks(self._instrument_id)

    # Steps ----------------------------------------------------------------------------------

    async def run_step(self, msg: ControlMessage) -> Outcome:
        """Run one ``drill_step``; ``executed`` with what it sent, or ``failed`` with a ``DrillRefusal`` reason."""
        step = parse_step(msg.step)
        refusal = step_refusal(
            step, msg.source, position_open=self._position() is not None, order_open=bool(self._open_entries())
        )
        if refusal is None and step in ENTRY_STEPS and self._entries_blocked():
            refusal = DrillRefusal.BLOCKED
        instrument = self.cache.instrument(self._instrument_id)
        if refusal is None and instrument is None:
            refusal = DrillRefusal.NO_INSTRUMENT
        if refusal is not None or step is None or instrument is None:
            reason = refusal or DrillRefusal.UNKNOWN_STEP
            log.warning(
                "drill step refused",
                extra={"fields": {"code": "drill_step_refused", "step": msg.step, "reason": reason.value}},
            )
            return Outcome(ControlOutcome.FAILED, reason.value)
        log.warning("drill step", extra={"fields": {"code": "drill_step", "step": step.value}})
        if step is DrillStep.FLATTEN:
            return self._flatten_step()
        if step is DrillStep.OPEN_POSITION:
            return await self._open_position(instrument)
        return self._rest_order(instrument, die=step is DrillStep.REST_ORDER_AND_DIE)

    def _rest_order(self, instrument: Instrument, die: bool) -> Outcome:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            return Outcome(ControlOutcome.FAILED, DrillRefusal.NO_QUOTE.value)
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        price = rest_price(mid, self._cfg.rest_distance_pct, instrument.price_increment.as_decimal())
        order = self.order_factory.limit(
            instrument.id,
            OrderSide.BUY,
            self._size(instrument),
            instrument.make_price(price),
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        if die:
            self._dying.add(order.client_order_id)
        self.submit_order(order)
        return Outcome(ControlOutcome.EXECUTED, details=_order_details(order, price=str(price)))

    async def _open_position(self, instrument: Instrument) -> Outcome:
        order = self.order_factory.market(instrument.id, OrderSide.BUY, self._size(instrument))
        confirmed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirms[order.client_order_id] = confirmed
        self.submit_order(order)
        try:
            async with asyncio.timeout(self._cfg.confirm_timeout_s):
                accepted = await confirmed
        except TimeoutError:
            return Outcome(ControlOutcome.FAILED, "not_confirmed", _order_details(order))
        finally:
            self._confirms.pop(order.client_order_id, None)
        if not accepted:
            return Outcome(ControlOutcome.FAILED, "not_opened", _order_details(order))
        return Outcome(ControlOutcome.EXECUTED, details=_order_details(order))

    def _flatten_step(self) -> Outcome:
        self._close_position([f"{ORIGIN_TAG}{OrderOrigin.STRATEGY}"])
        self._cancel_entries()
        if self._position() is None:
            self._cancel_stops()
        return Outcome(ControlOutcome.EXECUTED)

    # Order events ---------------------------------------------------------------------------

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        """End the process right after a ``rest_order_and_die`` entry went to Kraken (drill D3)."""
        if event.client_order_id not in self._dying:
            return
        log.critical(
            "drill exiting before the acknowledgement",
            extra={"fields": {"code": "drill_die", "client_order_id": str(event.client_order_id)}},
        )
        asyncio.get_running_loop().call_later(self._cfg.die_after_submit_s, self._die)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Protect a filled ``open_position`` entry with its stop."""
        order = self.cache.order(event.client_order_id)
        if order is None or order.is_reduce_only or not order.is_closed:
            return
        self._place_stop(order)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """A stop accepted: ``open_position`` may answer ``executed``."""
        entry = self._stops.get(event.client_order_id)
        if entry is not None:
            self._confirm(entry, True)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A refused entry or stop; a refused stop closes the position (never left without one)."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        """Same as a rejection."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_position_closed(self, event: PositionClosed) -> None:
        """Flat: cancel the stops left behind."""
        if event.instrument_id == self._instrument_id:
            self._cancel_stops()

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        self._confirm(client_order_id, False)
        entry = self._stops.get(client_order_id)
        if entry is None:
            return
        log.critical(
            "drill stop refused, closing the position",
            extra={"fields": {"code": "drill_stop_refused", "client_order_id": str(client_order_id), "why": why}},
        )
        self._confirm(entry, False)
        self._close_position([f"{ORIGIN_TAG}{OrderOrigin.STRATEGY}"])

    def _confirm(self, entry: ClientOrderId, accepted: bool) -> None:
        future = self._confirms.get(entry)
        if future is not None and not future.done():
            future.set_result(accepted)

    def _place_stop(self, entry: Order) -> None:
        instrument = self.cache.instrument(self._instrument_id)
        position = self._position()
        if instrument is None or position is None or entry.avg_px is None:
            log.critical(
                "drill stop not placed, closing the position",
                extra={"fields": {"code": "drill_stop_not_placed", "client_order_id": str(entry.client_order_id)}},
            )
            self._close_position([f"{ORIGIN_TAG}{OrderOrigin.STRATEGY}"])
            return
        trigger = stop_trigger(Decimal(str(entry.avg_px)), self._cfg.stop_pct, instrument.price_increment.as_decimal())
        stop = self.order_factory.stop_market(
            instrument.id,
            OrderSide.SELL,
            position.quantity,
            trigger_price=instrument.make_price(trigger),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
            tags=[f"{PARENT_TAG}{entry.client_order_id}"],
        )
        self._stops[stop.client_order_id] = entry.client_order_id
        log.warning(
            "drill stop placed",
            extra={
                "fields": {
                    "code": "drill_stop",
                    "client_order_id": str(stop.client_order_id),
                    "trigger_price": str(trigger),
                }
            },
        )
        self.submit_order(stop, position_id=position.id)

    # Kill switch, restart -------------------------------------------------------------------

    def flatten(self, halt_id: uuid.UUID) -> None:
        """Close the position and cancel the entries for the kill switch (idempotent)."""
        self._cancel_entries()
        self._close_position([f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"])

    def resume_position(self, position: Position, stop_order: Order | None) -> None:
        """After a restart (AC-16): keep the position and its stop, cancel any resting entry."""
        self._cancel_entries()

    def signals_after(self, finished_ns: int) -> None:
        """Entries resume: the first time, a resting entry left from before the restart is cancelled (D2)."""
        super().signals_after(finished_ns)
        if not self._entries_cancelled:
            self._entries_cancelled = True
            self._cancel_entries()

    # Helpers --------------------------------------------------------------------------------

    def _size(self, instrument: Instrument) -> Quantity:
        minimum = instrument.min_quantity
        size = drill_size(instrument.size_increment.as_decimal(), None if minimum is None else minimum.as_decimal())
        return instrument.make_qty(size)

    def _position(self) -> Position | None:
        positions = self.cache.positions_open(instrument_id=self._instrument_id, strategy_id=self.id)
        return positions[0] if positions else None

    def _open_entries(self) -> list[Order]:
        return entry_orders(
            [*self.cache.orders_open(strategy_id=self.id), *self.cache.orders_inflight(strategy_id=self.id)]
        )

    def _entries_blocked(self) -> bool:
        return self._gate.paused or self._gate.halted or bool(self._blocks.active())

    def _close_position(self, tags: list[str]) -> None:
        position = self._position()
        if position is None:
            return
        pending = [
            o
            for o in self.cache.orders_inflight(strategy_id=self.id)
            if o.is_reduce_only and o.order_type == OrderType.MARKET
        ]
        if pending:
            return  # a close is already on its way
        order = self.order_factory.market(
            position.instrument_id,
            OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,
        )
        self.submit_order(order, position_id=position.id)

    def _cancel_stops(self) -> None:
        for order in self.cache.orders_open(strategy_id=self.id):
            if order.is_reduce_only and not order.is_pending_cancel:
                self.cancel_order(order)


def _order_details(order: Order, **extra: str) -> dict[str, str]:
    return {"client_order_id": str(order.client_order_id), "quantity": str(order.quantity), **extra}
