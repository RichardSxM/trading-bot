"""``RandomTaker``: a known loser for the replay lab's self check (spec 0008 AC-14).

Every ``interval_s`` seconds on the UTC clock, while flat, it draws a side from the seed and the
boundary (``trading_core.strategy.random_taker``), sizes ``notional_usd`` at the mid, runs the
pure risk check, records the decision, and submits a market entry. It closes with a reduce only
market order ``hold_s`` seconds after the entry's first fill. It places no stop. Like every
trader strategy it honours the pause gate and records through ``record``.

Round trip: ``flat`` → ``entering`` (entry submitted) → ``holding`` (filled) → ``exiting`` → ``flat``.
"""

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
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
from nautilus_trader.model.objects import Currency, Quantity
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.node.placeholder import Record
from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate
from trading_core.recording.enums import Env, Intent, OrderSide, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import DecisionRow, JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import EntryRequest, InvalidRiskInput, RiskLimits, check_entry, refusal_severity
from trading_core.risk.sizing import entry_quantity
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.random_taker import NS_PER_S, draw_side, next_boundary_ns

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "random_taker"
EXIT_RETRY_NS: Final = NS_PER_S
USD: Final = Currency.from_str("USD")

_BOUNDARY: Final = "random_taker_boundary"
_HOLD: Final = "random_taker_hold"
_EXIT_RETRY: Final = "random_taker_exit_retry"


class Phase(StrEnum):
    """Where the round trip is."""

    FLAT = "flat"
    ENTERING = "entering"
    HOLDING = "holding"
    EXITING = "exiting"


class RandomTakerConfig(KfsStrategyConfig, frozen=True):
    """The random taker's ``[strategy.config]`` settings, the run's seed and risk caps, and the run it records under."""

    env: str
    run_id: str
    seed: int
    max_order_notional_usd: Decimal
    max_leverage: Decimal
    interval_s: int = 60
    hold_s: int = 30
    notional_usd: Decimal = Decimal(1000)


class RandomTaker(KfsStrategy):
    """Enters at market on a random side every boundary while flat, exits at market after ``hold_s``."""

    # Timers of one round trip, cancelled when it ends (the boundary timer keeps running).
    _round_trip_timers: tuple[str, ...] = (_HOLD, _EXIT_RETRY)

    def __init__(self, config: RandomTakerConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self._limits = RiskLimits(max_order_notional=config.max_order_notional_usd, max_leverage=config.max_leverage)
        self._boundary_ns = 0
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self._entry_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._exit_decision: UUID | None = None
        self._exit_id: ClientOrderId | None = None

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to quotes and arm the first boundary."""
        super().on_start()
        self.subscribe_quote_ticks(self._instrument_id)
        self._arm_boundary()

    def on_stop(self) -> None:
        """Cancel entries and pause (the base class); a position stays."""
        super().on_stop()

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """The mid is read from the cache at the boundary; nothing to do here."""

    # Entry ----------------------------------------------------------------------------------

    def _arm_boundary(self) -> None:
        self._boundary_ns = next_boundary_ns(self.clock.timestamp_ns(), self._cfg.interval_s)
        self._alert(_BOUNDARY, self._boundary_ns, self._on_boundary)

    def _on_boundary(self, event: object) -> None:
        boundary = self._boundary_ns
        self._arm_boundary()
        if self.phase != Phase.FLAT or self._position() is not None:
            return
        try:
            self._decide_entry(boundary)
        except Exception:
            # Fail closed: no entry this boundary; the next one tries again.
            log.exception("entry decision failed", extra={"fields": {"code": "entry_decision_failed"}})

    def _decide_entry(self, boundary_ns: int) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.info("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        instrument = self._instrument()
        side = draw_side(self._cfg.seed, boundary_ns)
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        quantity = entry_quantity(self._cfg.notional_usd, mid, instrument.size_increment.as_decimal())
        equity = self._equity()
        request = EntryRequest(
            quantity=quantity,
            mid=mid,
            equity=equity,
            open_notional=Decimal(0),
            has_position=False,
            paused=self._gate.paused,
            blocks=[str(r) for r in self._blocks.active()],
        )
        try:
            verdict = check_entry(request, self._limits)
        except InvalidRiskInput:
            log.exception("risk check could not judge the entry", extra={"fields": {"code": "risk_input_invalid"}})
            return
        decision = uuid.uuid7()
        inputs: JsonObject = {
            "boundary_ns": boundary_ns,
            "mid": mid,
            "equity": equity,
            "open_notional": Decimal(0),
            "quantity": quantity,
        }
        self._record_decision(decision, Intent.ENTER, side, "random_entry", inputs, verdict.approved, verdict.reason)
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
        order = self._entry_order(side, instrument.make_qty(quantity), quote, decision)
        self.phase = Phase.ENTERING
        self._entry_decision = decision
        self._entry_id = order.client_order_id
        self.submit_order(order)
        self._entry_submitted()

    def _entry_order(self, side: OrderSide, quantity: Quantity, quote: QuoteTick, decision: UUID) -> Order:
        """The entry: a market order (a subclass may enter otherwise)."""
        return self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.BUY if side == OrderSide.BUY else NautilusOrderSide.SELL,
            quantity,
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{decision}"],
        )

    def _entry_submitted(self) -> None:
        """Called right after the entry is submitted; a market entry needs nothing more."""

    # Order events ---------------------------------------------------------------------------

    def on_order_denied(self, event: OrderDenied) -> None:
        """A denied entry goes back to flat; a denied exit is retried."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejected entry goes back to flat; a rejected exit is retried."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """An entry canceled before any fill goes back to flat; a canceled exit is retried."""
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
                self._entry_decision,
                {"client_order_id": str(client_order_id)},
            )
            self._back_to_flat()
        elif client_order_id == self._exit_id and self.phase == Phase.EXITING and self._position() is not None:
            log.warning("exit order refused, retrying in 1 s", extra={"fields": {"code": "exit_refused", "why": why}})
            self._alert(_EXIT_RETRY, self.clock.timestamp_ns() + EXIT_RETRY_NS, self._submit_exit)

    def on_order_filled(self, event: OrderFilled) -> None:
        """The entry's first fill starts the hold."""
        if event.client_order_id != self._entry_id or self.phase != Phase.ENTERING:
            return
        self.phase = Phase.HOLDING
        self._alert(_HOLD, event.ts_event + self._cfg.hold_s * NS_PER_S, self._on_hold_over)

    def on_position_closed(self, event: PositionClosed) -> None:
        """Flat: cancel any leftover entry or exit and every timer but the boundary."""
        if event.instrument_id != self._instrument_id:
            return
        for client_order_id in (self._entry_id, self._exit_id):
            order = None if client_order_id is None else self.cache.order(client_order_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
        self._back_to_flat()

    # Exit -----------------------------------------------------------------------------------

    def _on_hold_over(self, event: object) -> None:
        position = self._position()
        if position is None or self.phase != Phase.HOLDING:
            return
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        if entry is not None and entry.is_open:
            self.cancel_order(entry)  # a part filled entry stops growing before the exit
        decision = uuid.uuid7()
        quote = self.cache.quote_tick(self._instrument_id)
        inputs: JsonObject = {"position_qty": position.quantity.as_decimal()}
        if quote is not None:
            inputs = {**inputs, "mid": (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2}
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        self._record_decision(decision, Intent.EXIT, closing, "time", inputs, True, None)
        self._exit_decision = decision
        self.phase = Phase.EXITING
        self._submit_exit()

    def _submit_exit(self, event: object = None) -> None:
        position = self._position()
        if position is None:
            return
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.SELL if position.side == NautilusPositionSide.LONG else NautilusOrderSide.BUY,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=[f"{DECISION_TAG}{self._exit_decision}"],
        )
        self._exit_id = order.client_order_id
        self.submit_order(order)

    # Helpers --------------------------------------------------------------------------------

    def _back_to_flat(self) -> None:
        for name in self._round_trip_timers:
            self._cancel_timer(name)
        self._reset()

    def _instrument(self) -> Instrument:
        instrument = self.cache.instrument(self._instrument_id)
        if instrument is None:
            raise LookupError(f"instrument {self._instrument_id} not in the cache")
        return instrument

    def _position(self) -> Position | None:
        positions = self.cache.positions_open(instrument_id=self._instrument_id)
        return positions[0] if positions else None

    def _equity(self) -> Decimal:
        # Flat at every entry, so equity is the balance (no unrealized P&L to add).
        account = self.portfolio.account(self._instrument_id.venue)
        if account is None:
            raise LookupError(f"no account for {self._instrument_id.venue}")
        balance = account.balance_total(USD)
        return Decimal(0) if balance is None else balance.as_decimal()

    def _alert(self, name: str, at_ns: int, callback: Callable[[object], None]) -> None:
        self._cancel_timer(name)
        self.clock.set_time_alert_ns(name, at_ns, callback)

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
            decided_ns=self.clock.timestamp_ns(),
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
        log.info(message, extra={"fields": {"code": kind.value, "event_key": key}})
