"""``LabRecorder`` and ``UnitRows``: a unit's rows, built exactly as the paper loop builds them (spec 0008 AC-7).

``LabRecorder`` is the trader's ``RecordingActor`` with only the order and position
subscriptions (no account snapshots, no timer, no instrument row: the replay writes that once,
AC-11). It goes through the same fact builders and pure mappers, and hands every row to
``UnitRows`` instead of a writer thread. It also notes the quoted spread when each order is
created, for the loser check (AC-14), and on Tardis runs samples the mid and the netted position
at each funding boundary inside the segment, for the parent's funding payments (AC-9).

``SegmentEndCloser`` is the runner's close when a segment's data ends (AC-8).

``UnitRows`` collapses what the writer would upsert: the newest row per order and per position,
one fill per trade (the one linked to its position, when it was), one event per event id. Every
row keeps the order it was first seen in, the "sequence within the unit" of AC-12.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import InstrumentId

from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import FundingSample, PositionKey
from trading_core.lab.markouts import FillMarkouts, MarkoutTracker
from trading_core.node.funding import FundingLedger
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import Env, Intent, OrderSide, RiskVerdict
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import (
    DecisionRow,
    FillRow,
    JsonObject,
    OrderEventRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
)
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import EntryBlocks

# The decision reason of the runner's close when a segment's data ends (AC-8).
SEGMENT_END: Final = "segment_end"
_UNUSED_STALE_QUOTE_S: Final = Decimal(10)


def _mid(quote: QuoteTick) -> Decimal:
    mid: Decimal = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
    return mid


# Rows a replay never writes: the instrument goes in once before the bundle, snapshots are paper only.
_IGNORED = frozenset(
    {WriteCall.UPSERT_INSTRUMENT, WriteCall.RECORD_POSITION_SNAPSHOT, WriteCall.RECORD_ACCOUNT_SNAPSHOT}
)


@dataclass
class UnitRows:
    """Collects one unit's rows from the strategy and the recorder; called like the writer's ``put``."""

    decisions: dict[object, tuple[int, DecisionRow]] = field(default_factory=dict)
    risk_events: dict[str, tuple[int, RiskEventRow]] = field(default_factory=dict)
    orders: dict[str, tuple[int, OrderRow]] = field(default_factory=dict)
    order_events: dict[tuple[str, object], tuple[int, OrderEventRow]] = field(default_factory=dict)
    fills: dict[str, tuple[int, FillRow]] = field(default_factory=dict)
    positions: dict[tuple[str, int], tuple[int, PositionRow]] = field(default_factory=dict)
    # The quoted spread (best ask - best bid, USD) when each order was created, by client order id.
    spreads: dict[str, Decimal] = field(default_factory=dict)
    # The mid and netted position at each funding boundary inside the segment (AC-9).
    funding_samples: list[FundingSample] = field(default_factory=list)
    _seq: int = 0

    def next_seq(self) -> int:
        """The next "sequence within the unit" (AC-12), for a row added outside ``__call__``."""
        self._seq += 1
        return self._seq

    def __call__(self, item: WriteItem) -> None:
        """Take one queued write, as ``RecordingWriter.put`` would."""
        row = item.row
        match item.call:
            case WriteCall.RECORD_DECISION if isinstance(row, DecisionRow):
                self.decisions.setdefault(row.id, (self.next_seq(), row))
            case WriteCall.RECORD_RISK_EVENT if isinstance(row, RiskEventRow):
                self.risk_events.setdefault(row.event_key, (self.next_seq(), row))
            case WriteCall.UPSERT_ORDER if isinstance(row, OrderRow):
                seen = self.orders.get(row.client_order_id)
                if seen is None:
                    self.orders[row.client_order_id] = (self.next_seq(), row)
                elif row.last_event_ns >= seen[1].last_event_ns:
                    self.orders[row.client_order_id] = (seen[0], row)
            case WriteCall.RECORD_ORDER_EVENT if isinstance(row, OrderEventRow):
                self.order_events.setdefault((row.client_order_id, row.event_id), (self.next_seq(), row))
            case WriteCall.RECORD_FILL if isinstance(row, FillRow):
                seen_fill = self.fills.get(row.trade_id)
                if seen_fill is None:
                    self.fills[row.trade_id] = (self.next_seq(), row)
                elif seen_fill[1].position_id is None and row.position_id is not None:
                    self.fills[row.trade_id] = (seen_fill[0], row)
            case WriteCall.UPSERT_POSITION if isinstance(row, PositionRow):
                key = (row.position_ref, row.opened_ns)
                seen_position = self.positions.get(key)
                if seen_position is None:
                    self.positions[key] = (self.next_seq(), row)
                elif row.last_event_ns >= seen_position[1].last_event_ns:
                    self.positions[key] = (seen_position[0], row)
            case call if call in _IGNORED:
                pass
            case call:
                raise TypeError(f"unexpected write {call} with {type(row).__name__}")


class LabRecorder(RecordingActor):
    """The paper loop's recording actor, minus snapshots, plus the spread at order creation and
    the funding samples at ``funding_boundaries`` (none when the run applies no funding)."""

    def __init__(
        self,
        *,
        env: Env,
        run_id: UUID,
        instrument_id: InstrumentId,
        rows: UnitRows,
        blocks: EntryBlocks,
        risk: RiskLimits,
        funding_boundaries: Sequence[int] = (),
    ) -> None:
        # The stale quote age and the ledger feed only the snapshots and the booked funding, which
        # the lab never starts (it samples funding at ``funding_boundaries`` instead).
        super().__init__(
            env=env,
            run_id=run_id,
            instrument_id=instrument_id,
            record=rows,
            blocks=blocks,
            maintenance_margin_rate=risk.maintenance_margin_rate,
            min_liq_distance_pct=risk.min_liq_distance_pct,
            stale_quote_s=_UNUSED_STALE_QUOTE_S,
            ledger=FundingLedger(),
        )
        self._rows = rows
        self._lab_instrument_id = instrument_id
        self._funding_boundaries = tuple(funding_boundaries)
        self._markouts = MarkoutTracker()

    def on_start(self) -> None:
        """Subscribe to every order and position event (the instrument and snapshots are not replay
        rows) and to quotes (for markouts), and set one time alert per funding boundary."""
        self.msgbus.subscribe(topic="events.order.*", handler=self._guarded(self._on_lab_order_event))
        self.msgbus.subscribe(topic="events.position.*", handler=self._guarded(self._on_position_event))
        self.subscribe_quote_ticks(self._lab_instrument_id)
        sample = self._guarded(self._on_funding_boundary)
        for boundary in self._funding_boundaries:
            self.clock.set_time_alert_ns(name=f"funding-{boundary}", alert_time_ns=boundary, callback=sample)

    def on_stop(self) -> None:
        """Nothing to stop: the lab starts no timer."""

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Feed the mid to the markouts, only while a fill waits for one (most quotes cost nothing)."""
        if self._markouts.waiting:
            self._markouts.on_mid(tick.ts_init, _mid(tick))

    def markouts(self, last_ts_ns: int) -> list[FillMarkouts]:
        """Every fill's markouts, once the segment's data has ended at ``last_ts_ns`` (AC-10)."""
        return self._markouts.finish(last_ts_ns)

    def _on_lab_order_event(self, event: object) -> None:
        if isinstance(event, OrderInitialized):
            quote = self.cache.quote_tick(self._lab_instrument_id)
            if quote is not None:
                spread = quote.ask_price.as_decimal() - quote.bid_price.as_decimal()
                self._rows.spreads.setdefault(event.client_order_id.value, spread)
        elif isinstance(event, OrderFilled):
            quote = self.cache.quote_tick(self._lab_instrument_id)
            if quote is not None:
                self._markouts.on_mid(quote.ts_init, _mid(quote))  # the mid the next quote will replace
            self._markouts.on_fill(
                event.trade_id.value,
                buy=event.order_side == NautilusOrderSide.BUY,
                fill_px=event.last_px.as_decimal(),
                fill_ns=event.ts_event,
            )
        self._on_order_event(event)

    def _on_funding_boundary(self, event: object) -> None:
        """The mid (last quote at or before now) and the open position, if any, at a funding boundary."""
        if not isinstance(event, TimeEvent):
            raise TypeError(f"unexpected funding alert {type(event).__name__}")
        quote = self.cache.quote_tick(self._lab_instrument_id)
        mid = Decimal(0) if quote is None else _mid(quote)
        position = next(iter(self.cache.positions_open(instrument_id=self._lab_instrument_id)), None)
        if position is None:
            self._rows.funding_samples.append(FundingSample(event.ts_event, mid, Decimal(0), None))
            return
        if quote is None:
            raise RecordingFailedError(f"an open position at {event.ts_event} but no quote to price funding")
        key = PositionKey(position.id.value, position.ts_opened)
        self._rows.funding_samples.append(FundingSample(event.ts_event, mid, position.signed_decimal_qty(), key))


class SegmentEndCloser(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """The lab runner's segment end (AC-8): when the data ends it stops new entries, cancels the
    strategy's orders, and closes any open position with a reduce only market order.

    It acts at ``close_at_ns`` (the last delta's ``ts_init``), so the cancel and the close reach
    the venue before the stream's final batch and fill against the last book. The close goes out
    through the strategy's own order factory and ``submit_order``, because a netting position
    belongs to the strategy that opened it; the strategy itself is not changed. A ``decisions``
    row with intent ``exit`` and reason ``segment_end`` is recorded first and tags the order.
    """

    def __init__(
        self,
        *,
        strategy: KfsStrategy,
        gate: PauseGate,
        rows: UnitRows,
        env: Env,
        run_id: UUID,
        instrument_id: InstrumentId,
        close_at_ns: int,
    ) -> None:
        super().__init__()
        self._strategy = strategy
        self._gate = gate
        self._rows = rows
        self._env = env
        self._run_id = run_id
        self._close_instrument_id = instrument_id
        self._close_at_ns = close_at_ns
        # The close order and its decision, once sent; the unit settles any rest the book left (AC-8).
        self.close_id: str | None = None
        self.close_decision: UUID | None = None

    def on_start(self) -> None:
        """Set the one alert at the end of the data."""
        self.clock.set_time_alert_ns(name="segment-end", alert_time_ns=self._close_at_ns, callback=self._on_end)

    def _on_end(self, event: object) -> None:
        self._gate.pause_in_memory()  # no new entry from here on; exits are reduce only and still pass
        self._strategy.cancel_all_orders(self._close_instrument_id)
        position = next(iter(self.cache.positions_open(instrument_id=self._close_instrument_id)), None)
        if position is None:
            return
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        decision = self.record_close_decision(closing, position.quantity.as_decimal(), self.clock.timestamp_ns())
        order = self._strategy.order_factory.market(
            self._close_instrument_id,
            NautilusOrderSide.SELL if closing == OrderSide.SELL else NautilusOrderSide.BUY,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=[f"{DECISION_TAG}{decision}"],
        )
        self.close_id = order.client_order_id.value
        self._strategy.submit_order(order)

    def record_close_decision(self, side: OrderSide, quantity: Decimal, decided_ns: int) -> UUID:
        """Record the ``segment_end`` exit decision and return its id."""
        quote = self.cache.quote_tick(self._close_instrument_id)
        inputs: JsonObject = {"position_qty": quantity}
        if quote is not None:
            inputs = {**inputs, "mid": _mid(quote)}
        row = DecisionRow(
            env=self._env,
            run_id=self._run_id,
            instrument_id=self._close_instrument_id.value,
            intent=Intent.EXIT,
            side=side,
            reason=SEGMENT_END,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED,
            decided_ns=decided_ns,
        )
        self._rows(WriteItem(WriteCall.RECORD_DECISION, row))
        self.close_decision = row.id
        return row.id
