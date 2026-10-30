"""``BuyAndHold``: buy once at the first bar close and hold (spec 0014 AC-12), the candle lab's benchmark.

It aggregates its own ``bar_spec`` bars from trades (``<instrument>-<bar_spec>-INTERNAL``), so it runs
unchanged in the candle lab, the order book replay, and live. At the first bar close while flat it
sizes ``notional_usd`` at the mid, asks ``check_entry`` (every input sourced as ``PlaceholderTimed``
sources it), records the decision, and buys at market. A refusal is retried at the next bar close, at
most ``max_entry_attempts`` times. It never sets a stop: the range end close or the liquidation guard
ends the position, and any close resets it. The stale quote block comes from ``StaleQuoteBlock``.
After a restart it ignores a bar older than the last check and keeps holding a resumed position
(spec 0023 AC-15, AC-16).
"""

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
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import (
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
from trading_core.node.strategy import KfsStrategy, PauseGate, decision_of
from trading_core.recording.enums import Env, Intent, OrderOrigin, OrderSide, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.mappers import DECISION_TAG, HALT_TAG, ORIGIN_TAG
from trading_core.recording.rows import DecisionRow, JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import EntryRequest, InvalidRiskInput, check_entry, refusal_severity
from trading_core.risk.funding import funding_periods
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.buy_and_hold import EntryAttempts, hold_quantity

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "buy_and_hold"
ENTRY_REASON: Final = "buy_and_hold_entry"
USD: Final = Currency.from_str("USD")
_STALE = "buy_and_hold_stale_check"
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}

type Record = Callable[[WriteItem], None]


class Phase(StrEnum):
    """Where the one round trip is."""

    FLAT = "flat"
    ENTERING = "entering"
    HOLDING = "holding"


class BuyAndHoldConfig(GuardedConfig, frozen=True, kw_only=True):
    """``buy_and_hold``'s ``[strategy.config]`` settings, the run's risk caps, and the run it records under."""

    env: str
    run_id: str
    notional_usd: Decimal
    bar_spec: str = "1-HOUR-LAST"
    max_entry_attempts: int = 24
    use_market_context: bool = True
    stale_quote_s: Decimal = Decimal(10)
    stale_check_s: int = 1


class BuyAndHold(KfsStrategy, StaleQuoteBlock):
    """Buys once at the first bar close and holds (see the module docstring)."""

    uses_stop = False  # it never places a stop (spec 0023 AC-9)

    def __init__(self, config: BuyAndHoldConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(f"{config.instrument_id}-{config.bar_spec}-INTERNAL")
        self._limits = config.risk_limits()
        self._attempts = EntryAttempts(config.max_entry_attempts)
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self.entry_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._exit_id: ClientOrderId | None = None

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

        Idempotent: a close already sent is not sent again.
        """
        self._cancel_entries()
        position = self._position()
        if position is None or self._exit_id is not None:
            return
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.SELL,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=[f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"],
        )
        self._exit_id = order.client_order_id
        self.submit_order(order)

    def resume_position(self, position: Position, stop_order: Order | None) -> None:
        """Take back ``position`` after a restart (spec 0023 AC-16): it keeps holding, as it always does."""
        self._cancel_entries()
        self.entry_decision = decision_of(self.cache.order(position.opening_order_id))
        self._entry_id = position.opening_order_id
        self.phase = Phase.HOLDING

    # Entry ----------------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """At each bar close while flat and not given up, decide the entry."""
        if bar.bar_type != self.bar_type or self.phase != Phase.FLAT or self._attempts.abandoned:
            return
        if self._position() is not None or not self.fresh_signal(bar.ts_event):
            return
        try:
            self._decide_entry(bar)
        except Exception:
            # Fail closed: no entry this bar; the next one tries again.
            log.exception("entry decision failed", extra={"fields": {"code": "entry_decision_failed"}})

    def _decide_entry(self, bar: Bar) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        instrument = self._instrument()
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        now = self.clock.timestamp_ns()
        step = instrument.size_increment.as_decimal()
        try:
            request = EntryRequest(
                side=OrderSide.BUY,
                mid=mid,
                equity=self._equity(),
                open_notional=guard.open_notional(self.cache),
                size_step=step,
                taker_fee=self._cfg.taker_fee,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                funding_periods=funding_periods(now, None),
                funding_rate=self._funding_rate(),
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
            "bar_close": bar.close.as_decimal(),
            "mid": mid,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
            "attempt": self._attempts.refused + 1,
            **verdict.figures.as_inputs(),
        }
        self._record_decision(decision, inputs, verdict.approved, verdict.reason, now)
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
            if self._attempts.refuse():
                log.warning(
                    "entry abandoned after the attempt cap",
                    extra={"fields": {"code": "entry_abandoned", "attempts": self._attempts.refused}},
                )
            return
        if self._gate.halted or self._gate.paused:  # the gate again, right before submitting
            log.info("entry denied by the gate", extra={"fields": {"code": "entry_denied_gate"}})
            return
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.BUY,
            instrument.make_qty(verdict.quantity),
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{decision}"],
        )
        self.phase = Phase.ENTERING
        self.entry_decision = decision
        self._entry_id = order.client_order_id
        self.submit_order(order)

    # Order and position events --------------------------------------------------------------

    def on_order_filled(self, event: OrderFilled) -> None:
        """The entry's complete fill: holding."""
        if event.client_order_id != self._entry_id:
            return
        entry = self.cache.order(event.client_order_id)
        if entry is not None and entry.status == NautilusOrderStatus.FILLED:
            self.phase = Phase.HOLDING

    def on_order_denied(self, event: OrderDenied) -> None:
        """An entry denied by the gate or the RiskEngine: see ``_refused``."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """An entry the venue rejected: see ``_refused``."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """An entry canceled before any fill: see ``_refused``."""
        self._refused(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """Like a cancel."""
        self._refused(event.client_order_id, "expired")

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING and self._position() is None:
            self._risk_event(
                f"order_rejected:{client_order_id}",
                RiskEventKind.ORDER_REJECTED,
                Severity.WARNING,
                f"entry {why}",
                self.entry_decision,
                {"client_order_id": str(client_order_id)},
            )
            self._reset()
        elif client_order_id == self._exit_id:
            self._exit_id = None  # the kill switch polls flatten again

    def on_position_closed(self, event: PositionClosed) -> None:
        """Closed from any source (range end, liquidation, kill switch): cancel what rests, start over."""
        if event.instrument_id != self._instrument_id:
            return
        for order in self.cache.orders_open(strategy_id=self.id):
            self.cancel_order(order)
        self._attempts.reset()
        self._reset()

    # Helpers --------------------------------------------------------------------------------

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

    def _record_decision(
        self, decision: UUID, inputs: JsonObject, approved: bool, risk_reason: str | None, now_ns: int
    ) -> None:
        row = DecisionRow(
            id=decision,
            env=self._env,
            run_id=self._run_id,
            instrument_id=self._cfg.instrument_id,
            intent=Intent.ENTER,
            side=OrderSide.BUY,
            reason=ENTRY_REASON,
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
