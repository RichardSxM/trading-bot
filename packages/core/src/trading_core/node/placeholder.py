"""``PlaceholderTimed``: the walking skeleton strategy (spec 0006 AC-3 to AC-9, AC-14, AC-20).

Every ``interval_min`` minutes (2 s after the UTC boundary) it decides an entry from 1 minute
mid bars, sizes it, runs the pure risk check, records the decision, and submits a market entry.
Once the entry is filled it rests a reduce only stop, then closes on time through the exit
loop. Every rule lives in ``trading_core.risk`` and ``trading_core.strategy``; this class only
holds the round trip state, its timers, and the order calls.

Round trip: ``flat`` → ``entering`` (entry submitted) → ``protected`` (stop accepted) →
``exiting`` (exit loop) → ``flat``. A stop fill goes straight to ``flat``. An order the venue
cancels or expires is handled like a refusal: an unfilled entry goes back to ``flat``, a partly
filled entry, a lost stop, or a lost exit runs (or keeps running) the exit loop.

``flatten`` (the kill switch, spec 0010 AC-1, AC-3) closes through the same exit loop, or adopts
the one already running; every exit order after it carries the kill switch tags in place of a
decision. Asked while an entry is in flight, it cancels the entry and remembers the request: a
late fill still gets its stop, then the exit loop starts at once.
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

from trading_core.marketdata.context import context_log_fields
from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate
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
from trading_core.risk.check import EntryRequest, InvalidRiskInput, RiskLimits, check_entry, refusal_severity
from trading_core.risk.sizing import entry_quantity
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.placeholder import DECISION_DELAY_NS, NS_PER_MIN, entry_side, next_boundary_ns, stop_price

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "placeholder_timed"
NS_PER_S: Final = 1_000_000_000
PARTIAL_ENTRY_NS: Final = 5 * NS_PER_S
EXIT_RETRY_NS: Final = NS_PER_S
EXIT_FAILURES_TO_REPORT: Final = 5
STALE_CHECK_NS: Final = NS_PER_S
USD: Final = Currency.from_str("USD")

_BOUNDARY = "placeholder_boundary"
_HOLD = "placeholder_hold"
_STOP_ACCEPT = "placeholder_stop_accept"
_PARTIAL = "placeholder_partial_entry"
_EXIT_RETRY = "placeholder_exit_retry"
_STALE = "placeholder_stale_check"
_ROUND_TRIP_TIMERS: Final = (_HOLD, _STOP_ACCEPT, _PARTIAL, _EXIT_RETRY)
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


class PlaceholderConfig(KfsStrategyConfig, frozen=True):
    """The placeholder's settings (``PLACEHOLDER_*``, ``RISK_*``) plus the run it records under."""

    env: str
    run_id: str
    interval_min: int
    hold_min: int
    lookback_min: int
    stop_pct: Decimal
    notional_usd: Decimal
    max_order_notional_usd: Decimal
    max_leverage: Decimal
    stale_quote_s: Decimal
    stop_accept_timeout_s: Decimal


def _ns(seconds: Decimal) -> int:
    return int(seconds * NS_PER_S)


class PlaceholderTimed(KfsStrategy):
    """Enters on a fixed beat, protects with a stop, closes on time (see the module docstring)."""

    def __init__(self, config: PlaceholderConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self._bar_type = BarType.from_str(f"{config.instrument_id}-1-MINUTE-MID-INTERNAL")
        self._limits = RiskLimits(max_order_notional=config.max_order_notional_usd, max_leverage=config.max_leverage)
        self._boundary_ns = 0
        self._stale_since_ns: int | None = None
        self._started_ns = 0
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self._entry_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._stop_id: ClientOrderId | None = None
        self._exit_decision: UUID | None = None
        self._exit_id: ClientOrderId | None = None
        self._exit_failures = 0
        self.position_opened_ns: int | None = None
        self.exit_due_ns: int | None = None
        # Set by ``flatten`` until the round trip ends (spec 0010 AC-1).
        self._kill_switch_tags: list[str] | None = None
        self._kill_switch_pending = False

    # Read by the snapshots ------------------------------------------------------------------

    @property
    def exiting(self) -> bool:
        """True while the exit loop runs."""
        return self.phase == Phase.EXITING

    def stop_order(self) -> Order | None:
        """The protective stop of the current round trip, if one was submitted."""
        return None if self._stop_id is None else self.cache.order(self._stop_id)

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to quotes and 1 minute mid bars, then arm the boundary and stale feed timers."""
        super().on_start()
        self._started_ns = self.clock.timestamp_ns()
        self.subscribe_quote_ticks(self._instrument_id)
        self.subscribe_bars(self._bar_type)
        self._arm_boundary()
        self.clock.set_timer_ns(_STALE, STALE_CHECK_NS, 0, 0, self._on_stale_check)

    def on_stop(self) -> None:
        """Cancel entries and pause (the base class); positions, stops, and exits stay."""
        super().on_stop()

    def flatten(self, halt_id: UUID) -> None:
        """Close the position through the exit loop, or cancel an entry in flight (spec 0010 AC-1, AC-3).

        Idempotent: an exit loop already running is adopted (its next retries carry the tags), and
        the kill switch may call this again on every poll until flat. While flat it only cancels
        stray entries; nothing is tagged, since no round trip end would clear the tags.
        """
        position = self._position()
        if position is not None or self.phase != Phase.FLAT:
            self._kill_switch_tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        if position is not None:
            self._cancel_timer(_PARTIAL)
            self._start_exit_loop()
        elif self.phase == Phase.ENTERING:
            self._kill_switch_pending = True
        self._cancel_entries()  # after: a cancel of a part filled entry arrives while ``exiting``
        log.info(
            "flatten requested",
            extra={"fields": {"code": "strategy_flatten", "halt_id": str(halt_id), "phase": self.phase.value}},
        )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Quotes drive the sandbox and the stale check; nothing to do here."""

    def on_bar(self, bar: Bar) -> None:
        """Bars are read from the cache at the boundary; with the market context on, log it once per bar.

        No entry or exit rule reads the context yet (spec 0009 AC-11).
        """
        if self._cfg.use_market_context:
            now_ns = self.clock.timestamp_ns()
            fields = context_log_fields(self.market_context(), now_ns)
            log.info("market context", extra={"fields": {"code": "market_context", **fields}})

    # Entry ----------------------------------------------------------------------------------

    def _arm_boundary(self) -> None:
        self._boundary_ns = next_boundary_ns(self.clock.timestamp_ns(), self._cfg.interval_min)
        self._alert(_BOUNDARY, self._boundary_ns + DECISION_DELAY_NS, self._on_boundary)

    def _on_boundary(self, event: object) -> None:
        boundary = self._boundary_ns
        self._arm_boundary()
        if self.phase != Phase.FLAT or self.cache.positions_open(instrument_id=self._instrument_id):
            log.info("entry skipped, a round trip is open", extra={"fields": {"code": "entry_skipped_open"}})
            return
        try:
            self._decide_entry(boundary)
        except Exception:
            # Fail closed: no entry this boundary; the next one tries again.
            log.exception("entry decision failed", extra={"fields": {"code": "entry_decision_failed"}})

    def _decide_entry(self, boundary_ns: int) -> None:
        bars = [b for b in reversed(self.cache.bars(self._bar_type)) if b.ts_event <= boundary_ns]
        closes = [b.close.as_decimal() for b in bars]
        side = entry_side(closes, self._cfg.lookback_min)
        if side is None:
            log.info(
                "entry skipped, warming up",
                extra={"fields": {"code": "entry_skipped_warmup", "bars": len(closes)}},
            )
            return
        instrument = self._instrument()
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        quantity = entry_quantity(self._cfg.notional_usd, mid, instrument.size_increment.as_decimal())
        equity = self._equity(mid)
        request = EntryRequest(
            quantity=quantity,
            mid=mid,
            equity=equity,
            open_notional=Decimal(0),
            has_position=False,
            paused=self._gate.paused,
            halted=self._gate.halted,
            blocks=[str(r) for r in self._blocks.active()],
        )
        try:
            verdict = check_entry(request, self._limits)
        except InvalidRiskInput:
            log.exception("risk check could not judge the entry", extra={"fields": {"code": "risk_input_invalid"}})
            return
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        inputs: JsonObject = {
            "close_now": closes[-1],
            "close_lookback": closes[-1 - self._cfg.lookback_min],
            "mid": mid,
            "equity": equity,
            "open_notional": Decimal(0),
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
            "quantity": quantity,
        }
        self._record_decision(
            decision, Intent.ENTER, side, "timed_entry", inputs, verdict.approved, verdict.reason, now
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
        if self._gate.halted or self._gate.paused:  # the gate again, right before submitting (AC-20)
            self._entry_denied(decision, "halted" if self._gate.halted else "paused")
            return
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.BUY if side == OrderSide.BUY else NautilusOrderSide.SELL,
            instrument.make_qty(quantity),
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{decision}"],
        )
        self.phase = Phase.ENTERING
        self._entry_decision = decision
        self._entry_id = order.client_order_id
        try:
            self.submit_order(order)
        except Exception:
            # Fail closed: no entry in flight, and the next boundary tries again.
            self._risk_event(
                f"entry_submit_failed:{decision}",
                RiskEventKind.ORDER_REJECTED,
                Severity.WARNING,
                "entry submit failed",
                decision,
                {"client_order_id": str(order.client_order_id)},
            )
            self._back_to_flat()
            raise

    def _entry_denied(self, decision: UUID, reason: str) -> None:
        self._risk_event(
            f"entry_denied:{decision}",
            RiskEventKind.LIMIT_HIT,
            Severity.INFO,
            f"entry denied: {reason}",
            decision,
            {"reason": reason},
        )
        self._back_to_flat()

    # Order events ---------------------------------------------------------------------------

    def on_order_denied(self, event: OrderDenied) -> None:
        """An entry denied by the gate or the RiskEngine goes back to flat; a stop or exit counts as failed."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejected entry goes back to flat; a rejected stop or exit counts as failed."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING:
            decision = self._entry_decision
            assert decision is not None
            if why.startswith("denied"):
                self._entry_denied(decision, why)
            else:
                self._risk_event(
                    f"order_rejected:{client_order_id}",
                    RiskEventKind.ORDER_REJECTED,
                    Severity.WARNING,
                    f"entry {why}",
                    decision,
                    {"client_order_id": str(client_order_id)},
                )
                self._back_to_flat()
        elif client_order_id == self._stop_id:
            self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_id:
            self._exit_refused(why)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """An order the venue cancels: see ``_ended``."""
        self._ended(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """An order the venue expires: see ``_ended``."""
        self._ended(event.client_order_id, "expired")

    def _ended(self, client_order_id: ClientOrderId, why: str) -> None:
        # Our own cancels never get here: a flat round trip has reset the ids, and the partial
        # entry timeout is already ``exiting`` when its cancel arrives.
        position = self._position()
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING:
            if position is None and (self._kill_switch_pending or self._gate.halted):
                # Expected: the kill switch cancelled it (spec 0010 AC-1).
                log.info(
                    "entry cancelled by the kill switch",
                    extra={"fields": {"code": "entry_canceled_kill_switch", "client_order_id": str(client_order_id)}},
                )
                self._back_to_flat()
            elif position is None:
                self._risk_event(
                    f"entry_canceled:{client_order_id}",
                    RiskEventKind.ORDER_REJECTED,
                    Severity.WARNING,
                    f"entry {why} before any fill",
                    self._entry_decision,
                    {"client_order_id": str(client_order_id)},
                )
                self._back_to_flat()
            else:
                self._close_partial_entry(f"entry {why} after a partial fill; closing it")
        elif position is None:
            return
        elif client_order_id == self._stop_id and self.phase in (Phase.ENTERING, Phase.PROTECTED):
            self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_id and self.phase == Phase.EXITING:
            self._exit_refused(why)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """An accepted stop protects the position (unless the exit loop already runs)."""
        if event.client_order_id == self._stop_id:
            self._cancel_timer(_STOP_ACCEPT)
            if self.phase == Phase.ENTERING:
                self.phase = Phase.PROTECTED

    def on_order_filled(self, event: OrderFilled) -> None:
        """The entry's first fill starts the hold; its complete fill rests the stop."""
        if event.client_order_id != self._entry_id:
            return
        if self.position_opened_ns is None:
            self.position_opened_ns = event.ts_event
            self.exit_due_ns = event.ts_event + self._cfg.hold_min * NS_PER_MIN
            self._alert(_HOLD, self.exit_due_ns, self._on_hold_over)
            self._alert(_PARTIAL, event.ts_event + PARTIAL_ENTRY_NS, self._on_partial_timeout)
        entry = self.cache.order(event.client_order_id)
        if entry is not None and entry.status == NautilusOrderStatus.FILLED and self.phase == Phase.ENTERING:
            self._cancel_timer(_PARTIAL)
            self._submit_stop(entry)
            if self._kill_switch_pending:  # a late fill after flatten (spec 0010 AC-3)
                log.info("late entry fill, flattening", extra={"fields": {"code": "late_fill_flatten"}})
                self._start_exit_loop()

    def on_position_closed(self, event: PositionClosed) -> None:
        """Flat: cancel the leftover stop and exit order and every round trip timer, then wait for the boundary."""
        if event.instrument_id != self._instrument_id:
            return
        for client_order_id in (self._stop_id, self._exit_id, self._entry_id):
            order = None if client_order_id is None else self.cache.order(client_order_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
        self._back_to_flat()

    # Stop -----------------------------------------------------------------------------------

    def _submit_stop(self, entry: Order) -> None:
        position = self._position()
        if position is None:  # the stop filled already, or the position closed some other way
            return
        instrument = self._instrument()
        fills = [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in entry.events if isinstance(e, OrderFilled)]
        avg_px = weighted_avg(fills)
        assert avg_px is not None  # the entry is filled
        side = PositionSide.LONG if position.side == NautilusPositionSide.LONG else PositionSide.SHORT
        trigger = stop_price(avg_px, side, self._cfg.stop_pct, instrument.price_increment.as_decimal())
        order = self.order_factory.stop_market(
            self._instrument_id,
            NautilusOrderSide.SELL if side == PositionSide.LONG else NautilusOrderSide.BUY,
            position.quantity,
            trigger_price=instrument.make_price(trigger),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{self._entry_decision}", f"{PARENT_TAG}{entry.client_order_id}"],
        )
        self._stop_id = order.client_order_id
        self._alert(
            _STOP_ACCEPT, self.clock.timestamp_ns() + _ns(self._cfg.stop_accept_timeout_s), self._on_stop_timeout
        )
        self.submit_order(order)

    def _on_stop_timeout(self, event: object) -> None:
        stop = self.stop_order()
        if stop is not None and stop.status in (NautilusOrderStatus.ACCEPTED, NautilusOrderStatus.TRIGGERED):
            return
        if stop is not None and stop.is_closed and self._position() is None:
            return  # filled, and the position closed with it
        self._stop_failed("stop not accepted in time")

    def _stop_failed(self, why: str) -> None:
        if self._stop_id is None or self.phase == Phase.FLAT:
            return
        self._cancel_timer(_STOP_ACCEPT)
        self._blocks.add(BlockReason.STOP_NOT_ACCEPTED)
        self._risk_event(
            f"stop_not_accepted:{self._stop_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.CRITICAL,
            f"{why}; closing the position",
            self._entry_decision,
            {"client_order_id": str(self._stop_id)},
        )
        self._start_exit_loop()

    def _on_partial_timeout(self, event: object) -> None:
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        if entry is None or entry.status != NautilusOrderStatus.PARTIALLY_FILLED or self.phase != Phase.ENTERING:
            return
        self._close_partial_entry("entry still partly filled after 5 s; closing it")
        self.cancel_order(entry)  # after: its cancel arrives while ``exiting`` and is ignored

    def _close_partial_entry(self, message: str) -> None:
        self._cancel_timer(_PARTIAL)
        self._risk_event(
            f"partial_entry:{self._entry_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.WARNING,
            message,
            self._entry_decision,
            {"client_order_id": str(self._entry_id)},
        )
        self._start_exit_loop()

    # Exit -----------------------------------------------------------------------------------

    def _on_hold_over(self, event: object) -> None:
        position = self._position()
        if position is None or self.phase == Phase.FLAT:
            return
        if self.phase == Phase.EXITING:
            # The exit loop already closes it under the entry decision (a stop failure or partial
            # entry); a time decision here would carry no order.
            log.info("hold over while the exit loop runs", extra={"fields": {"code": "hold_over_exiting"}})
            return
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        quote = self.cache.quote_tick(self._instrument_id)
        inputs: JsonObject = {"position_qty": position.quantity.as_decimal(), "opened_ns": self.position_opened_ns}
        if quote is not None:
            inputs = {**inputs, "mid": (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2}
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        self._record_decision(decision, Intent.EXIT, closing, "time", inputs, True, None, now)
        self._exit_decision = decision
        self._start_exit_loop()

    def _start_exit_loop(self) -> None:
        if self.phase == Phase.EXITING:
            return
        self.phase = Phase.EXITING
        self._submit_exit()

    def _submit_exit(self, event: object = None) -> None:
        position = self._position()
        if position is None:
            return
        tags = self._kill_switch_tags or [f"{DECISION_TAG}{self._exit_decision or self._entry_decision}"]
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
        position = self._position()
        if position is None:
            log.info("exit refused while flat, ignored", extra={"fields": {"code": "exit_refused_flat", "why": why}})
            return
        self._exit_failures += 1
        log.warning(
            "exit order refused, retrying in 1 s",
            extra={"fields": {"code": "exit_refused", "why": why, "failures": self._exit_failures}},
        )
        if self._exit_failures == EXIT_FAILURES_TO_REPORT:
            self._blocks.add(BlockReason.EXIT_FAILED)
            self._risk_event(
                f"exit_failed:{position.id}",
                RiskEventKind.ORDER_REJECTED,
                Severity.CRITICAL,
                f"closing the position failed {EXIT_FAILURES_TO_REPORT} times; still retrying every 1 s",
                self._exit_decision or self._entry_decision,
                {"position_id": str(position.id), "last_reason": why},
            )
        self._alert(_EXIT_RETRY, self.clock.timestamp_ns() + EXIT_RETRY_NS, self._submit_exit)

    # Stale feed -----------------------------------------------------------------------------

    def _on_stale_check(self, event: object) -> None:
        now = self.clock.timestamp_ns()
        quote = self.cache.quote_tick(self._instrument_id)
        # Before the first quote, the feed counts from the start: a fresh node is not a stale feed.
        last_ns = self._started_ns if quote is None else quote.ts_event
        stale = now - last_ns > _ns(self._cfg.stale_quote_s)
        if stale and self._blocks.add(BlockReason.STALE_FEED):
            self._stale_since_ns = now
            self._risk_event(
                f"stale_feed:{self._run_id}:{now}",
                RiskEventKind.STALE_FEED,
                Severity.WARNING,
                f"no quote for {self._cfg.instrument_id} in {self._cfg.stale_quote_s} s; entries blocked",
                None,
                {"last_quote_ns": None if quote is None else quote.ts_event},
            )
        elif not stale and self._blocks.clear(BlockReason.STALE_FEED):
            log.info("quotes fresh again", extra={"fields": {"code": "stale_feed_cleared"}})
            self._stale_since_ns = None

    # Helpers --------------------------------------------------------------------------------

    def _back_to_flat(self) -> None:
        for name in _ROUND_TRIP_TIMERS:
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

    def _equity(self, mid: Decimal) -> Decimal:
        account = self.portfolio.account(self._instrument_id.venue)
        if account is None:
            raise LookupError(f"no account for {self._instrument_id.venue}")
        balance = account.balance_total(USD)
        equity = Decimal(0) if balance is None else balance.as_decimal()
        position = self._position()
        if position is not None:
            equity += position.unrealized_pnl(self._instrument().make_price(mid)).as_decimal()
        return equity

    def _alert(self, name: str, at_ns: int, callback: Callable[[object], None]) -> None:
        self._cancel_timer(name)
        self.clock.set_time_alert_ns(name, at_ns, functools.partial(self._when_due, name, at_ns, callback))

    def _when_due(self, name: str, at_ns: int, callback: Callable[[object], None], event: object) -> None:
        # A live alert sleeps a fixed duration on the monotonic clock, which can run ahead of the
        # wall clock (0.6 % in the Docker Desktop VM), so it may wake early: sleep the rest.
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
        log.log(
            _LOG_LEVELS[severity],
            message,
            extra={"fields": {"code": kind.value, "event_key": key}},
        )
