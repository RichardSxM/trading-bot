"""``ScalpStrategy``: the shared entry and exit framework of the scalping candidates (spec 0017 AC-5).

A child feeds each closed ``<instrument>-<bar_spec>-INTERNAL`` bar to its pure state in
``signal_on_bar`` and returns a ``ScalpSignal`` (or None). While flat with no entry working, the base
passes a signal through ``pre_refusal`` (the gate and entry blocks, the spread cap, the reward floor,
the stop side) and then ``check_entry``, which sizes it from ``stop_px`` under the run's caps. Every
signal it acts on is recorded as an ``enter`` decision, approved or rejected.

An approved entry is a post only limit at the last quote's touch (``maker``) or a market order
(``taker``). A maker entry still open ``entry_timeout_s`` after it was submitted has its remainder
cancelled; any filled part is kept. While a position is open, one reduce only stop market order at
``stop_px`` follows the position's quantity. The target exit fires on the first quote that reaches
``target_px``; the time stop ``time_stop_min`` after the first entry fill. Each exit is a reduce only
market order with an ``exit`` decision (``target``, ``time_stop``, or ``stop`` for a stop fill).
Before any exit, the entry's open remainder is cancelled; an entry fill that still arrives after
that is closed at once (``late_entry_fill``). A closed position cancels whatever rests.

A stop that is refused or not accepted in time blocks entries and closes the position, as in
``MacdBaseline`` (fail closed). A stop already through at the first fill is never submitted: a
``stop_through`` exit closes the position with no block (AC-18). A venue rejection is settled by
the next quote, the one the venue already judged the stop by: through means ``stop_through`` with
no block, otherwise the block. Entries stay refused until then.

With ``regime_adx_max`` set, the base also feeds ``<instrument>-<regime_bar_spec>-INTERNAL`` bars to an
``Adx(regime_adx_period)`` (AC-6): ``regime`` blocks entries from the start until the ADX is warm and
whenever it is above the limit, and clears at or below it and on stop.
"""

import dataclasses
import functools
import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal
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
    OrderModifyRejected,
    OrderRejected,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency, Quantity
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
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.scalp import (
    EXIT_LATE_ENTRY_FILL,
    EXIT_STOP,
    EXIT_STOP_THROUGH,
    EXIT_TARGET,
    EXIT_TIME_STOP,
    MINUTE_NS,
    SCALP_REFUSALS,
    EntryGate,
    ScalpSignal,
    mid_of,
    pre_refusal,
    round_stop,
    spread_bps,
    stop_through,
    target_hit,
)
from trading_core.strategy.scalp_indicators import Adx

log = logging.getLogger(__name__)

NS_PER_S: Final = 1_000_000_000
STOP_ACCEPT_TIMEOUT_NS: Final = 5 * NS_PER_S
EXIT_RETRY_NS: Final = NS_PER_S
USD: Final = Currency.from_str("USD")

_STALE: Final = "scalp_stale_check"
_ENTRY_TIMEOUT: Final = "scalp_entry_timeout"
_TIME_STOP: Final = "scalp_time_stop"
_STOP_ACCEPT: Final = "scalp_stop_accept"
_EXIT_RETRY: Final = "scalp_exit_retry"
_TIMERS: Final = (_ENTRY_TIMEOUT, _TIME_STOP, _STOP_ACCEPT, _EXIT_RETRY)
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}

type Record = Callable[[WriteItem], None]


@dataclasses.dataclass(frozen=True, slots=True)
class _RejectedStop:
    """A rejected stop awaiting the next quote's verdict (AC-18)."""

    stop_id: ClientOrderId
    long: bool
    stop_px: Decimal
    why: str
    entry_decision: UUID | None


class Phase(StrEnum):
    """Where the round trip is."""

    FLAT = "flat"
    ENTERING = "entering"
    HOLDING = "holding"
    EXITING = "exiting"


class ScalpConfig(GuardedConfig, frozen=True, kw_only=True):
    """The settings every scalping candidate shares (spec 0017, *ScalpConfig fields*), plus the run's."""

    env: str
    run_id: str
    time_stop_min: int
    bar_spec: str = "1-MINUTE-LAST"
    stale_quote_s: Decimal = Decimal(300)
    stale_check_s: int = 60
    entry_mode: Literal["maker", "taker"] = "maker"
    entry_timeout_s: int = 30
    max_spread_bps: Decimal | None = None
    min_target_bps: Decimal | None = None
    regime_adx_max: Decimal | None = None
    regime_adx_period: int = 14
    regime_bar_spec: str = "5-MINUTE-LAST"
    use_market_context: bool = True


def _refusal_severity(reason: str) -> Severity:
    return Severity.INFO if reason in SCALP_REFUSALS else refusal_severity(reason)


class ScalpStrategy(KfsStrategy, StaleQuoteBlock):
    """Enters on a child's signal, protected by a stop, out at the target or the time stop (module docstring)."""

    def __init__(self, config: ScalpConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate)
        if config.time_stop_min < 1:
            raise ValueError(f"time_stop_min must be at least 1, got {config.time_stop_min}")
        if config.entry_timeout_s < 1:
            raise ValueError(f"entry_timeout_s must be at least 1, got {config.entry_timeout_s}")
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(f"{config.instrument_id}-{config.bar_spec}-INTERNAL")
        self._limits = config.risk_limits()
        self.regime_bar_type: BarType | None = None
        self.regime_adx: Adx | None = None
        if config.regime_adx_max is not None:
            self.regime_bar_type = BarType.from_str(f"{config.instrument_id}-{config.regime_bar_spec}-INTERNAL")
            self.regime_adx = Adx(config.regime_adx_period)
        # Entries whose remainder was cancelled for an exit: a fill of one is a late entry fill.
        self._retired: set[ClientOrderId] = set()
        # Outlives the round trip: the verdict can land after the exit made the strategy flat.
        self._rejected_stop: _RejectedStop | None = None
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self.entry_decision: UUID | None = None
        self._exit_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._stop_id: ClientOrderId | None = None
        self._exit_id: ClientOrderId | None = None
        self._signal: ScalpSignal | None = None
        self._first_fill_ns: int | None = None
        self._kill_switch_tags: list[str] | None = None

    # The child's hook -----------------------------------------------------------------------

    def signal_on_bar(self, bar: Bar) -> ScalpSignal | None:
        """Feed one closed bar to the child's state; return its signal, or None (also while not warm)."""
        raise NotImplementedError

    def decision_inputs(self) -> JsonObject:
        """Extra inputs every ``enter`` decision carries: the regime's ADX, when the gate is set."""
        if self.regime_adx is None:
            return {}
        return {"adx": self.regime_adx.value}

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to quotes and the strategy's bars, then arm the stale quote check."""
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
        if self.regime_bar_type is not None:
            self._blocks.add(BlockReason.REGIME)  # until the ADX is warm
            self.subscribe_bars(self.regime_bar_type)

    def on_stop(self) -> None:
        """The base's stop (pause in memory, cancel entries), then lift ``regime``.

        A rejected stop no quote has settled yet is recorded as not accepted (fail closed).
        """
        super().on_stop()
        rejected, self._rejected_stop = self._rejected_stop, None
        if rejected is not None:
            self._block_stop_not_accepted(rejected.stop_id, rejected.why, rejected.entry_decision)
        if self.regime_bar_type is not None:
            self._blocks.clear(BlockReason.REGIME)

    def flatten(self, halt_id: UUID) -> None:
        """Cancel the entry's remainder and close the position at market for the kill switch (spec 0010 AC-1).

        Idempotent: a close already sent is not sent again. The stop rests until the position closes.
        """
        self._retire_entry()
        self._kill_switch_tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        if self._position() is None or self._exit_id is not None:
            return
        self.phase = Phase.EXITING
        self._submit_exit()

    # Bars and quotes ------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Feed every bar to the child; act on its signal only while flat with no entry working."""
        if self.regime_bar_type is not None and bar.bar_type == self.regime_bar_type:
            self._on_regime_bar(bar)
            return
        if bar.bar_type != self.bar_type:
            return
        try:
            signal = self.signal_on_bar(bar)
            if signal is not None and self.phase == Phase.FLAT and self._position() is None:
                self._decide_entry(signal)
        except Exception:
            # Fail closed: no new order this bar; a position keeps its stop.
            log.exception("bar decision failed", extra={"fields": {"code": "bar_decision_failed"}})

    def _on_regime_bar(self, bar: Bar) -> None:
        limit = self._cfg.regime_adx_max
        if self.regime_adx is None or limit is None:  # set together in __init__
            return
        adx = self.regime_adx.update(bar.high.as_decimal(), bar.low.as_decimal(), bar.close.as_decimal())
        if adx is None or adx > limit:
            self._blocks.add(BlockReason.REGIME)
        elif self._blocks.clear(BlockReason.REGIME):
            log.debug("regime calm again", extra={"fields": {"code": "regime_cleared", "adx": str(adx)}})

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """The verdict on a rejected stop, then the target exit: the first quote that reaches ``target_px``."""
        if self._rejected_stop is not None and tick.instrument_id == self._instrument_id:
            self._settle_rejected_stop(tick)
        if self.phase != Phase.HOLDING or self._signal is None or tick.instrument_id != self._instrument_id:
            return
        position = self._position()
        if position is None:
            return
        long = position.side == NautilusPositionSide.LONG
        if target_hit(long, tick.bid_price.as_decimal(), tick.ask_price.as_decimal(), self._signal.target_px):
            self._decide_exit(EXIT_TARGET, position)

    # Entry ----------------------------------------------------------------------------------

    def _decide_entry(self, signal: ScalpSignal) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        instrument = self._instrument()
        tick = instrument.price_increment.as_decimal()
        signal = dataclasses.replace(signal, stop_px=round_stop(signal.side, signal.stop_px, tick))
        bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        mid = mid_of(bid, ask)
        now = self.clock.timestamp_ns()
        decision = uuid.uuid7()
        inputs: JsonObject = {
            **signal.inputs,
            **self.decision_inputs(),
            "mid": mid,
            "spread_bps": spread_bps(bid, ask),
            "stop_px": signal.stop_px,
            "target_px": signal.target_px,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
        }
        refusal = pre_refusal(
            signal,
            EntryGate(
                halted=self._gate.halted,
                paused=self._gate.paused,
                blocks=self._entry_blocks(),
                bid=bid,
                ask=ask,
                max_spread_bps=self._cfg.max_spread_bps,
                min_target_bps=self._cfg.min_target_bps,
            ),
        )
        if refusal is not None:
            self._record_decision(decision, Intent.ENTER, signal.side, inputs, False, refusal, now)
            self._refusal_event(decision, refusal)
            return
        try:
            request = EntryRequest(
                side=signal.side,
                mid=mid,
                equity=self._equity(),
                open_notional=guard.open_notional(self.cache),
                size_step=instrument.size_increment.as_decimal(),
                taker_fee=self._cfg.taker_fee,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                funding_periods=funding_periods(now, self._cfg.time_stop_min * MINUTE_NS),
                funding_rate=self._funding_rate(),
                stop_px=signal.stop_px,
                has_position=False,
                paused=self._gate.paused,
                halted=self._gate.halted,
                blocks=self._entry_blocks(),
            )
            verdict = check_entry(request, self._limits)
        except InvalidRiskInput:
            log.exception("risk check could not judge the entry", extra={"fields": {"code": "risk_input_invalid"}})
            return
        inputs = {**inputs, **verdict.figures.as_inputs()}
        self._record_decision(decision, Intent.ENTER, signal.side, inputs, verdict.approved, verdict.reason, now)
        if not verdict.approved:
            self._refusal_event(decision, verdict.reason or "")
            return
        if self._gate.halted or self._gate.paused:  # the gate again, right before submitting
            log.info("entry denied by the gate", extra={"fields": {"code": "entry_denied_gate"}})
            return
        order = self._entry_order(signal.side, instrument.make_qty(verdict.quantity), quote, decision)
        self.phase = Phase.ENTERING
        self.entry_decision = decision
        self._signal = signal
        self._entry_id = order.client_order_id
        self.submit_order(order)
        if self._cfg.entry_mode == "maker":
            self._alert(_ENTRY_TIMEOUT, now + self._cfg.entry_timeout_s * NS_PER_S, self._on_entry_timeout)

    def _entry_order(self, side: OrderSide, quantity: Quantity, quote: QuoteTick, decision: UUID) -> Order:
        buy = side == OrderSide.BUY
        nautilus_side = NautilusOrderSide.BUY if buy else NautilusOrderSide.SELL
        tags = [f"{DECISION_TAG}{decision}"]
        if self._cfg.entry_mode == "taker":
            return self.order_factory.market(
                self._instrument_id, nautilus_side, quantity, time_in_force=TimeInForce.GTC, tags=tags
            )
        return self.order_factory.limit(
            self._instrument_id,
            nautilus_side,
            quantity,
            quote.bid_price if buy else quote.ask_price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
            tags=tags,
        )

    def _on_entry_timeout(self, event: object) -> None:
        # The remainder goes; a filled part is kept (no taker chase).
        entry = self._entry_order_open()
        if entry is not None:
            self.cancel_order(entry)

    def _entry_order_open(self) -> Order | None:
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        return entry if entry is not None and entry.is_open and not entry.is_pending_cancel else None

    def _retire_entry(self) -> None:
        """Cancel the entry's open remainder before an exit; a fill that still comes is a late entry fill."""
        self._cancel_timer(_ENTRY_TIMEOUT)
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        if entry is None or entry.is_closed:
            return
        self._retired.add(entry.client_order_id)
        if not entry.is_pending_cancel:
            self.cancel_order(entry)

    # Order and position events --------------------------------------------------------------

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """An accepted stop protects the position."""
        if event.client_order_id == self._stop_id:
            self._cancel_timer(_STOP_ACCEPT)

    def on_order_filled(self, event: OrderFilled) -> None:
        """A retired entry's fill is closed at once; a stop fill is the ``stop`` exit."""
        if event.client_order_id in self._retired:
            self._close_late_fill(event)
        elif event.client_order_id == self._stop_id and self._exit_decision is None:
            position = self._position()
            closing = OrderSide.SELL if event.order_side == NautilusOrderSide.SELL else OrderSide.BUY
            self._exit_decision = uuid.uuid7()
            inputs: JsonObject = {"fill_px": event.last_px.as_decimal(), "fill_qty": event.last_qty.as_decimal()}
            now = self.clock.timestamp_ns()
            self._record_decision(self._exit_decision, Intent.EXIT, closing, inputs, True, None, now, EXIT_STOP)
            self.phase = Phase.EXITING
            self._retire_entry()
            if position is None:
                self._back_to_flat()

    def on_position_opened(self, event: PositionOpened) -> None:
        """The first entry fill: rest the stop and arm the time stop."""
        if event.instrument_id != self._instrument_id or self.phase != Phase.ENTERING:
            return
        self._first_fill_ns = event.ts_event
        self.phase = Phase.HOLDING
        self._alert(_TIME_STOP, event.ts_event + self._cfg.time_stop_min * MINUTE_NS, self._on_time_stop)
        self._sync_stop()

    def on_position_changed(self, event: PositionChanged) -> None:
        """A later entry fill: the stop follows the position's quantity."""
        if event.instrument_id == self._instrument_id and self.phase == Phase.HOLDING:
            self._sync_stop()

    def on_position_closed(self, event: PositionClosed) -> None:
        """Closed from any source: cancel what rests and start over."""
        if event.instrument_id != self._instrument_id:
            return
        for order in self.cache.orders_open(strategy_id=self.id):
            if not order.is_reduce_only:
                self._retired.add(order.client_order_id)
            if not order.is_pending_cancel:
                self.cancel_order(order)
        self._back_to_flat()

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        """A stop that already triggered cannot follow a late fill; logged, not retried."""
        if event.client_order_id == self._stop_id:
            log.info(
                "stop quantity not modified",
                extra={"fields": {"code": "stop_modify_rejected", "reason": str(event.reason)}},
            )

    def on_order_denied(self, event: OrderDenied) -> None:
        """A denied entry goes back to flat; a denied stop or exit: see ``_refused``."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejected entry goes back to flat; a rejected stop or exit: see ``_refused``."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """An unfilled entry cancelled at its timeout ends the round trip; others: see ``_refused``."""
        self._refused(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """Like a cancel."""
        self._refused(event.client_order_id, "expired")

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        position = self._position()
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING and position is None:
            if why == "canceled":
                log.debug("maker entry timed out unfilled", extra={"fields": {"code": "maker_entry_timeout"}})
            else:
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
                # The kill switch, or the runner's close or a shutdown, cancelled it and closes the position.
                log.info("stop cancelled by the close in progress", extra={"fields": {"code": "stop_canceled_closing"}})
                self._stop_id = None
                return
            if why.startswith("rejected"):
                self._stop_rejected(f"stop {why}")
            else:
                self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_id:
            self._exit_refused(why)

    # Stop -----------------------------------------------------------------------------------

    def _sync_stop(self) -> None:
        position = self._position()
        if position is None or self._signal is None:
            return
        stop = self._stop_order()
        if stop is None:
            if self._stop_through(position):
                return
            self._submit_stop(position)
        elif stop.is_open and stop.quantity != position.quantity and stop.status != NautilusOrderStatus.TRIGGERED:
            self.modify_order(stop, quantity=position.quantity)

    def _stop_through(self, position: Position) -> bool:
        # AC-18: a stop already in the market would be rejected and block entries until a restart,
        # so exit at market instead, with no block.
        assert self._signal is not None  # set with the entry
        quote = self.cache.quote_tick(self._instrument_id)
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        fills = (
            []
            if entry is None
            else [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in entry.events if isinstance(e, OrderFilled)]
        )
        avg_px = weighted_avg(fills)
        if quote is None or avg_px is None:
            return False
        bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        long = position.side == NautilusPositionSide.LONG
        if not stop_through(long, bid, ask, avg_px, self._signal.stop_px):
            return False
        log.info("stop already through at the fill; exiting", extra={"fields": {"code": EXIT_STOP_THROUGH}})
        self._decide_exit(
            EXIT_STOP_THROUGH, position, {"bid": bid, "ask": ask, "avg_px": avg_px, "stop_px": self._signal.stop_px}
        )
        return True

    def _submit_stop(self, position: Position) -> None:
        assert self._signal is not None  # set with the entry
        instrument = self._instrument()
        long = position.side == NautilusPositionSide.LONG
        order = self.order_factory.stop_market(
            self._instrument_id,
            NautilusOrderSide.SELL if long else NautilusOrderSide.BUY,
            position.quantity,
            trigger_price=instrument.make_price(self._signal.stop_px),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{self.entry_decision}", f"{PARENT_TAG}{self._entry_id}"],
        )
        self._stop_id = order.client_order_id
        self._alert(_STOP_ACCEPT, self.clock.timestamp_ns() + STOP_ACCEPT_TIMEOUT_NS, self._on_stop_timeout)
        self.submit_order(order)

    def _on_stop_timeout(self, event: object) -> None:
        stop = self._stop_order()
        if stop is not None and stop.status in (
            NautilusOrderStatus.ACCEPTED,
            NautilusOrderStatus.TRIGGERED,
            NautilusOrderStatus.PENDING_UPDATE,
            NautilusOrderStatus.PARTIALLY_FILLED,
            NautilusOrderStatus.FILLED,
        ):
            return
        if self._position() is None:
            return
        self._stop_failed("stop not accepted in time")

    def _stop_failed(self, why: str) -> None:
        self._cancel_timer(_STOP_ACCEPT)
        self._block_stop_not_accepted(self._stop_id, why, self.entry_decision)
        self.phase = Phase.EXITING
        self._retire_entry()
        self._submit_exit()

    def _block_stop_not_accepted(self, stop_id: ClientOrderId | None, why: str, decision: UUID | None) -> None:
        self._blocks.add(BlockReason.STOP_NOT_ACCEPTED)
        self._risk_event(
            f"stop_not_accepted:{stop_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.CRITICAL,
            f"{why}; closing the position",
            decision,
            {"client_order_id": str(stop_id)},
        )

    def _stop_rejected(self, why: str) -> None:
        # The venue may judge the stop by a quote this strategy has not seen yet (in a backtest it always
        # sees data first), so the next quote settles it; the position closes now either way.
        assert self._signal is not None  # set with the entry
        assert self._stop_id is not None  # set with the stop
        position = self._position()
        self._cancel_timer(_STOP_ACCEPT)
        self._rejected_stop = _RejectedStop(
            self._stop_id,
            position is not None and position.side == NautilusPositionSide.LONG,
            self._signal.stop_px,
            why,
            self.entry_decision,
        )
        self.phase = Phase.EXITING
        self._retire_entry()
        self._submit_exit()

    def _settle_rejected_stop(self, tick: QuoteTick) -> None:
        rejected = self._rejected_stop
        assert rejected is not None  # checked by the caller
        self._rejected_stop = None
        bid, ask = tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
        if not self._rejected_stop_through(rejected, bid, ask):
            self._block_stop_not_accepted(rejected.stop_id, rejected.why, rejected.entry_decision)
            return
        log.info("rejected stop was already through; exiting", extra={"fields": {"code": EXIT_STOP_THROUGH}})
        inputs: JsonObject = {"bid": bid, "ask": ask, "stop_px": rejected.stop_px, "mid": mid_of(bid, ask)}
        closing = OrderSide.SELL if rejected.long else OrderSide.BUY
        now = self.clock.timestamp_ns()
        self._record_decision(uuid.uuid7(), Intent.EXIT, closing, inputs, True, None, now, EXIT_STOP_THROUGH)

    def _rejected_stop_through(self, rejected: _RejectedStop, bid: Decimal, ask: Decimal) -> bool:
        # Only the quote decides here: the fill price was already checked before the stop went out.
        return bid <= rejected.stop_px if rejected.long else ask >= rejected.stop_px

    def _entry_blocks(self) -> list[str]:
        blocks = [str(r) for r in self._blocks.active()]
        if self._rejected_stop is not None and BlockReason.STOP_NOT_ACCEPTED not in self._blocks.active():
            blocks.append(str(BlockReason.STOP_NOT_ACCEPTED))  # until the next quote settles it
        return sorted(blocks)

    # Exit -----------------------------------------------------------------------------------

    def _on_time_stop(self, event: object) -> None:
        position = self._position()
        if position is not None and self.phase == Phase.HOLDING:
            self._decide_exit(EXIT_TIME_STOP, position)

    def _decide_exit(self, reason: str, position: Position, extra: JsonObject | None = None) -> None:
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        closing = OrderSide.SELL if position.side == NautilusPositionSide.LONG else OrderSide.BUY
        inputs: JsonObject = {
            "position_qty": position.quantity.as_decimal(),
            "first_fill_ns": self._first_fill_ns,
            **(extra or {}),
        }
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is not None:
            inputs = {**inputs, "mid": mid_of(quote.bid_price.as_decimal(), quote.ask_price.as_decimal())}
        self._record_decision(decision, Intent.EXIT, closing, inputs, True, None, now, reason)
        self._exit_decision = decision
        self.phase = Phase.EXITING
        self._retire_entry()
        self._submit_exit()

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

    def _close_late_fill(self, event: OrderFilled) -> None:
        # The fill came after its remainder was cancelled for an exit: close just that quantity.
        decision = uuid.uuid7()
        bought = event.order_side == NautilusOrderSide.BUY
        closing = OrderSide.SELL if bought else OrderSide.BUY
        inputs: JsonObject = {
            "client_order_id": str(event.client_order_id),
            "fill_px": event.last_px.as_decimal(),
            "fill_qty": event.last_qty.as_decimal(),
        }
        now = self.clock.timestamp_ns()
        self._record_decision(decision, Intent.EXIT, closing, inputs, True, None, now, EXIT_LATE_ENTRY_FILL)
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.SELL if bought else NautilusOrderSide.BUY,
            event.last_qty,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=self._kill_switch_tags or [f"{DECISION_TAG}{decision}"],
        )
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

    def _refusal_event(self, decision: UUID, reason: str) -> None:
        self._risk_event(
            f"limit_hit:{decision}",
            RiskEventKind.LIMIT_HIT,
            _refusal_severity(reason),
            f"entry refused: {reason}",
            decision,
            {"reason": reason},
        )

    def _record_decision(
        self,
        decision: UUID,
        intent: Intent,
        side: OrderSide,
        inputs: JsonObject,
        approved: bool,
        risk_reason: str | None,
        now_ns: int,
        reason: str | None = None,
    ) -> None:
        row = DecisionRow(
            id=decision,
            env=self._env,
            run_id=self._run_id,
            instrument_id=self._cfg.instrument_id,
            intent=intent,
            side=side,
            reason=reason or self.entry_reason,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED if approved else RiskVerdict.REJECTED,
            risk_reason=risk_reason,
            decided_ns=now_ns,
        )
        self._record(WriteItem(WriteCall.RECORD_DECISION, row))

    @property
    def entry_reason(self) -> str:
        """The ``reason`` of this strategy's ``enter`` decisions (the child's name plus ``_entry``)."""
        raise NotImplementedError

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
