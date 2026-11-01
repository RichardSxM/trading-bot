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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.position import Position

from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import FundingSample, PositionKey
from trading_core.lab.markouts import FillMarkouts, MarkoutTracker
from trading_core.node import guard
from trading_core.node.funding import FundingLedger
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import Env, Intent, OrderSide, RiskVerdict
from trading_core.recording.mappers import DECISION_TAG, weighted_avg
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
from trading_core.risk.liquidation import liquidation_price, maintenance_rate
from trading_core.strategy.blocks import EntryBlocks

# The decision reason of the runner's close when a segment's data ends (AC-8).
SEGMENT_END: Final = "segment_end"
# The same close at the end of a candle backtest's range (spec 0014 AC-14).
RANGE_END: Final = "range_end"
# The liquidation guard's close when the quote crosses the liquidation price, and its ruin close (AC-14).
LIQUIDATED: Final = "liquidated"
RUINED: Final = "ruined"
USD: Final = Currency.from_str("USD")
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
    # The mid and netted position at each funding boundary inside the segment (AC-9), per coin by
    # instrument id (spec 0022 AC-8: the key is the sample's coin).
    funding_samples: dict[str, list[FundingSample]] = field(default_factory=dict)
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
    the funding samples at ``funding_boundaries`` (none when the run applies no funding).

    One recorder serves every coin of a unit (spec 0022 AC-4): the order and position rows take their
    coin from each event, and the quotes, markouts, bar counts, and funding samples are kept per coin.
    """

    def __init__(
        self,
        *,
        env: Env,
        run_id: UUID,
        instrument_ids: Sequence[InstrumentId],
        rows: UnitRows,
        blocks: EntryBlocks,
        risk: RiskLimits,
        funding_boundaries: Sequence[int] = (),
        bar_types: Sequence[BarType] = (),
        segment_start_ns: int | None = None,
    ) -> None:
        # The stale quote age and the ledger feed only the snapshots and the booked funding, which
        # the lab never starts (it samples funding at ``funding_boundaries`` instead), and the lab's
        # own on_start replaces the base one, so the coins given here feed nothing at replay time.
        super().__init__(
            env=env,
            run_id=run_id,
            instrument_ids=instrument_ids,
            record=rows,
            blocks=blocks,
            maintenance_margin_rate=risk.maintenance_margin_rate,
            min_liq_distance_pct=risk.min_liq_distance_pct,
            stale_quote_s=_UNUSED_STALE_QUOTE_S,
            ledger=FundingLedger(),
        )
        self._rows = rows
        self._lab_instrument_ids = tuple(instrument_ids)
        self._funding_boundaries = tuple(funding_boundaries)
        self._markouts = {i: MarkoutTracker() for i in self._lab_instrument_ids}
        for instrument_id in self._lab_instrument_ids:
            rows.funding_samples.setdefault(instrument_id.value, [])
        # Each strategy's own internal bar type: subscribing to the same one adds no second aggregator
        # (spec 0014 gate G-3b), and its volume 0 bars are the report's ``zero_volume_bars`` (AC-16).
        self._bar_types = tuple(bar_types)
        self.zero_volume_by_coin = {b.instrument_id.value: 0 for b in self._bar_types}
        # A trend run's pre roll bars (closed at or before the segment start) are not the run's (spec 0027.1 AC-10a).
        self._bars_after_ns = segment_start_ns

    @property
    def zero_volume_bars(self) -> int:
        """The strategies' bars with no volume, over every coin."""
        return sum(self.zero_volume_by_coin.values())

    def on_start(self) -> None:
        """Subscribe to every order and position event (the instrument and snapshots are not replay
        rows) and to quotes (for markouts), and set one time alert per funding boundary."""
        self.msgbus.subscribe(topic="events.order.*", handler=self._guarded(self._on_lab_order_event))
        self.msgbus.subscribe(topic="events.position.*", handler=self._guarded(self._on_position_event))
        for instrument_id in self._lab_instrument_ids:
            self.subscribe_quote_ticks(instrument_id)
        for bar_type in self._bar_types:
            self.subscribe_bars(bar_type)
        sample = self._guarded(self._on_funding_boundary)
        for boundary in self._funding_boundaries:
            self.clock.set_time_alert_ns(name=f"funding-{boundary}", alert_time_ns=boundary, callback=sample)

    def on_stop(self) -> None:
        """Nothing to stop: the lab starts no timer."""

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Feed the mid to the coin's markouts, only while a fill waits for one (most quotes cost nothing)."""
        markouts = self._markouts[tick.instrument_id]
        if markouts.waiting:
            markouts.on_mid(tick.ts_init, _mid(tick))

    def on_bar(self, bar: Bar) -> None:
        """Count the strategies' bars with no volume (quiet hours, or hours with no candles at all)."""
        if self._bars_after_ns is not None and bar.ts_event <= self._bars_after_ns:
            return
        if bar.bar_type in self._bar_types and bar.volume.as_decimal() == 0:
            self.zero_volume_by_coin[bar.bar_type.instrument_id.value] += 1

    def markouts(self, last_ts_ns: int) -> list[FillMarkouts]:
        """Every fill's markouts, once the segment's data has ended at ``last_ts_ns`` (AC-10), coin by coin."""
        return [m for i in self._lab_instrument_ids for m in self._markouts[i].finish(last_ts_ns)]

    def _on_lab_order_event(self, event: object) -> None:
        if isinstance(event, OrderInitialized):
            quote = self.cache.quote_tick(event.instrument_id)
            if quote is not None:
                spread = quote.ask_price.as_decimal() - quote.bid_price.as_decimal()
                self._rows.spreads.setdefault(event.client_order_id.value, spread)
        elif isinstance(event, OrderFilled):
            markouts = self._markouts[event.instrument_id]
            quote = self.cache.quote_tick(event.instrument_id)
            if quote is not None:
                markouts.on_mid(quote.ts_init, _mid(quote))  # the mid the next quote will replace
            markouts.on_fill(
                event.trade_id.value,
                buy=event.order_side == NautilusOrderSide.BUY,
                fill_px=event.last_px.as_decimal(),
                fill_ns=event.ts_event,
            )
        self._on_order_event(event)

    def _on_funding_boundary(self, event: object) -> None:
        """Each coin's mid (last quote at or before now) and open position, if any, at a funding boundary,
        appended under that coin (spec 0022 AC-8)."""
        if not isinstance(event, TimeEvent):
            raise TypeError(f"unexpected funding alert {type(event).__name__}")
        for instrument_id in self._lab_instrument_ids:
            samples = self._rows.funding_samples[instrument_id.value]
            quote = self.cache.quote_tick(instrument_id)
            mid = Decimal(0) if quote is None else _mid(quote)
            position = next(iter(self.cache.positions_open(instrument_id=instrument_id)), None)
            if position is None:
                samples.append(FundingSample(event.ts_event, mid, Decimal(0), None))
                continue
            if quote is None:
                raise RecordingFailedError(
                    f"an open {instrument_id} position at {event.ts_event} but no quote to price funding"
                )
            key = PositionKey(position.id.value, position.ts_opened)
            samples.append(FundingSample(event.ts_event, mid, position.signed_decimal_qty(), key))


def closing_side(position: Position) -> OrderSide:
    """The side of the order that closes ``position``."""
    return OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY


def submit_market_close(strategy: KfsStrategy, position: Position, decision: UUID) -> str:
    """Close ``position`` with a reduce only market order through the strategy's own order factory and
    ``submit_order`` (a netting position belongs to the strategy that opened it), tagged with
    ``decision``; return the order's client order id."""
    order = strategy.order_factory.market(
        position.instrument_id,
        NautilusOrderSide.SELL if position.side == NautilusPositionSide.LONG else NautilusOrderSide.BUY,
        position.quantity,
        time_in_force=TimeInForce.GTC,
        reduce_only=True,
        tags=[f"{DECISION_TAG}{decision}"],
    )
    strategy.submit_order(order)
    client_order_id: str = order.client_order_id.value
    return client_order_id


class SegmentEndCloser(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """The lab runner's segment end (AC-8): when the data ends it stops new entries, cancels the
    strategy's orders, and closes any open position with a reduce only market order.

    It acts at ``close_at_ns`` (the last delta's ``ts_init``, or a candle range's last point + 1 ms),
    so the cancel and the close reach the venue before the stream's final batch or quote and fill
    against it. The close goes out
    through the strategy's own order factory and ``submit_order``, because a netting position
    belongs to the strategy that opened it; the strategy itself is not changed. A ``decisions``
    row with intent ``exit`` and ``reason`` (``segment_end``, or ``range_end`` in the candle lab) is
    recorded first and tags the order.
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
        reason: str = SEGMENT_END,
        component_id: str | None = None,
    ) -> None:
        # A multi coin unit names each coin's closer, since component ids are unique in an engine.
        super().__init__(None if component_id is None else ActorConfig(component_id=component_id))
        self._strategy = strategy
        self._gate = gate
        self._rows = rows
        self._env = env
        self._run_id = run_id
        self._close_instrument_id = instrument_id
        self._close_at_ns = close_at_ns
        self._reason = reason
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
        decision = self.record_close_decision(
            closing_side(position), position.quantity.as_decimal(), self.clock.timestamp_ns()
        )
        self.close_id = submit_market_close(self._strategy, position, decision)

    def record_close_decision(self, side: OrderSide, quantity: Decimal, decided_ns: int) -> UUID:
        """Record the exit decision with this closer's reason and return its id."""
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
            reason=self._reason,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED,
            decided_ns=decided_ns,
        )
        self._rows(WriteItem(WriteCall.RECORD_DECISION, row))
        self.close_decision = row.id
        return row.id


@dataclass
class RuinFlag:
    """When ``LiquidationGuard`` found account equity at zero or below (AC-14), else None."""

    at_ns: int | None = None


class SharedRuin:
    """The ruin stop every coin's ``LiquidationGuard`` calls (spec 0022 AC-4b): it closes each coin's open
    position at its last quote, in sorted coin order, sets the ``RuinFlag``, and only then calls ``stop``
    (which ends the variant early), so the variant reports ruin instead of failing on an open position."""

    def __init__(self, ruin: RuinFlag, stop: Callable[[], None]) -> None:
        self.guards: list[LiquidationGuard] = []
        self._ruin = ruin
        self._stop = stop

    def __call__(self, ts_ns: int, equity: Decimal) -> None:
        """Close every coin, then end the variant at ``ts_ns``."""
        for coin_guard in self.guards:
            coin_guard.close_for_ruin(equity)
        self._ruin.at_ns = ts_ns
        self._stop()


class LiquidationGuard(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """The candle lab's forced liquidation and ruin stop (spec 0014 AC-14).

    On every quote while a position is open it compares the side's quote with the position's
    liquidation price (``risk.liquidation.liquidation_price`` with the approved entry decision's
    ``figures.equity``, no other notional, the entry's taker fee in USD, and the run's
    maintenance rate). A long's bid at or below it, or a short's ask at or above it, records an
    ``exit`` decision with reason ``liquidated`` and closes at market on that quote through the
    strategy's own order factory, as ``SegmentEndCloser`` does. Funding is booked after the run, so
    it does not move the price. When the account balance plus open P&L (over every coin) is zero or
    below, it calls ``on_ruin`` (a ``SharedRuin``), which closes what is still open on every coin
    (reason ``ruined``), sets the ``RuinFlag``, and ends the variant early.
    """

    def __init__(
        self,
        *,
        strategy: KfsStrategy,
        rows: UnitRows,
        env: Env,
        run_id: UUID,
        instrument_id: InstrumentId,
        taker_fee: Decimal,
        risk: RiskLimits,
        ruin: RuinFlag,
        on_ruin: Callable[[int, Decimal], None],
        component_id: str | None = None,
    ) -> None:
        super().__init__(None if component_id is None else ActorConfig(component_id=component_id))
        self._on_ruin = on_ruin
        self._strategy = strategy
        self._rows = rows
        self._env = env
        self._run_id = run_id
        self._guard_instrument_id = instrument_id
        self._taker_fee = taker_fee
        self._risk = risk
        self._ruin = ruin
        # The liquidation price per opened position ((position id, opened ns) under netting).
        self._liq: dict[tuple[str, int], Decimal | None] = {}
        self._close_id: str | None = None

    def on_start(self) -> None:
        """Watch every quote."""
        self.subscribe_quote_ticks(self._guard_instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Liquidate a position whose quote crossed its liquidation price; stop the run on ruin."""
        if self._ruin.at_ns is not None:
            return
        position = next(iter(self.cache.positions_open(instrument_id=self._guard_instrument_id)), None)
        if position is not None and not self._closing():
            liq_px = self.liquidation_px(position)
            long = position.side == NautilusPositionSide.LONG
            bid, ask = tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
            if liq_px is not None and ((long and bid <= liq_px) or (not long and ask >= liq_px)):
                self._close(position, LIQUIDATED, {"liq_px": liq_px, "bid": bid, "ask": ask})
                position = None
        equity = self._equity()
        if equity <= 0:
            self.log.warning(f"account equity reached zero at {tick.ts_event}; ending the variant")
            self._on_ruin(tick.ts_event, equity)

    def close_for_ruin(self, equity: Decimal) -> None:
        """Close this coin's open position, if any and not already closing, with reason ``ruined``."""
        position = next(iter(self.cache.positions_open(instrument_id=self._guard_instrument_id)), None)
        if position is not None and not self._closing():
            self._close(position, RUINED, {"equity": equity})

    def liquidation_px(self, position: Position) -> Decimal | None:
        """The position's liquidation price, computed once when it opened (``None``: none reachable)."""
        key = (position.id.value, position.ts_opened)
        if key not in self._liq:
            self._liq[key] = self._liquidation_px(position)
        return self._liq[key]

    def _liquidation_px(self, position: Position) -> Decimal | None:
        instrument = self.cache.instrument(self._guard_instrument_id)
        if instrument is None:
            raise LookupError(f"instrument {self._guard_instrument_id} not in the cache")
        opening = self.cache.order(position.opening_order_id)
        if opening is None:
            raise RecordingFailedError(f"position {position.id} has no opening order in the cache")
        # The entry price from the opening order's fills (Decimal; the position's own average is a float).
        entry_px = weighted_avg(
            [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in opening.events if isinstance(e, OrderFilled)]
        )
        if entry_px is None:
            raise RecordingFailedError(f"position {position.id}'s opening order has no fill")
        quantity = position.quantity.as_decimal()
        return liquidation_price(
            OrderSide.BUY if position.side == NautilusPositionSide.LONG else OrderSide.SELL,
            quantity,
            entry_px,
            self._entry_equity(position, opening.tags),
            Decimal(0),
            self._taker_fee * quantity * entry_px,
            maintenance_rate(guard.margin_maint(instrument), self._risk.maintenance_margin_rate),
        )

    def _entry_equity(self, position: Position, tags: Sequence[str] | None) -> Decimal:
        # ``figures.equity`` of the approved entry decision that tagged the opening order.
        ids = [t.removeprefix(DECISION_TAG) for t in tags or () if t.startswith(DECISION_TAG)]
        seen = self._rows.decisions.get(UUID(ids[0])) if ids else None
        if seen is None or "equity" not in seen[1].inputs:
            raise RecordingFailedError(f"position {position.id} has no recorded entry equity")
        return Decimal(str(seen[1].inputs["equity"]))

    def _equity(self) -> Decimal:
        account = self.portfolio.account(self._guard_instrument_id.venue)
        if account is None:
            raise LookupError(f"no account for {self._guard_instrument_id.venue}")
        balance = account.balance_total(USD)
        return (Decimal(0) if balance is None else balance.as_decimal()) + guard.unrealized(self.cache)

    def _closing(self) -> bool:
        order = None if self._close_id is None else self.cache.order(ClientOrderId(self._close_id))
        return order is not None and not order.is_closed

    def _close(self, position: Position, reason: str, extra: JsonObject) -> None:
        # No cancel first: the strategy cancels its own stop once the position closes, so it never
        # reads a stop it did not cancel as a failed one.
        quote = self.cache.quote_tick(self._guard_instrument_id)
        inputs: JsonObject = {"position_qty": position.quantity.as_decimal(), **extra}
        if quote is not None:
            inputs = {**inputs, "mid": _mid(quote)}
        row = DecisionRow(
            env=self._env,
            run_id=self._run_id,
            instrument_id=self._guard_instrument_id.value,
            intent=Intent.EXIT,
            side=closing_side(position),
            reason=reason,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED,
            decided_ns=self.clock.timestamp_ns(),
        )
        self._rows(WriteItem(WriteCall.RECORD_DECISION, row))
        self._close_id = submit_market_close(self._strategy, position, row.id)
