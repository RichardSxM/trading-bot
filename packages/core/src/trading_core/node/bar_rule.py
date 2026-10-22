"""``BarRuleStrategy``: the shared base of the hour level candidates (spec 0018 AC-1 to AC-5, AC-8).

It aggregates its own ``bar_spec`` bars from trades (``<instrument>-<bar_spec>-INTERNAL``), so it runs
unchanged in the candle lab, the order book replay, and live. The order lifecycle is ``macd_baseline``'s,
copied unchanged (phases, the stale quote block, the stop accept timer and ``STOP_NOT_ACCEPTED`` block,
the exit retry, the kill switch ``flatten``, the reset on any close). What differs is the decision: a pure
``BarRule`` (``trading_core.strategy.bar_rule``) gives each entry with a structural stop, the base floors
that stop at ``atr_floor_mult`` ATRs from the mid and sizes the entry from it at ``risk_per_trade`` of
equity (spec 0011), places the protective stop at that absolute price, and exits at market on the rule's
exit or after ``time_stop_bars`` bar closes.

On each bar close, in this order: the base's ATR and then the rule are updated (always); in a position
(``ENTERING`` or ``PROTECTED``) ``bars_held`` goes up and the exit step runs; while flat with no exit sent
at this bar the entry step runs; otherwise a signal the rule gives is consumed and dropped. At most one
entry or exit is sent per bar close, and nothing is decided before the rule and the ATR are ready.
Frozen after the first study (AC-7): a change goes into a new ``bar_rule_v2`` module.
"""

import functools
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick
from nautilus_trader.model.enums import BarAggregation, PriceType, TimeInForce, TriggerType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import OrderStatus as NautilusOrderStatus
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
    PositionClosed,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.node import guard
from trading_core.node.guard import GuardedConfig
from trading_core.node.stale import StaleQuoteBlock
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import (
    Env,
    Intent,
    OrderOrigin,
    OrderSide,
    RiskEventKind,
    RiskVerdict,
    Severity,
)
from trading_core.recording.mappers import DECISION_TAG, HALT_TAG, ORIGIN_TAG, PARENT_TAG, weighted_avg
from trading_core.recording.rows import DecisionRow, JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import EntryRequest, InvalidRiskInput, check_entry, refusal_severity
from trading_core.risk.funding import funding_periods
from trading_core.strategy.bar_indicators import Atr
from trading_core.strategy.bar_rule import (
    BadSignalError,
    BarInput,
    BarRule,
    EnterSignal,
    Side,
    TradeView,
    floored_stop,
)
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

TIME_STOP: Final = "time_stop"
STOP_THROUGH: Final = "stop_through"
BAD_SIGNAL: Final = "bad_signal"
NS_PER_S: Final = 1_000_000_000
STOP_ACCEPT_TIMEOUT_NS: Final = 5 * NS_PER_S
EXIT_RETRY_NS: Final = NS_PER_S
USD: Final = Currency.from_str("USD")

_STALE = "bar_rule_stale_check"
_STOP_ACCEPT = "bar_rule_stop_accept"
_EXIT_RETRY = "bar_rule_exit_retry"
_TIMERS: Final = (_STOP_ACCEPT, _EXIT_RETRY)
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}
_BAR_STEPS: Final = {
    BarAggregation.MINUTE: timedelta(minutes=1),
    BarAggregation.HOUR: timedelta(hours=1),
    BarAggregation.DAY: timedelta(days=1),
}

type Record = Callable[[WriteItem], None]


def bar_interval_ns(bar_spec: str) -> int:
    """The length of one ``bar_spec`` bar in nanoseconds (spec 0014 AC-11).

    Raises ``ValueError`` unless it is a ``MINUTE``, ``HOUR``, or ``DAY`` step of at least 1 with ``LAST`` prices.
    """
    try:
        spec = BarSpecification.from_str(bar_spec)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"bar_spec {bar_spec!r} is not a bar specification like 1-HOUR-LAST") from exc
    unit = _BAR_STEPS.get(spec.aggregation)
    if unit is None or spec.price_type != PriceType.LAST or spec.step < 1:
        raise ValueError(f"bar_spec {bar_spec!r}: only MINUTE, HOUR, or DAY steps with LAST prices are accepted")
    return int(spec.step) * (unit // timedelta(microseconds=1)) * 1_000


class Phase(StrEnum):
    """Where the round trip is."""

    FLAT = "flat"
    ENTERING = "entering"
    PROTECTED = "protected"
    EXITING = "exiting"


class BarRuleConfig(GuardedConfig, frozen=True, kw_only=True):
    """The settings every candidate inherits (spec 0018 *Base config*), the run's risk caps, and its run.

    Bad values are refused here with ``ValueError`` (AC-8); a candidate's config adds its own checks in
    ``__post_init__`` after calling this one.
    """

    env: str
    run_id: str
    time_stop_bars: int
    bar_spec: str = "1-HOUR-LAST"
    atr_period: int = 14
    atr_floor_mult: Decimal = Decimal(1)
    use_market_context: bool = True
    stale_quote_s: Decimal = Decimal(10)
    stale_check_s: int = 1

    def __post_init__(self) -> None:
        """Refuse a bad ``bar_spec``, ``atr_period``, ``atr_floor_mult``, or ``time_stop_bars``."""
        bar_interval_ns(self.bar_spec)
        if self.atr_period < 2:
            raise ValueError(f"atr_period must be at least 2, got {self.atr_period}")
        if self.atr_floor_mult <= 0:
            raise ValueError(f"atr_floor_mult must be above 0, got {self.atr_floor_mult}")
        if self.time_stop_bars < 1:
            raise ValueError(f"time_stop_bars must be at least 1, got {self.time_stop_bars}")

    @property
    def bar_interval_ns(self) -> int:
        """One bar's length in nanoseconds, from ``bar_spec``."""
        return bar_interval_ns(self.bar_spec)


def _side_of(position: Position) -> Side:
    return Side.LONG if position.side == NautilusPositionSide.LONG else Side.SHORT


def _order_side(side: Side) -> OrderSide:
    return OrderSide.BUY if side == Side.LONG else OrderSide.SELL


class BarRuleStrategy(KfsStrategy, StaleQuoteBlock):
    """Trades a candidate's ``BarRule`` with a floored, risk sized stop (see the module docstring).

    A candidate's node module subclasses it only to build its rule from its own config.
    """

    def __init__(
        self, config: BarRuleConfig, gate: PauseGate, blocks: EntryBlocks, record: Record, rule: BarRule
    ) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(f"{config.instrument_id}-{config.bar_spec}-INTERNAL")
        self._hold_ns = config.time_stop_bars * config.bar_interval_ns
        self._limits = config.risk_limits()
        self._atr = Atr(config.atr_period)
        self.rule = rule
        self._last_bar: BarInput | None = None
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self.entry_decision: UUID | None = None
        self._exit_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._stop_id: ClientOrderId | None = None
        self._exit_id: ClientOrderId | None = None
        self._kill_switch_tags: list[str] | None = None
        self.bars_held = 0
        self._side: Side | None = None
        self._stop_px: Decimal | None = None
        self._entry_px: Decimal | None = None

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to quotes and to the strategy's own bars, then arm the stale quote check."""
        super().on_start()
        self.subscribe_quote_ticks(self._instrument_id)
        self.subscribe_bars(self.bar_type)
        self.start_stale_check(
            timer=_STALE,
            instrument_id=self._instrument_id,
            run_id=self._run_id,
            blocks=self._blocks,
            stale_quote_s=self._cfg.stale_quote_s,
            stale_check_s=self._cfg.stale_check_s,
        )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """The mid is read from the cache at the bar close; nothing to do here."""

    def flatten(self, halt_id: UUID) -> None:
        """Cancel the entry and close the position at market, tagged for the kill switch (spec 0010 AC-1).

        Idempotent: a close already sent is not sent again. The stop rests until the position closes.
        """
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        if entry is not None and entry.is_open:
            self.cancel_order(entry)
        self._kill_switch_tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        if self._position() is None or self._exit_id is not None:
            return
        self.phase = Phase.EXITING
        self._submit_exit()

    # Bars -----------------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Update the ATR and the rule with every bar close, then run the exit or the entry step (AC-1)."""
        if bar.bar_type != self.bar_type:
            return
        try:
            closed = BarInput(
                ts_ns=bar.ts_event,
                open=bar.open.as_decimal(),
                high=bar.high.as_decimal(),
                low=bar.low.as_decimal(),
                close=bar.close.as_decimal(),
                volume=bar.volume.as_decimal(),
            )
            atr = self._atr.update(closed.high, closed.low, closed.close)
            self.rule.update(closed, atr)
            self._last_bar = closed
            if atr is None or not self.rule.ready():
                return
            position = self._position()
            exited = False
            if self.phase in (Phase.ENTERING, Phase.PROTECTED) and position is not None:
                self.bars_held += 1
                exited = self._exit_step(closed, atr, position)
            if self.phase == Phase.FLAT and position is None and not exited:
                self._entry_step(closed, atr)
            else:
                self._drop_signal()
        except Exception:
            # Fail closed: no new order this bar; a position keeps its stop.
            log.exception("bar decision failed", extra={"fields": {"code": "bar_decision_failed"}})

    def _drop_signal(self) -> None:
        # A trigger while in a position (or exiting) is consumed, never saved for later.
        if self.rule.entry() is not None:
            self.rule.consumed()

    def _bar_inputs(self, bar: BarInput, atr: Decimal) -> JsonObject:
        return {"bar_close": bar.close, "atr": atr, **self.rule.values()}

    def _entry_step(self, bar: BarInput, atr: Decimal) -> None:
        signal = self.rule.entry()
        if signal is None:
            return
        self.rule.consumed()  # one bar event: whether approved, refused, or skipped (AC-2)
        self._decide_entry(bar, atr, signal)

    def _decide_entry(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        instrument = self._instrument()
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        now = self.clock.timestamp_ns()
        side = _order_side(signal.side)
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "mid": mid,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
            "structural_stop": signal.structural_stop,
        }
        tick = instrument.price_increment.as_decimal()
        try:
            stop, floor_applied = floored_stop(
                signal.side, signal.structural_stop, mid, atr, self._cfg.atr_floor_mult, tick
            )
        except BadSignalError:
            log.warning(
                "structural stop on the wrong side of the mid",
                extra={"fields": {"code": "bar_rule_bad_signal", "reason": signal.reason}},
            )
            self._record_decision(uuid.uuid7(), Intent.ENTER, side, signal.reason, inputs, False, BAD_SIGNAL, now)
            return
        try:
            request = EntryRequest(
                side=side,
                mid=mid,
                equity=self._equity(),
                open_notional=guard.open_notional(self.cache),
                size_step=instrument.size_increment.as_decimal(),
                taker_fee=self._cfg.taker_fee,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                funding_periods=funding_periods(now, self._hold_ns),
                funding_rate=self._funding_rate(),
                stop_px=stop,
                has_position=False,
                paused=self._gate.paused,
                halted=self._gate.halted,
                blocks=[str(r) for r in self._blocks.active()],
            )
            verdict = check_entry(request, self._limits)
        except InvalidRiskInput:
            log.exception("risk check could not judge the entry", extra={"fields": {"code": "risk_input_invalid"}})
            return
        decision = uuid.uuid7()
        inputs = {**inputs, "stop": stop, "floor_applied": floor_applied, **verdict.figures.as_inputs()}
        self._record_decision(
            decision, Intent.ENTER, side, signal.reason, inputs, verdict.approved, verdict.reason, now
        )
        if not verdict.approved:
            reason = verdict.reason or ""
            self._risk_event(
                f"limit_hit:{decision}",
                RiskEventKind.LIMIT_HIT,
                refusal_severity(reason),
                f"entry refused: {reason}",
                decision,
                {"reason": reason},
            )
            return
        if self._gate.halted or self._gate.paused:  # the gate again, right before submitting
            log.info("entry denied by the gate", extra={"fields": {"code": "entry_denied_gate"}})
            return
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.BUY if side == OrderSide.BUY else NautilusOrderSide.SELL,
            instrument.make_qty(verdict.quantity),
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{decision}"],
        )
        self.phase = Phase.ENTERING
        self.entry_decision = decision
        self._entry_id = order.client_order_id
        self._side = signal.side
        self._stop_px = stop
        self.bars_held = 0
        self.submit_order(order)

    def _exit_step(self, bar: BarInput, atr: Decimal, position: Position) -> bool:
        # The time stop first, then the rule's own exit (AC-4). True when an exit was sent.
        assert self._stop_px is not None  # set with the entry
        entry_px = self._entry_px if self._entry_px is not None else Decimal(str(position.avg_px_open))
        trade = TradeView(_side_of(position), entry_px, self._stop_px, self.bars_held)
        reason = TIME_STOP if self.bars_held >= self._cfg.time_stop_bars else self.rule.exit(trade)
        if reason is None:
            return False
        inputs: JsonObject = {**self._bar_inputs(bar, atr), "bars_held": self.bars_held, "stop": self._stop_px}
        self._decide_exit(position, reason, inputs)
        return True

    def _decide_exit(self, position: Position, reason: str, inputs: JsonObject) -> None:
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        inputs = {**inputs, "position_qty": position.quantity.as_decimal()}
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is not None:
            inputs = {**inputs, "mid": (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2}
        self._record_decision(decision, Intent.EXIT, closing, reason, inputs, True, None, now)
        self._exit_decision = decision
        self.phase = Phase.EXITING
        stop = self._stop_order()
        if stop is not None and stop.is_open:
            self.cancel_order(stop)
        self._submit_exit()

    # Order and position events --------------------------------------------------------------

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """An accepted stop protects the position."""
        if event.client_order_id == self._stop_id:
            self._cancel_timer(_STOP_ACCEPT)
            if self.phase == Phase.ENTERING:
                self.phase = Phase.PROTECTED

    def on_order_filled(self, event: OrderFilled) -> None:
        """The entry's complete fill rests the stop, or exits at once when the fill is already through it."""
        if event.client_order_id != self._entry_id or self._stop_id is not None:
            return
        entry = self.cache.order(event.client_order_id)
        if entry is None or entry.status != NautilusOrderStatus.FILLED or self.phase != Phase.ENTERING:
            return
        fills = [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in entry.events if isinstance(e, OrderFilled)]
        avg_px = weighted_avg(fills)
        # the entry is filled, its stop decided
        assert avg_px is not None
        assert self._stop_px is not None
        self._entry_px = avg_px
        through = avg_px <= self._stop_px if self._side == Side.LONG else avg_px >= self._stop_px
        position = self._position()
        if through and position is not None:
            inputs: JsonObject = {"entry_px": avg_px, "stop": self._stop_px, "bars_held": self.bars_held}
            if self._last_bar is not None:
                inputs = {"bar_close": self._last_bar.close, **inputs}
            self._decide_exit(position, STOP_THROUGH, inputs)
            return
        self._submit_stop(entry)

    def on_order_denied(self, event: OrderDenied) -> None:
        """A denied entry goes back to flat; a denied stop or exit: see ``_refused``."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejected entry goes back to flat; a rejected stop or exit: see ``_refused``."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """An order canceled that we did not cancel: see ``_refused``."""
        self._refused(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """Like a cancel."""
        self._refused(event.client_order_id, "expired")

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        # Our own cancels never get here as failures: the exit sets ``EXITING`` before it cancels
        # the stop, and a closed position has reset every id before its cancels arrive. A stop
        # someone else cancels while we hold a position fails closed, unless a close is under way.
        position = self._position()
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING and position is None:
            self._risk_event(
                f"order_rejected:{client_order_id}",
                RiskEventKind.ORDER_REJECTED,
                Severity.WARNING,
                f"entry {why}",
                self.entry_decision,
                {"client_order_id": str(client_order_id)},
            )
            self._back_to_flat()
        elif client_order_id == self._stop_id and position is not None and self.phase != Phase.EXITING:
            if why == "canceled" and (self._gate.halted or (self._gate.paused and self._gate.state is None)):
                # Expected: the kill switch, or the runner's close or a shutdown (an in memory pause, no
                # pause key), cancelled it and closes the position itself. An operator pause still fails closed.
                log.info("stop cancelled by the close in progress", extra={"fields": {"code": "stop_canceled_closing"}})
                self._stop_id = None
                return
            self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_id:
            self._exit_refused(why)

    def on_position_closed(self, event: PositionClosed) -> None:
        """Closed from any source: cancel what rests, reset the trade state and the rule's (AC-4)."""
        if event.instrument_id != self._instrument_id:
            return
        for order in self.cache.orders_open(strategy_id=self.id):
            self.cancel_order(order)
        self._back_to_flat()

    # Stop -----------------------------------------------------------------------------------

    def _submit_stop(self, entry: Order) -> None:
        position = self._position()
        if position is None or self._stop_px is None:
            return
        instrument = self._instrument()
        side = _side_of(position)
        order = self.order_factory.stop_market(
            self._instrument_id,
            NautilusOrderSide.SELL if side == Side.LONG else NautilusOrderSide.BUY,
            position.quantity,
            trigger_price=instrument.make_price(self._stop_px),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{self.entry_decision}", f"{PARENT_TAG}{entry.client_order_id}"],
        )
        self._stop_id = order.client_order_id
        self._alert(_STOP_ACCEPT, self.clock.timestamp_ns() + STOP_ACCEPT_TIMEOUT_NS, self._on_stop_timeout)
        self.submit_order(order)

    def _on_stop_timeout(self, event: object) -> None:
        stop = self._stop_order()
        if stop is not None and stop.status in (NautilusOrderStatus.ACCEPTED, NautilusOrderStatus.TRIGGERED):
            return
        if self._position() is None:
            return
        self._stop_failed("stop not accepted in time")

    def _stop_failed(self, why: str) -> None:
        self._cancel_timer(_STOP_ACCEPT)
        self._blocks.add(BlockReason.STOP_NOT_ACCEPTED)
        self._risk_event(
            f"stop_not_accepted:{self._stop_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.CRITICAL,
            f"{why}; closing the position",
            self.entry_decision,
            {"client_order_id": str(self._stop_id)},
        )
        self.phase = Phase.EXITING
        self._submit_exit()

    # Exit -----------------------------------------------------------------------------------

    def _submit_exit(self, event: object = None) -> None:
        position = self._position()
        if position is None:
            return
        tags = self._kill_switch_tags or [f"{DECISION_TAG}{self._exit_decision or self.entry_decision}"]
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.SELL if position.side == NautilusPositionSide.LONG else NautilusOrderSide.BUY,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,
        )
        self._exit_id = order.client_order_id
        self.submit_order(order)

    def _exit_refused(self, why: str) -> None:
        self._exit_id = None
        position = self._position()
        if position is None:
            return
        self._risk_event(
            f"exit_refused:{position.id}:{self.clock.timestamp_ns()}",
            RiskEventKind.ORDER_REJECTED,
            Severity.WARNING,
            f"exit order {why}; retrying in 1 s",
            self._exit_decision or self.entry_decision,
            {"position_id": str(position.id), "reason": why},
        )
        self._alert(_EXIT_RETRY, self.clock.timestamp_ns() + EXIT_RETRY_NS, self._submit_exit)

    # Helpers --------------------------------------------------------------------------------

    def _back_to_flat(self) -> None:
        for name in _TIMERS:
            self._cancel_timer(name)
        self._reset()
        self.rule.on_flat()

    def _stop_order(self) -> Order | None:
        return None if self._stop_id is None else self.cache.order(self._stop_id)

    def _instrument(self) -> Instrument:
        instrument = self.cache.instrument(self._instrument_id)
        if instrument is None:
            raise LookupError(f"instrument {self._instrument_id} not in the cache")
        return instrument

    def _position(self) -> Position | None:
        positions = self.cache.positions_open(instrument_id=self._instrument_id)
        return positions[0] if positions else None

    def _equity(self) -> Decimal:
        # As PlaceholderTimed: the account balance plus every open position's unrealized P&L.
        account = self.portfolio.account(self._instrument_id.venue)
        if account is None:
            raise LookupError(f"no account for {self._instrument_id.venue}")
        balance = account.balance_total(USD)
        equity = Decimal(0) if balance is None else balance.as_decimal()
        return equity + guard.unrealized(self.cache)

    def _funding_rate(self) -> Decimal | None:
        # The context's rate, or ``None`` when the context is off, the rate missing or stale.
        if not self._cfg.use_market_context:
            return None
        rate = self.market_context().funding_rate
        return None if rate is None or rate.is_stale else rate.value

    def _alert(self, name: str, at_ns: int, callback: Callable[[object], None]) -> None:
        self._cancel_timer(name)
        self.clock.set_time_alert_ns(name, at_ns, functools.partial(self._when_due, name, at_ns, callback))

    def _when_due(self, name: str, at_ns: int, callback: Callable[[object], None], event: object) -> None:
        # A live alert can wake early (the monotonic clock runs ahead of the wall clock): sleep the rest.
        if self.clock.timestamp_ns() < at_ns:
            self._alert(name, at_ns, callback)
            return
        callback(event)

    def _cancel_timer(self, name: str) -> None:
        if name in self.clock.timer_names:
            self.clock.cancel_timer(name)

    def _record_decision(
        self,
        decision: UUID,
        intent: Intent,
        side: OrderSide,
        reason: str,
        inputs: JsonObject,
        approved: bool,
        risk_reason: str | None,
        now_ns: int,
    ) -> None:
        row = DecisionRow(
            id=decision,
            env=self._env,
            run_id=self._run_id,
            instrument_id=self._cfg.instrument_id,
            intent=intent,
            side=side,
            reason=reason,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED if approved else RiskVerdict.REJECTED,
            risk_reason=risk_reason,
            decided_ns=now_ns,
        )
        self._record(WriteItem(WriteCall.RECORD_DECISION, row))

    def _risk_event(
        self,
        key: str,
        kind: RiskEventKind,
        severity: Severity,
        message: str,
        decision: UUID | None,
        details: JsonObject,
    ) -> None:
        row = RiskEventRow(
            env=self._env,
            event_key=key,
            kind=kind,
            severity=severity,
            message=message,
            details=details,
            run_id=self._run_id,
            decision_id=decision,
            occurred_ns=self.clock.timestamp_ns(),
        )
        self._record(WriteItem(WriteCall.RECORD_RISK_EVENT, row))
        log.log(_LOG_LEVELS[severity], message, extra={"fields": {"code": kind.value, "event_key": key}})
