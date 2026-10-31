"""``MakerBarRuleStrategy``: ``BarRuleStrategy`` with maker entries and maker planned exits (spec 0026).

The base for maker variants of the bar rule candidates. It starts as a copy of the frozen ``bar_rule``
module (bar loop, ATR, quote and bar subscriptions, stale quote check, stop submit and accept timer,
``STOP_NOT_ACCEPTED``, exit retry, reset on any close) and adds, from ``ScalpStrategy``, the maker entry,
its wait, partial fills, and the late fill close. Frozen once spec 0026 is Accepted: a later change goes
into a new module (until then demo findings may still change it).

On each bar close, in this order: an entry still resting ``entry_wait_bars`` bar closes after it was
submitted has its remainder cancelled, and an exit limit resting ``exit_wait_bars`` bar closes is replaced
by a market order (AC-4, AC-8); the stop follows the position's quantity again if a modify was refused;
then the ATR and the rule are updated (always), the exit step runs in a position, the entry step while
flat, and otherwise a signal is consumed and dropped. At most one decision per bar close, and nothing is
decided before the rule and the ATR are ready.

An approved entry is a post only GTC limit ``entry_offset_atr`` ATR behind the signal bar's close, sized
by ``check_entry`` from that price (AC-2, AC-3). Its first fill rests the reduce only stop market order
(or exits at once as ``stop_through``); each later fill moves the stop to the position's quantity (AC-5).
A post only rejection drops the signal quietly; any other refusal raises the base's risk event (AC-6).
A planned exit (``time_stop`` or the rule's) rests a post only, reduce only limit at the passive touch
while the stop keeps resting, and falls back to market on its wait, on any refusal, or on a cancel this
strategy did not ask for (AC-7, AC-8). Every other exit is market (AC-9). ``_pre_entry`` is the
subclass hook for entry filters, called once the gate and the blocks are clear and before sizing.
"""

import functools
import logging
import uuid
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import OrderStatus as NautilusOrderStatus
from nautilus_trader.model.enums import OrderType, TimeInForce, TriggerType
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderModifyRejected,
    OrderRejected,
    PositionClosed,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.node import guard
from trading_core.node.bar_rule import bar_interval_ns
from trading_core.node.guard import GuardedConfig
from trading_core.node.stale import StaleQuoteBlock
from trading_core.node.strategy import KfsStrategy, PauseGate, decision_of
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
LATE_ENTRY_FILL: Final = "late_entry_fill"
BAD_SIGNAL: Final = "bad_signal"
NS_PER_S: Final = 1_000_000_000
STOP_ACCEPT_TIMEOUT_NS: Final = 5 * NS_PER_S
EXIT_RETRY_NS: Final = NS_PER_S
# The wait for the cancel of a timed out exit limit before its market order goes anyway (AC-8).
EXIT_CANCEL_GUARD_NS: Final = STOP_ACCEPT_TIMEOUT_NS
USD: Final = Currency.from_str("USD")

_STALE: Final = "maker_bar_rule_stale_check"
_STOP_ACCEPT: Final = "maker_bar_rule_stop_accept"
_EXIT_RETRY: Final = "maker_bar_rule_exit_retry"
_EXIT_CANCEL: Final = "maker_exit_cancel"
_TIMERS: Final = (_STOP_ACCEPT, _EXIT_RETRY, _EXIT_CANCEL)
_LOG_LEVELS: Final = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}

type Record = Callable[[WriteItem], None]
type FallbackWhy = Literal["timeout", "post_only_rejected", "rejected", "canceled"]


class Phase(StrEnum):
    """Where the round trip is (the same values as the frozen base's ``Phase``)."""

    FLAT = "flat"
    ENTERING = "entering"
    PROTECTED = "protected"
    EXITING = "exiting"


class MakerBarRuleConfig(GuardedConfig, frozen=True, kw_only=True):
    """``BarRuleConfig``'s settings plus the maker ones (spec 0026 *Maker base*).

    Bad values are refused here with ``ValueError``; a candidate's config adds its own checks in
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
    entry_offset_atr: Decimal = Decimal("0.5")
    entry_wait_bars: int = 3
    exit_wait_bars: int = 3
    atr_min_bps: Decimal | None = None

    def __post_init__(self) -> None:
        """Refuse a bad bar, ATR, time stop, offset, wait, or floor setting."""
        bar_interval_ns(self.bar_spec)
        if self.atr_period < 2:
            raise ValueError(f"atr_period must be at least 2, got {self.atr_period}")
        if self.atr_floor_mult <= 0:
            raise ValueError(f"atr_floor_mult must be above 0, got {self.atr_floor_mult}")
        if self.time_stop_bars < 1:
            raise ValueError(f"time_stop_bars must be at least 1, got {self.time_stop_bars}")
        if self.entry_offset_atr < 0:
            raise ValueError(f"entry_offset_atr must be at least 0, got {self.entry_offset_atr}")
        if self.entry_wait_bars < 1:
            raise ValueError(f"entry_wait_bars must be at least 1, got {self.entry_wait_bars}")
        if self.exit_wait_bars < 1:
            raise ValueError(f"exit_wait_bars must be at least 1, got {self.exit_wait_bars}")
        if self.atr_min_bps is not None and self.atr_min_bps < 0:
            raise ValueError(f"atr_min_bps must be at least 0, got {self.atr_min_bps}")

    @property
    def bar_interval_ns(self) -> int:
        """One bar's length in nanoseconds, from ``bar_spec``."""
        return bar_interval_ns(self.bar_spec)


def entry_limit_px(side: Side, close: Decimal, atr: Decimal, offset_atr: Decimal, tick: Decimal) -> Decimal:
    """The entry limit ``offset_atr`` ATR behind ``close``, rounded to ``tick`` away from the market (AC-2).

    A long buys at ``close - offset_atr * atr`` rounded down; a short sells at ``close + offset_atr * atr``
    rounded up.
    """
    if side == Side.LONG:
        raw, rounding = close - offset_atr * atr, ROUND_FLOOR
    else:
        raw, rounding = close + offset_atr * atr, ROUND_CEILING
    return ((raw / tick).to_integral_value(rounding=rounding) * tick).quantize(tick)


def _side_of(position: Position) -> Side:
    return Side.LONG if position.side == NautilusPositionSide.LONG else Side.SHORT


def order_side(side: Side) -> OrderSide:
    """The order side that opens ``side``."""
    return OrderSide.BUY if side == Side.LONG else OrderSide.SELL


class MakerBarRuleStrategy(KfsStrategy, StaleQuoteBlock):
    """Trades a ``BarRule`` with maker entries and planned exits under a stop market (module docstring).

    A candidate's node module subclasses it to build its rule and may override ``_pre_entry``.
    """

    def __init__(
        self, config: MakerBarRuleConfig, gate: PauseGate, blocks: EntryBlocks, record: Record, rule: BarRule
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
        # Entries whose remainder was cancelled for an exit or a close: a fill of one is a late entry fill.
        # Outlives the round trip, as the fill can land after the position closed.
        self._retired: set[ClientOrderId] = set()
        self._reset()

    def _reset(self) -> None:
        self.phase = Phase.FLAT
        self.entry_decision: UUID | None = None
        self._exit_decision: UUID | None = None
        self._entry_id: ClientOrderId | None = None
        self._entry_bars = 0
        self._entry_cancel_asked = False  # the wait ran out (AC-4)
        self._stop_id: ClientOrderId | None = None
        self._exit_limit_id: ClientOrderId | None = None
        self._exit_bars = 0
        self._exit_cancel_asked = False  # the exit wait ran out; the market goes on the cancel (AC-8)
        self._exit_id: ClientOrderId | None = None  # the market close
        self._kill_switch_tags: list[str] | None = None
        self.bars_held = 0
        self._side: Side | None = None
        self._stop_px: Decimal | None = None
        self._entry_px: Decimal | None = None

    # The subclass hook ----------------------------------------------------------------------

    def _pre_entry(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> bool:
        """An entry filter: False after recording its refusal, so no order goes. The base passes everything."""
        return True

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
        """The quote is read from the cache at the bar close; nothing to do here."""

    def flatten(self, halt_id: UUID) -> None:
        """Cancel a resting entry and exit limit, then close the position at market for the kill switch (AC-11).

        Idempotent: a market close already sent is not sent again (a resting exit limit is not a close
        sent). During the exit limit's cancel guard the close goes at once. The stop rests until the
        position closes.
        """
        if self._position() is None:
            self._retire_entry()  # nothing filled: no tags, so none outlive this round trip
            return
        self._kill_switch_tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        self._retire_entry()
        if self._exit_id is not None:
            return
        self._cancel_exit_limit()
        self.phase = Phase.EXITING
        self._submit_exit()

    def resume_position(self, position: Position, stop_order: Order | None) -> None:
        """Take back ``position`` after a restart (AC-12): protected by ``stop_order`` at its trigger.

        Any resting limit of this strategy (an entry remainder or an exit) is cancelled with no decision.
        ``bars_held`` is the bar closes since the position opened; past ``time_stop_bars`` it exits at once
        at market. Without a stop it fails closed, as a stop not accepted.
        """
        for order in self.cache.orders_open(instrument_id=self._instrument_id, strategy_id=self.id):
            if order.order_type == OrderType.LIMIT and not order.is_pending_cancel:
                self.cancel_order(order)
        opening = self.cache.order(position.opening_order_id)
        self.entry_decision = decision_of(opening)
        self._entry_id = position.opening_order_id
        self._side = _side_of(position)
        self._entry_px = Decimal(str(position.avg_px_open))
        interval = self._cfg.bar_interval_ns
        self.bars_held = max(0, self.clock.timestamp_ns() // interval - position.ts_opened // interval)
        self.phase = Phase.PROTECTED
        if stop_order is None:
            self._stop_failed("no stop to resume with")
            return
        self._stop_id = stop_order.client_order_id
        self._stop_px = stop_order.trigger_price.as_decimal()
        if self.bars_held >= self._cfg.time_stop_bars:
            inputs: JsonObject = {"bars_held": self.bars_held, "stop": self._stop_px, "resumed": True}
            self._decide_exit(position, TIME_STOP, inputs, maker=False)

    # Bars -----------------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """The waits, then the ATR and the rule with every bar close, then the exit or the entry step (AC-1)."""
        if bar.bar_type != self.bar_type:
            return
        try:
            self._wait_step()
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

    def _wait_step(self) -> None:
        # Both waits count bar closes, not time, so the lab and live agree (AC-4, AC-8).
        entry = self._entry_order_open()
        if entry is not None and not self._entry_cancel_asked:
            self._entry_bars += 1
            if self._entry_bars >= self._cfg.entry_wait_bars:
                self._entry_cancel_asked = True
                self.cancel_order(entry)
        exit_limit = self._exit_limit()
        if exit_limit is not None and not self._exit_cancel_asked:
            self._exit_bars += 1
            if self._exit_bars >= self._cfg.exit_wait_bars:
                self._exit_cancel_asked = True
                if exit_limit.is_open and not exit_limit.is_pending_cancel:
                    self.cancel_order(exit_limit)
                self._alert(_EXIT_CANCEL, self.clock.timestamp_ns() + EXIT_CANCEL_GUARD_NS, self._on_exit_cancel_guard)
        if self._stop_id is not None:
            self._sync_stop()  # a modify refused at a fill is tried again here

    def _drop_signal(self) -> None:
        # A trigger while an order rests or in a position is consumed, never saved for later (AC-10).
        if self.rule.entry() is not None:
            self.rule.consumed()

    def _bar_inputs(self, bar: BarInput, atr: Decimal) -> JsonObject:
        return {"bar_close": bar.close, "atr": atr, **self.rule.values()}

    def _entry_step(self, bar: BarInput, atr: Decimal) -> None:
        signal = self.rule.entry()
        if signal is None:
            return
        self.rule.consumed()  # one bar event: whether approved, refused, or skipped
        if self.fresh_signal(bar.ts_ns):
            self._decide_entry(bar, atr, signal)

    def _decide_entry(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> None:
        quote = self.cache.quote_tick(self._instrument_id)
        if quote is None:
            log.warning("entry skipped, no quote", extra={"fields": {"code": "entry_skipped_no_quote"}})
            return
        # The base's own refusals first, so a halted, paused, or blocked trader records its real reason.
        clear = not (self._gate.halted or self._gate.paused or self._blocks.active())
        if clear and not self._pre_entry(bar, atr, signal):
            return
        instrument = self._instrument()
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        now = self.clock.timestamp_ns()
        side = order_side(signal.side)
        tick = instrument.price_increment.as_decimal()
        limit_px = entry_limit_px(signal.side, bar.close, atr, self._cfg.entry_offset_atr, tick)
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "mid": mid,
            "limit_px": limit_px,
            "entry_offset_atr": self._cfg.entry_offset_atr,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
            "structural_stop": signal.structural_stop,
        }
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
        inputs = {**inputs, "stop": stop, "floor_applied": floor_applied}
        if (limit_px <= stop) if signal.side == Side.LONG else (limit_px >= stop):
            log.warning(
                "entry limit not on the profit side of the stop",
                extra={"fields": {"code": "bar_rule_bad_signal", "reason": signal.reason}},
            )
            self._record_decision(uuid.uuid7(), Intent.ENTER, side, signal.reason, inputs, False, BAD_SIGNAL, now)
            return
        try:
            request = EntryRequest(
                side=side,
                mid=limit_px,  # the risk per trade is measured from where the order fills (AC-3)
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
        inputs = {**inputs, **verdict.figures.as_inputs()}
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
        order = self.order_factory.limit(
            self._instrument_id,
            NautilusOrderSide.BUY if side == OrderSide.BUY else NautilusOrderSide.SELL,
            instrument.make_qty(verdict.quantity),
            instrument.make_price(limit_px),
            time_in_force=TimeInForce.GTC,
            post_only=True,
            tags=[f"{DECISION_TAG}{decision}"],
        )
        self.phase = Phase.ENTERING
        self.entry_decision = decision
        self._entry_id = order.client_order_id
        self._entry_bars = 0
        self._side = signal.side
        self._stop_px = stop
        self.bars_held = 0
        self.submit_order(order)

    def _exit_step(self, bar: BarInput, atr: Decimal, position: Position) -> bool:
        # The time stop first, then the rule's own exit. True when an exit was decided.
        assert self._stop_px is not None  # set with the entry
        entry_px = self._entry_px if self._entry_px is not None else Decimal(str(position.avg_px_open))
        trade = TradeView(_side_of(position), entry_px, self._stop_px, self.bars_held)
        reason = TIME_STOP if self.bars_held >= self._cfg.time_stop_bars else self.rule.exit(trade)
        if reason is None:
            return False
        inputs: JsonObject = {**self._bar_inputs(bar, atr), "bars_held": self.bars_held, "stop": self._stop_px}
        self._decide_exit(position, reason, inputs, maker=True)
        return True

    def _decide_exit(self, position: Position, reason: str, inputs: JsonObject, *, maker: bool) -> None:
        # A planned exit (``maker``) rests a limit at the touch unless the quote is missing or stale (AC-7);
        # every other exit is market (AC-9). The stop keeps resting either way.
        decision = uuid.uuid7()
        now = self.clock.timestamp_ns()
        long = position.side == NautilusPositionSide.LONG
        closing = OrderSide.SELL if long else OrderSide.BUY
        inputs = {**inputs, "position_qty": position.quantity.as_decimal()}
        quote = self.cache.quote_tick(self._instrument_id)
        limit_px: Decimal | None = None
        if quote is not None:
            bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
            inputs = {**inputs, "mid": (bid + ask) / 2}
            stale = now - quote.ts_event > self._cfg.stale_quote_s * NS_PER_S
            if maker and stale:
                inputs = {**inputs, "why": "stale_quote"}
            elif maker:
                limit_px = ask if long else bid
        elif maker:
            inputs = {**inputs, "why": "no_quote"}
        if limit_px is not None:
            inputs = {**inputs, "exit_limit_px": limit_px, "exit_mode": "maker"}
        else:
            inputs = {**inputs, "exit_mode": "market"}
        self._record_decision(decision, Intent.EXIT, closing, reason, inputs, True, None, now)
        self._exit_decision = decision
        self.phase = Phase.EXITING
        self._retire_entry()
        if limit_px is not None:
            self._submit_exit_limit(position, limit_px)
        else:
            self._submit_exit()

    # Order and position events --------------------------------------------------------------

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """An accepted stop protects the position, and then follows any fill that came while it was in flight."""
        if event.client_order_id == self._stop_id:
            self._cancel_timer(_STOP_ACCEPT)
            if self.phase == Phase.ENTERING:
                self.phase = Phase.PROTECTED
            self._sync_stop()

    def on_order_filled(self, event: OrderFilled) -> None:
        """Entry fills rest or follow the stop (AC-5); a retired entry's fill closes at once (AC-10)."""
        if event.client_order_id in self._retired:
            self._close_late_fill(event)
        elif event.client_order_id == self._entry_id:
            self._on_entry_fill(event)
        elif event.client_order_id == self._exit_limit_id and self._position() is not None:
            self._sync_stop()  # a part filled exit limit: the stop shrinks to the rest

    def _on_entry_fill(self, event: OrderFilled) -> None:
        entry = self.cache.order(event.client_order_id)
        position = self._position()
        if entry is None or position is None:
            return
        fills = [(e.last_px.as_decimal(), e.last_qty.as_decimal()) for e in entry.events if isinstance(e, OrderFilled)]
        self._entry_px = weighted_avg(fills)
        if self._stop_id is not None or self.phase != Phase.ENTERING:
            self._sync_stop()
            return
        # The first fill: once, on its own price (later fills never trigger stop_through).
        assert self._stop_px is not None  # set with the entry
        first_px = event.last_px.as_decimal()
        through = first_px <= self._stop_px if self._side == Side.LONG else first_px >= self._stop_px
        if through:
            inputs: JsonObject = {"entry_px": first_px, "stop": self._stop_px, "bars_held": self.bars_held}
            if self._last_bar is not None:
                inputs = {"bar_close": self._last_bar.close, **inputs}
            self._decide_exit(position, STOP_THROUGH, inputs, maker=False)
            return
        self._submit_stop(position)

    def on_order_denied(self, event: OrderDenied) -> None:
        """A denial: see ``_refused`` (never a post only rejection)."""
        self._refused(event.client_order_id, f"denied: {event.reason}", post_only=False)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejection: see ``_refused``; the venue's post only flag decides the entry's and the exit's path."""
        self._refused(event.client_order_id, f"rejected: {event.reason}", post_only=bool(event.due_post_only))

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """A cancel: see ``_canceled``."""
        self._canceled(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """Like a cancel."""
        self._canceled(event.client_order_id, "expired")

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        """A refused stop modify is logged and tried again at the next fill or bar close (AC-5)."""
        if event.client_order_id == self._stop_id:
            log.info(
                "stop quantity not modified",
                extra={"fields": {"code": "stop_modify_rejected", "reason": str(event.reason)}},
            )

    def _refused(self, client_order_id: ClientOrderId, why: str, *, post_only: bool) -> None:
        position = self._position()
        if client_order_id == self._entry_id and self.phase == Phase.ENTERING and position is None:
            if post_only:
                log.info("maker entry rejected post only", extra={"fields": {"code": "maker_entry_rejected"}})
            else:
                self._entry_failed(client_order_id, why)
            self._back_to_flat()
        elif client_order_id == self._stop_id and position is not None and not self._closing_at_market():
            self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_limit_id:
            self._exit_limit_id = None
            self._fallback("post_only_rejected" if post_only else "rejected")
        elif client_order_id == self._exit_id:
            self._exit_refused(why)

    def _canceled(self, client_order_id: ClientOrderId, why: str) -> None:
        # Our own cancels: a closed position has reset every id before its cancels arrive; the exit wait's
        # cancel sends its market; an entry remainder this strategy cancelled ends the entry.
        position = self._position()
        if client_order_id == self._entry_id and position is None and self.phase == Phase.ENTERING:
            if self._entry_cancel_asked:
                log.info("maker entry timed out unfilled", extra={"fields": {"code": "maker_entry_timeout"}})
            elif not self._closing_in_progress():
                self._entry_failed(client_order_id, why)
            self._back_to_flat()
        elif client_order_id == self._stop_id and position is not None and not self._closing_at_market():
            if why == "canceled" and self._closing_in_progress():
                # Expected: the kill switch, or the runner's close or a shutdown (an in memory pause, no
                # pause key), cancelled it and closes the position itself. An operator pause still fails closed.
                log.info("stop cancelled by the close in progress", extra={"fields": {"code": "stop_canceled_closing"}})
                self._stop_id = None
                return
            self._stop_failed(f"stop {why}")
        elif client_order_id == self._exit_limit_id:
            self._exit_limit_id = None
            self._fallback("timeout" if self._exit_cancel_asked else "canceled")
        elif client_order_id == self._exit_id:
            self._exit_refused(why)

    def _closing_in_progress(self) -> bool:
        return self._gate.halted or (self._gate.paused and self._gate.state is None)

    def _closing_at_market(self) -> bool:
        return self._exit_id is not None

    def _entry_failed(self, client_order_id: ClientOrderId, why: str) -> None:
        self._risk_event(
            f"order_rejected:{client_order_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.WARNING,
            f"entry {why}",
            self.entry_decision,
            {"client_order_id": str(client_order_id)},
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        """Closed from any source: cancel what rests, reset the trade state and the rule's."""
        if event.instrument_id != self._instrument_id:
            return
        for order in self.cache.orders_open(strategy_id=self.id):
            if not order.is_reduce_only:
                self._retired.add(order.client_order_id)
            if not order.is_pending_cancel:
                self.cancel_order(order)
        self._back_to_flat()

    # Entry ----------------------------------------------------------------------------------

    def _entry_order_open(self) -> Order | None:
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        return (
            entry if entry is not None and not entry.is_closed and entry.client_order_id not in self._retired else None
        )

    def _retire_entry(self) -> None:
        """Cancel the entry's open remainder before an exit or a close; a fill that still comes closes at once."""
        entry = None if self._entry_id is None else self.cache.order(self._entry_id)
        if entry is None or entry.is_closed:
            return
        self._retired.add(entry.client_order_id)
        if not entry.is_pending_cancel:
            self.cancel_order(entry)
        if self._position() is None and self.phase == Phase.ENTERING:
            self._back_to_flat()  # nothing filled: the round trip ends here, its cancel matches nothing

    def _close_late_fill(self, event: OrderFilled) -> None:
        # The fill came after its remainder was cancelled for an exit: close just that quantity at market.
        decision = uuid.uuid7()
        bought = event.order_side == NautilusOrderSide.BUY
        closing = OrderSide.SELL if bought else OrderSide.BUY
        inputs: JsonObject = {
            "client_order_id": str(event.client_order_id),
            "fill_px": event.last_px.as_decimal(),
            "fill_qty": event.last_qty.as_decimal(),
        }
        now = self.clock.timestamp_ns()
        self._record_decision(decision, Intent.EXIT, closing, LATE_ENTRY_FILL, inputs, True, None, now)
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.SELL if bought else NautilusOrderSide.BUY,
            event.last_qty,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=self._kill_switch_tags or [f"{DECISION_TAG}{decision}"],
        )
        self.submit_order(order)

    # Stop -----------------------------------------------------------------------------------

    def _submit_stop(self, position: Position) -> None:
        if self._stop_px is None:
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
            tags=[f"{DECISION_TAG}{self.entry_decision}", f"{PARENT_TAG}{self._entry_id}"],
        )
        self._stop_id = order.client_order_id
        self._alert(_STOP_ACCEPT, self.clock.timestamp_ns() + STOP_ACCEPT_TIMEOUT_NS, self._on_stop_timeout)
        self.submit_order(order)

    def _sync_stop(self) -> None:
        # The stop follows the position's quantity (ScalpStrategy's rule): skipped while in flight, triggered,
        # or already being changed; a stop no longer open fails closed, unless a market close is under way.
        position = self._position()
        stop = self._stop_order()
        if position is None or stop is None or stop.is_inflight:
            return
        if stop.is_closed:
            if not self._closing_at_market():
                self._stop_failed("stop no longer open")
            return
        if stop.status == NautilusOrderStatus.TRIGGERED or stop.is_pending_update or stop.is_pending_cancel:
            return
        if stop.quantity != position.quantity:
            self.modify_order(stop, quantity=position.quantity)

    def _on_stop_timeout(self, event: object) -> None:
        if self._closing_at_market():
            return  # a close is under way (AC-11)
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
        self._blocks.add(BlockReason.STOP_NOT_ACCEPTED)
        self._risk_event(
            f"stop_not_accepted:{self._stop_id}",
            RiskEventKind.ORDER_REJECTED,
            Severity.CRITICAL,
            f"{why}; closing the position",
            self.entry_decision,
            {"client_order_id": str(self._stop_id)},
        )
        self._stop_id = None  # forgotten, so no later event or bar close fails it again
        self.phase = Phase.EXITING
        self._retire_entry()
        if self._closing_at_market():
            return
        self._cancel_exit_limit()
        self._submit_exit()

    # Exit -----------------------------------------------------------------------------------

    def _submit_exit_limit(self, position: Position, limit_px: Decimal) -> None:
        instrument = self._instrument()
        long = position.side == NautilusPositionSide.LONG
        order = self.order_factory.limit(
            self._instrument_id,
            NautilusOrderSide.SELL if long else NautilusOrderSide.BUY,
            position.quantity,
            instrument.make_price(limit_px),
            time_in_force=TimeInForce.GTC,
            post_only=True,
            reduce_only=True,
            tags=[f"{DECISION_TAG}{self._exit_decision}"],
        )
        self._exit_limit_id = order.client_order_id
        self._exit_bars = 0
        self._exit_cancel_asked = False
        self.submit_order(order)

    def _exit_limit(self) -> Order | None:
        order = None if self._exit_limit_id is None else self.cache.order(self._exit_limit_id)
        return None if order is None or order.is_closed else order

    def _cancel_exit_limit(self) -> None:
        # Forgotten here, so its cancel event matches nothing; both it and the market are reduce only.
        order = self._exit_limit()
        self._exit_limit_id = None
        self._cancel_timer(_EXIT_CANCEL)
        if order is not None and order.is_open and not order.is_pending_cancel:
            self.cancel_order(order)

    def _on_exit_cancel_guard(self, event: object) -> None:
        if self._exit_limit_id is not None and self._exit_cancel_asked:
            self._exit_limit_id = None
            self._fallback("timeout")

    def _fallback(self, why: FallbackWhy) -> None:
        # The exit limit is gone or going: the rest closes at market, under the same exit decision (AC-8).
        self._cancel_timer(_EXIT_CANCEL)
        if self._closing_at_market() or self._position() is None:
            return
        log.info(
            "exit limit replaced by market",
            extra={"fields": {"code": "exit_fallback_market", "why": why}},
        )
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
