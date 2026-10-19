"""``MacdBaseline``: the MACD crossover baseline (spec 0014 AC-13), the first hour level candidate.

It aggregates its own ``bar_spec`` bars from trades (``<instrument>-<bar_spec>-INTERNAL``), so it runs
unchanged in the candle lab, the order book replay, and live. Every bar close goes to the pure rule in
``trading_core.strategy.macd_baseline``. While flat, an entry signal sizes ``notional_usd`` at the mid
and asks ``check_entry`` with a stop ``stop_pct`` percent from the mid (every other input sourced as
``PlaceholderTimed`` sources it). The decision is recorded and an approved entry goes out at market.
Once it fills, a reduce only stop market order rests ``stop_pct`` percent from the fill price, so the
stop judged and the stop placed can differ by the fill's slippage. An exit signal cancels the stop and
closes at market. A stop that is refused or not accepted in time blocks entries and closes the position
(fail closed). A position closed from any source (its stop, its exit, the range end closer, the
liquidation guard, the kill switch) cancels what rests and resets the strategy.
"""

import functools
import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import OrderStatus as NautilusOrderStatus
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.enums import TimeInForce, TriggerType
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
    PositionSide,
    RiskEventKind,
    RiskVerdict,
    Severity,
)
from trading_core.recording.mappers import DECISION_TAG, HALT_TAG, ORIGIN_TAG, PARENT_TAG, weighted_avg
from trading_core.recording.rows import DecisionRow, JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import EntryRequest, InvalidRiskInput, check_entry, refusal_severity
from trading_core.risk.funding import funding_periods
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.buy_and_hold import hold_quantity
from trading_core.strategy.macd_baseline import MacdState, Signal
from trading_core.strategy.placeholder import stop_price

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "macd_baseline"
ENTRY_REASON: Final = "macd_entry"
EXIT_REASON: Final = "macd_exit"
NS_PER_S: Final = 1_000_000_000
STOP_ACCEPT_TIMEOUT_NS: Final = 5 * NS_PER_S
EXIT_RETRY_NS: Final = NS_PER_S
USD: Final = Currency.from_str("USD")

_STALE = "macd_baseline_stale_check"
_STOP_ACCEPT = "macd_baseline_stop_accept"
_EXIT_RETRY = "macd_baseline_exit_retry"
_TIMERS: Final = (_STOP_ACCEPT, _EXIT_RETRY)
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}

type Record = Callable[[WriteItem], None]


class Phase(StrEnum):
    """Where the round trip is."""

    FLAT = "flat"
    ENTERING = "entering"
    PROTECTED = "protected"
    EXITING = "exiting"


class MacdBaselineConfig(GuardedConfig, frozen=True, kw_only=True):
    """``macd_baseline``'s ``[strategy.config]`` settings, the run's risk caps, and the run it records under."""

    env: str
    run_id: str
    notional_usd: Decimal
    bar_spec: str = "1-HOUR-LAST"
    fast: int = 12
    slow: int = 26
    signal: int = 9
    trend_ema: int = 50
    stop_pct: Decimal = Decimal(3)
    use_market_context: bool = True
    stale_quote_s: Decimal = Decimal(10)
    stale_check_s: int = 1


def _side_of(position: Position | None) -> PositionSide:
    if position is None:
        return PositionSide.FLAT
    return PositionSide.LONG if position.side == NautilusPositionSide.LONG else PositionSide.SHORT


class MacdBaseline(KfsStrategy, StaleQuoteBlock):
    """Trades MACD histogram crosses with the trend, protected by a stop (see the module docstring)."""

    def __init__(self, config: MacdBaselineConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate)
        if not 0 < config.stop_pct < 100:
            raise ValueError(f"stop_pct must be inside (0, 100), got {config.stop_pct}")
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(f"{config.instrument_id}-{config.bar_spec}-INTERNAL")
        self._limits = config.risk_limits()
        self.rule = MacdState(config.fast, config.slow, config.signal, config.trend_ema)
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self.entry_decision: UUID | None = None
        self._exit_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._stop_id: ClientOrderId | None = None
        self._exit_id: ClientOrderId | None = None
        self._kill_switch_tags: list[str] | None = None

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
        """Feed every bar close to the rule; act on its signal (entry while flat, exit while in a position)."""
        if bar.bar_type != self.bar_type:
            return
        position = self._position()
        signal = self.rule.on_close(bar.close.as_decimal(), _side_of(position))
        try:
            if signal in (Signal.ENTER_LONG, Signal.ENTER_SHORT) and self.phase == Phase.FLAT and position is None:
                self._decide_entry(bar, OrderSide.BUY if signal == Signal.ENTER_LONG else OrderSide.SELL)
            elif signal == Signal.EXIT and position is not None and self.phase in (Phase.ENTERING, Phase.PROTECTED):
                self._decide_exit(bar, position)
        except Exception:
            # Fail closed: no new order this bar; a position keeps its stop.
            log.exception("bar decision failed", extra={"fields": {"code": "bar_decision_failed"}})

    def _signal_inputs(self, bar: Bar) -> JsonObject:
        return {
            "bar_close": bar.close.as_decimal(),
            "histogram": self.rule.histogram,
            "histogram_previous": self.rule.previous,
            "trend_ema": self.rule.trend,
        }

    def _decide_entry(self, bar: Bar, side: OrderSide) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        instrument = self._instrument()
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        now = self.clock.timestamp_ns()
        step = instrument.size_increment.as_decimal()
        position_side = PositionSide.LONG if side == OrderSide.BUY else PositionSide.SHORT
        try:
            request = EntryRequest(
                side=side,
                mid=mid,
                equity=self._equity(),
                open_notional=guard.open_notional(self.cache),
                size_step=step,
                taker_fee=self._cfg.taker_fee,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                funding_periods=funding_periods(now, None),
                funding_rate=self._funding_rate(),
                stop_px=stop_price(mid, position_side, self._cfg.stop_pct, instrument.price_increment.as_decimal()),
                fixed_quantity=hold_quantity(self._cfg.notional_usd, mid, step),
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
        inputs: JsonObject = {
            **self._signal_inputs(bar),
            "mid": mid,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
            **verdict.figures.as_inputs(),
        }
        self._record_decision(decision, Intent.ENTER, side, ENTRY_REASON, inputs, verdict.approved, verdict.reason, now)
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
        self.submit_order(order)

    def _decide_exit(self, bar: Bar, position: Position) -> None:
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        inputs: JsonObject = {**self._signal_inputs(bar), "position_qty": position.quantity.as_decimal()}
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is not None:
            inputs = {**inputs, "mid": (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2}
        self._record_decision(decision, Intent.EXIT, closing, EXIT_REASON, inputs, True, None, now)
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
        """The entry's complete fill rests the stop."""
        if event.client_order_id != self._entry_id or self._stop_id is not None:
            return
        entry = self.cache.order(event.client_order_id)
        if entry is not None and entry.status == NautilusOrderStatus.FILLED and self.phase == Phase.ENTERING:
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
        """Closed from any source: cancel what rests and start over."""
        if event.instrument_id != self._instrument_id:
            return
        for order in self.cache.orders_open(strategy_id=self.id):
            self.cancel_order(order)
        self._back_to_flat()

    # Stop -----------------------------------------------------------------------------------

    def _submit_stop(self, entry: Order) -> None:
        position = self._position()
        if position is None:
            return
        instrument = self._instrument()
        fills = [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in entry.events if isinstance(e, OrderFilled)]
        avg_px = weighted_avg(fills)
        assert avg_px is not None  # the entry is filled
        side = _side_of(position)
        trigger = stop_price(avg_px, side, self._cfg.stop_pct, instrument.price_increment.as_decimal())
        order = self.order_factory.stop_market(
            self._instrument_id,
            NautilusOrderSide.SELL if side == PositionSide.LONG else NautilusOrderSide.BUY,
            position.quantity,
            trigger_price=instrument.make_price(trigger),
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
