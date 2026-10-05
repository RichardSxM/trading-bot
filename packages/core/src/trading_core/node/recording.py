"""``RecordingActor``: every order event, fill, and position change to the recording queue (spec 0006 AC-11).

It runs on the node's event loop, so it only reads the cache, turns Nautilus objects into
plain facts, maps them with ``trading_core.recording.mappers``, and hands the rows to
``record`` (the ``RecordingWriter``'s ``put``). Postgres is touched only on the writer thread.

Order of rows on the queue matters, and the queue keeps it: the order row before its events
and fills, the position row before the fills that point at it and its snapshots.
"""

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Final
from uuid import UUID

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.events import (
    AccountState,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderInitialized,
    OrderRejected,
    PositionEvent,
)
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.model.position import Position

from trading_core.recording.enums import Env
from trading_core.recording.mappers import (
    FillFacts,
    OrderFacts,
    PositionFacts,
    fill_row,
    order_event_row,
    order_row,
    position_row,
    weighted_avg,
)
from trading_core.recording.rows import AccountSnapshotRow, InstrumentRow, JsonObject, PositionSnapshotRow
from trading_core.recording.spool import QueuedRow, WriteCall, WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_NS: Final = 60 * 1_000_000_000
_SNAPSHOT_TIMER: Final = "recording_snapshots"

type Record = Callable[[WriteItem], None]


def _fills(events: list[OrderEvent]) -> list[OrderFilled]:
    return [e for e in events if isinstance(e, OrderFilled)]


def _fill_facts(fill: OrderFilled) -> FillFacts:
    return FillFacts(
        client_order_id=fill.client_order_id.value,
        instrument_id=fill.instrument_id.value,
        trade_id=fill.trade_id.value,
        side=fill.order_side.name,
        quantity=fill.last_qty.as_decimal(),
        price=fill.last_px.as_decimal(),
        fee=fill.commission.as_decimal(),
        fee_currency=fill.commission.currency.code,
        liquidity_side=fill.liquidity_side.name,
        ts_event=fill.ts_event,
    )


def instrument_row(instrument: Instrument) -> InstrumentRow:
    """The ``instruments`` row from the adapter's instrument (a perpetual)."""
    return InstrumentRow(
        instrument_id=instrument.id.value,
        venue=instrument.id.venue.value,
        symbol=instrument.raw_symbol.value,
        base_currency=instrument.get_base_currency().code,
        quote_currency=instrument.quote_currency.code,
        settle_currency=instrument.get_settlement_currency().code,
        tick_size=instrument.price_increment.as_decimal(),
        lot_size=instrument.size_increment.as_decimal(),
        multiplier=instrument.multiplier.as_decimal(),
        is_inverse=instrument.is_inverse,
    )


def order_facts(order: Order) -> OrderFacts:
    """The order as the cache holds it, in plain values; the average comes from its fills as ``Decimal``."""
    price = getattr(order, "price", None)
    trigger = getattr(order, "trigger_price", None)
    return OrderFacts(
        client_order_id=order.client_order_id.value,
        venue_order_id=None if order.venue_order_id is None else order.venue_order_id.value,
        instrument_id=order.instrument_id.value,
        side=order.side.name,
        order_type=order.order_type.name,
        time_in_force=order.time_in_force.name,
        quantity=order.quantity.as_decimal(),
        price=None if price is None else price.as_decimal(),
        trigger_price=None if trigger is None else trigger.as_decimal(),
        reduce_only=order.is_reduce_only,
        post_only=order.is_post_only,
        status=order.status.name,
        filled_qty=order.filled_qty.as_decimal(),
        avg_px=weighted_avg([(f.last_px.as_decimal(), f.last_qty.as_decimal()) for f in _fills(order.events)]),
        tags=order.tags or [],
    )


def order_last_event_ns(order: Order, event: OrderEvent) -> int:
    """The newest event time ``order`` has seen, so its row's ``last_event_ns`` never moves backwards.

    Events come from two clocks: the strategy stamps ``OrderInitialized`` and
    ``OrderPendingCancel`` with the node's wall clock, the sandbox stamps the rest with the last
    quote's time, which trails it. Taking ``event.ts_event`` alone made the guarded upsert drop
    every later update as older.
    """
    return max([event.ts_event, *(e.ts_event for e in order.events)])


def order_for(cache: Cache, event: OrderEvent) -> Order:
    """The order behind ``event``: from the cache, or from the event itself for ``OrderInitialized``.

    On a live node the init event is published before the order reaches the cache.
    """
    order = cache.order(event.client_order_id)
    if order is not None:
        return order
    if isinstance(event, OrderInitialized):
        return OrderUnpacker.from_init(event)
    raise LookupError(f"order {event.client_order_id} not in the cache")


def position_facts(position: Position) -> PositionFacts:
    """The position in plain values; fills on its entry side open it, the others close it."""
    opening: list[tuple[Decimal, Decimal]] = []
    closing: list[tuple[Decimal, Decimal]] = []
    for fill in _fills(position.events):
        side = opening if fill.order_side == position.entry else closing
        side.append((fill.last_px.as_decimal(), fill.last_qty.as_decimal()))
    currency = position.settlement_currency
    fees = sum((m.as_decimal() for m in position.commissions() if m.currency == currency), Decimal(0))
    realized = position.realized_pnl
    return PositionFacts(
        position_ref=position.id.value,
        instrument_id=position.instrument_id.value,
        side=position.side.name,
        quantity=position.signed_decimal_qty(),
        peak_qty=position.peak_qty.as_decimal(),
        opening=opening,
        closing=closing,
        realized_pnl=Decimal(0) if realized is None else realized.as_decimal(),
        fees_total=fees,
        currency=currency.code,
        opened_ns=position.ts_opened,
        closed_ns=position.ts_closed if position.is_closed else None,
        last_event_ns=position.ts_last,
    )


def _event_details(event: OrderEvent) -> JsonObject:
    if isinstance(event, OrderFilled):
        return {
            "trade_id": event.trade_id.value,
            "last_qty": event.last_qty.as_decimal(),
            "last_px": event.last_px.as_decimal(),
            "commission": event.commission.as_decimal(),
        }
    if isinstance(event, OrderDenied | OrderRejected):
        return {"reason": event.reason}
    return {}


class RecordingActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Maps order, position, and account events to rows and queues them; snapshots every 60 s."""

    def __init__(
        self,
        *,
        env: Env,
        run_id: UUID,
        instrument_id: InstrumentId,
        record: Record,
        blocks: EntryBlocks,
    ) -> None:
        super().__init__(ActorConfig(component_id="RecordingActor"))
        self._env = env
        self._run_id = run_id
        self._instrument_id = instrument_id
        self._venue: Venue = instrument_id.venue
        self._record = record
        self._blocks = blocks
        self._position_ids: dict[tuple[str, int], UUID] = {}
        self._linked_trades: set[str] = set()

    def on_start(self) -> None:
        """Queue the instrument, subscribe to every order, position, and account event, and start the snapshot timer.

        The instrument goes first so every order row after it has its parent (AC-11).
        """
        self._guarded(self._record_instrument)(None)
        self.msgbus.subscribe(topic="events.order.*", handler=self._guarded(self._on_order_event))
        self.msgbus.subscribe(topic="events.position.*", handler=self._guarded(self._on_position_event))
        self.msgbus.subscribe(topic="events.account.*", handler=self._guarded(self._on_account_state))
        self.clock.set_timer_ns(_SNAPSHOT_TIMER, SNAPSHOT_INTERVAL_NS, 0, 0, self._guarded(self._on_snapshot_timer))

    def on_stop(self) -> None:
        """Stop the snapshot timer; events already queued stay for the drain (AC-13)."""
        if _SNAPSHOT_TIMER in self.clock.timer_names:
            self.clock.cancel_timer(_SNAPSHOT_TIMER)

    def _guarded(self, handler: Callable[[object], None]) -> Callable[[object], None]:
        def run(event: object) -> None:
            try:
                handler(event)
            except Exception:
                # A mapper error is a bug (AC-12): stop entries until restart, keep the node running.
                self._blocks.add(BlockReason.RECORDING_FAILED)
                log.exception(
                    "recording an event failed",
                    extra={"fields": {"code": "recording_failed", "event": type(event).__name__}},
                )

        return run

    def _record_instrument(self, event: object) -> None:
        instrument = self.cache.instrument(self._instrument_id)
        if instrument is None:
            raise LookupError(f"instrument {self._instrument_id} not in the cache at start")
        self._put(WriteCall.UPSERT_INSTRUMENT, instrument_row(instrument))

    # Orders ---------------------------------------------------------------------------------

    def _on_order_event(self, event: object) -> None:
        if not isinstance(event, OrderEvent):
            return
        order = order_for(self.cache, event)
        row = order_row(
            order_facts(order), env=self._env, run_id=self._run_id, last_event_ns=order_last_event_ns(order, event)
        )
        self._put(WriteCall.UPSERT_ORDER, row)
        self._put(
            WriteCall.RECORD_ORDER_EVENT,
            order_event_row(
                env=self._env,
                run_id=self._run_id,
                client_order_id=event.client_order_id.value,
                event_id=UUID(event.id.value),
                event_class=type(event).__name__,
                details=_event_details(event),
                ts_event=event.ts_event,
            ),
        )
        if isinstance(event, OrderFilled):
            log.info(
                "fill seen",
                extra={"fields": {"code": "fill_seen", "trade_id": event.trade_id.value}},
            )
            self._put(
                WriteCall.RECORD_FILL,
                fill_row(_fill_facts(event), env=self._env, run_id=self._run_id, position_id=None),
            )

    # Positions ------------------------------------------------------------------------------

    def _on_position_event(self, event: object) -> None:
        if not isinstance(event, PositionEvent):
            return
        position = self.cache.position(event.position_id)
        if position is None:
            raise LookupError(f"position {event.position_id} not in the cache")
        facts = position_facts(position)
        row_id = self._position_ids.setdefault((facts.position_ref, facts.opened_ns), uuid.uuid7())
        self._put(WriteCall.UPSERT_POSITION, position_row(facts, env=self._env, run_id=self._run_id, row_id=row_id))
        for fill in _fills(position.events):
            if fill.trade_id.value in self._linked_trades:
                continue
            self._linked_trades.add(fill.trade_id.value)
            self._put(
                WriteCall.RECORD_FILL,
                fill_row(_fill_facts(fill), env=self._env, run_id=self._run_id, position_id=row_id),
            )
        if position.is_open:
            self._position_snapshot(position, row_id)

    # Snapshots ------------------------------------------------------------------------------

    def _on_account_state(self, event: object) -> None:
        if isinstance(event, AccountState):
            self._account_snapshot()

    def _on_snapshot_timer(self, event: object) -> None:
        self._account_snapshot()
        for position in self.cache.positions_open(instrument_id=self._instrument_id):
            row_id = self._position_ids.get((position.id.value, position.ts_opened))
            if row_id is not None:
                self._position_snapshot(position, row_id)

    def _account_snapshot(self) -> None:
        account = self.portfolio.account(self._venue)
        if account is None:
            return
        for currency in account.currencies():
            total = account.balance_total(currency)
            if total is None:
                continue
            free = account.balance_free(currency)
            locked = account.balance_locked(currency)
            self._put(
                WriteCall.RECORD_ACCOUNT_SNAPSHOT,
                AccountSnapshotRow(
                    env=self._env,
                    run_id=self._run_id,
                    account_ref=account.id.value,
                    balance_total=total.as_decimal(),
                    balance_free=Decimal(0) if free is None else free.as_decimal(),
                    margin_used=Decimal(0) if locked is None else locked.as_decimal(),
                    currency=currency.code,
                    captured_ns=self.clock.timestamp_ns(),
                ),
            )

    def _position_snapshot(self, position: Position, row_id: UUID) -> None:
        quote = self.cache.quote_tick(position.instrument_id)
        instrument = self.cache.instrument(position.instrument_id)
        if quote is None or instrument is None:
            return  # no mark yet; the next timer tick tries again
        mark = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        pnl = position.unrealized_pnl(instrument.make_price(mark))
        currency: Currency = position.settlement_currency
        self._put(
            WriteCall.RECORD_POSITION_SNAPSHOT,
            PositionSnapshotRow(
                env=self._env,
                position_id=row_id,
                quantity=position.signed_decimal_qty(),
                mark_px=mark,
                unrealized_pnl=pnl.as_decimal(),
                currency=currency.code,
                captured_ns=self.clock.timestamp_ns(),
            ),
        )

    def _put(self, call: WriteCall, row: QueuedRow) -> None:
        self._record(WriteItem(call, row))
