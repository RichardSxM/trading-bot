"""``TrendPortfolio``: the weekly 30 day trend portfolio across every coin, one instance (spec 0030 AC-3 to AC-9).

One strategy trades all coins with one ``order_id_tag``. It keeps each coin's daily closes (the warmup seed,
then ``1-DAY-LAST-INTERNAL`` bars built from trades), and every day at ``run_at_utc`` it plans from the
closes up to the day that just ended (``trading_core.strategy.trend_portfolio``), logs ``portfolio_targets``
for each coin, and stores the plan for the ``portfolio_plan`` snapshot. On ``rebalance_weekday`` it also
rebalances, at most once per date. It rebalances at start too (once the seed is in, the pause key is read,
the start check is clean, and the quotes are in, unless a ``start`` rebalance was recorded within
``start_cooldown_s``), and when the pause gate clears after a pause or halt (``resume``).

A rebalance sends every reducing order first (reduce only market orders), then, once those are done,
every order that opens or grows, each through ``check_rebalance``. A flip closes the whole position reduce
only, then opens the new side sized again at the mid when the close fills. While paused, halted, or blocked,
opening and growing orders are held (reductions still go). A rejected, denied, or stale quote order is
retried for that coin up to ``retry_max`` times ``retry_wait_s`` apart, then the coin lands in ``failed``
until the next rebalance. Positions carry no protective stop (``uses_stop`` False, spec 0023 AC-9).
"""

import functools
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Final
from uuid import UUID

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
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
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import Env, Intent, OrderOrigin, OrderSide, RiskVerdict
from trading_core.recording.mappers import DECISION_TAG, HALT_TAG, ORIGIN_TAG
from trading_core.recording.rows import DecisionRow, JsonObject
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.check import InvalidRiskInput
from trading_core.risk.rebalance import (
    LIQ_DISTANCE,
    STALE_QUOTE,
    InvalidRebalanceInput,
    RebalanceRequest,
    check_rebalance,
)
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.trend_portfolio import (
    DailyClose,
    PortfolioError,
    PortfolioParams,
    PortfolioPlan,
    close_day,
    order_delta,
    portfolio_plan,
    target_quantity,
)

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "trend_portfolio"
ORDER_ID_TAG: Final = "TP"
BAR_SPEC: Final = "1-DAY-LAST"
NS_PER_S: Final = 1_000_000_000
USD: Final = Currency.from_str("USD")

WEEKLY: Final = "weekly"
START: Final = "start"
RESUME: Final = "resume"
HELD_PREFIX: Final = "held:"

_RUN: Final = "tp_daily_run"
_START: Final = "tp_start_wait"
_GATE: Final = "tp_gate_watch"
_GROWS: Final = "tp_grows_fallback"
_RETRY_PREFIX: Final = "tp_retry:"
_HHMM: Final = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_POLL_NS: Final = NS_PER_S

type Record = Callable[[WriteItem], None]


class Leg(StrEnum):
    """What one rebalance order does to its coin."""

    REDUCE = "reduce"
    FLIP_CLOSE = "flip_close"
    GROW = "grow"


class TrendPortfolioConfig(GuardedConfig, frozen=True, kw_only=True):
    """The portfolio's settings (AC-3): the coins, the rule's parameters, the schedule, retries, and seed sizes.

    ``instrument_id`` is BTC, the base's context instrument only. Bad values are refused here with
    ``ValueError``.
    """

    env: str
    run_id: str
    instruments: tuple[str, ...]
    lookback_days: int = 30
    vol_days: int = 30
    vol_target: Decimal = Decimal("0.02")
    gross_cap: Decimal = Decimal(2)
    min_history_days: int = 120
    rebalance_weekday: int = 0
    run_at_utc: str = "00:05"
    retry_max: int = 3
    retry_wait_s: int = 60
    quote_wait_s: int = 60
    start_cooldown_s: int = 3600
    seed_days: int = 150
    seed_min_days: int = 121
    stale_quote_s: Decimal = Decimal(10)

    def __post_init__(self) -> None:
        """Refuse a bad rule parameter, weekday, run time, retry, cooldown, or coin list."""
        self.params()  # the rule's own checks
        if not 0 <= self.rebalance_weekday <= 6:
            raise ValueError(f"rebalance_weekday must be 0 to 6, got {self.rebalance_weekday}")
        if not _HHMM.fullmatch(self.run_at_utc):
            raise ValueError(f"run_at_utc must be HH:MM, got {self.run_at_utc!r}")
        if self.retry_max < 0:
            raise ValueError(f"retry_max must be at least 0, got {self.retry_max}")
        if self.retry_wait_s < 1:
            raise ValueError(f"retry_wait_s must be at least 1, got {self.retry_wait_s}")
        if self.start_cooldown_s < 0:
            raise ValueError(f"start_cooldown_s must be at least 0, got {self.start_cooldown_s}")
        if not self.instruments:
            raise ValueError("instruments must not be empty")
        if self.instrument_id not in self.instruments:
            raise ValueError(f"instruments must hold {self.instrument_id}")

    def params(self) -> PortfolioParams:
        """The rule's parameters."""
        return PortfolioParams(
            lookback_days=self.lookback_days,
            vol_days=self.vol_days,
            vol_target=self.vol_target,
            gross_cap=self.gross_cap,
            min_history_days=self.min_history_days,
        )

    def run_at(self) -> tuple[int, int]:
        """``run_at_utc`` as (hour, minute)."""
        hour, minute = self.run_at_utc.split(":")
        return int(hour), int(minute)


def next_run_ns(now_ns: int, hour: int, minute: int) -> int:
    """The first ``hour:minute`` UTC strictly after ``now_ns``."""
    now = datetime.fromtimestamp(now_ns / NS_PER_S, tz=UTC)
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return int(at.timestamp()) * NS_PER_S


class TrendPortfolio(KfsStrategy):
    """The weekly trend portfolio over every coin (module docstring)."""

    uses_stop: ClassVar[bool] = False

    def __init__(
        self,
        config: TrendPortfolioConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Record,
        seeds: Mapping[str, Sequence[DailyClose]] | None = None,
        last_start_ns: int | None = None,
    ) -> None:
        super().__init__(config, gate)
        self._cfg = config
        self._blocks = blocks
        self._record = record
        self._env = Env(config.env)
        self._run_id = UUID(config.run_id)
        self._params = config.params()
        self._coins = [InstrumentId.from_str(coin) for coin in config.instruments]
        self._bar_types = {
            BarType.from_str(f"{coin}-{BAR_SPEC}-INTERNAL"): InstrumentId.from_str(coin) for coin in config.instruments
        }
        self._closes: dict[str, dict[date, Decimal]] = {coin: {} for coin in config.instruments}
        for coin, seed in (seeds or {}).items():
            if coin in self._closes:
                for item in seed:
                    self._closes[coin][item.day] = item.close
                self._trim(coin)
        self._last_start_ns = last_start_ns
        self.plan: PortfolioPlan | None = None
        self.plan_day: date | None = None
        self.last_rebalance_ns: int | None = None
        self.last_reason: str | None = None
        self.failed: set[str] = set()
        self._last_weekly: date | None = None
        self._started = False
        self._start_deadline_ns = 0
        self._was_blocked = False
        self._reset_round()

    def _reset_round(self) -> None:
        # One rebalance's working state, dropped by the next rebalance, a flatten, or a stop.
        self._decisions: dict[str, UUID] = {}
        self._reason = ""
        self._orders: dict[ClientOrderId, tuple[str, Leg]] = {}
        self._awaiting_reductions: set[ClientOrderId] = set()
        self._grows: list[str] = []
        self._grows_sent = True
        self._pending_opens: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._inflight_grow: dict[ClientOrderId, Decimal] = {}
        self._closing: set[str] = set()
        self._written: set[str] = set()
        self._pending_inputs: dict[str, JsonObject] = {}

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe trades, quotes, and daily bars for every coin; arm the daily run and the start wait."""
        super().on_start()
        for coin in self._coins:
            self.subscribe_trade_ticks(coin)
            self.subscribe_quote_ticks(coin)
        for bar_type in self._bar_types:
            self.subscribe_bars(bar_type)
        self._arm_daily()
        self._start_deadline_ns = self.clock.timestamp_ns() + self._cfg.quote_wait_s * NS_PER_S
        self.clock.set_timer_ns(_START, _POLL_NS, 0, 0, self._on_start_poll)
        self.clock.set_timer_ns(_GATE, _POLL_NS, 0, 0, self._on_gate_poll)

    def on_stop(self) -> None:
        """Cancel every timer of this strategy, then the base's stop."""
        self._cancel_all_timers()
        super().on_stop()

    def resume_position(self, position: Position, stop_order: Order | None) -> None:
        """Adopt ``position`` as its coin's current quantity (AC-9): there is no stop to rest and no timer."""
        log.info(
            "portfolio position adopted",
            extra={
                "fields": {
                    "code": "portfolio_position_adopted",
                    "instrument": str(position.instrument_id),
                    "quantity": str(position.signed_decimal_qty()),
                }
            },
        )

    def has_open_positions(self) -> bool:
        """True while any coin is held or any order of this strategy is open."""
        return bool(self.cache.positions_open(strategy_id=self.id) or self.cache.orders_open(strategy_id=self.id))

    def flatten(self, halt_id: UUID) -> None:
        """Cancel retries and pending flip opens, then close every coin reduce only at market (idempotent)."""
        self._drop_round()
        tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        self._cancel_entries()
        for position in self.cache.positions_open(strategy_id=self.id):
            coin = str(position.instrument_id)
            if coin in self._closing:
                continue
            self._closing.add(coin)
            self._submit(position.instrument_id, -position.signed_decimal_qty(), reduce_only=True, tags=tags)

    def flatten_for(self, halt_id: UUID, untrusted: frozenset[str]) -> None:
        """``flatten`` that skips the close of an untrusted coin (spec 0023 AC-8); its entries are still cancelled."""
        if not untrusted:
            self.flatten(halt_id)
            return
        self._drop_round()
        self._cancel_entries()
        tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        for position in self.cache.positions_open(strategy_id=self.id):
            coin = str(position.instrument_id)
            if coin in untrusted or coin in self._closing:
                continue
            self._closing.add(coin)
            self._submit(position.instrument_id, -position.signed_decimal_qty(), reduce_only=True, tags=tags)

    # Data -----------------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Store a daily close under its UTC day; keep at most ``seed_days``."""
        coin = self._bar_types.get(bar.bar_type)
        if coin is None:
            return
        key = str(coin)
        self._closes[key][close_day(bar.ts_event)] = bar.close.as_decimal()
        self._trim(key)

    def _trim(self, coin: str) -> None:
        days = self._closes[coin]
        for day in sorted(days)[: max(0, len(days) - self._cfg.seed_days)]:
            del days[day]

    def closes_until(self, day: date) -> dict[str, list[DailyClose]]:
        """Every coin's stored closes up to and including ``day``, oldest first."""
        return {coin: [DailyClose(d, days[d]) for d in sorted(days) if d <= day] for coin, days in self._closes.items()}

    # Schedule -------------------------------------------------------------------------------

    def _arm_daily(self) -> None:
        hour, minute = self._cfg.run_at()
        self._alert(_RUN, next_run_ns(self.clock.timestamp_ns(), hour, minute), self._on_daily)

    def _on_daily(self, event: object) -> None:
        now = self.clock.timestamp_ns()
        self._arm_daily()
        if not self.fresh_signal(now):
            return
        today = datetime.fromtimestamp(now / NS_PER_S, tz=UTC).date()
        try:
            planned = self._make_plan(today - timedelta(days=1))
            if planned and today.weekday() == self._cfg.rebalance_weekday and self._last_weekly != today:
                self._last_weekly = today
                self._rebalance(WEEKLY)
        except Exception:
            log.exception("daily portfolio run failed", extra={"fields": {"code": "portfolio_run_failed"}})

    def _on_start_poll(self, event: object) -> None:
        if self._started:
            return
        if not self._gate.loaded or BlockReason.RECONCILING in self._blocks.active():
            return  # the pause key is not read yet, or the start check still holds every grow (spec 0023)
        now = self.clock.timestamp_ns()
        missing = [coin for coin in self._eligible_coins() if self.cache.quote_tick(coin) is None]
        if missing and now < self._start_deadline_ns:
            return
        self._started = True
        self._cancel_timer(_START)
        for coin in missing:
            log.warning(
                "no quote for the start rebalance", extra={"fields": {"code": "no_quote", "instrument": str(coin)}}
            )
        try:
            today = datetime.fromtimestamp(now / NS_PER_S, tz=UTC).date()
            if not self._make_plan(today - timedelta(days=1)):
                return
            cooldown_ns = self._cfg.start_cooldown_s * NS_PER_S
            if self._last_start_ns is not None and now - self._last_start_ns < cooldown_ns:
                log.info(
                    "start rebalance skipped, one ran recently",
                    extra={"fields": {"code": "start_rebalance_cooldown", "last_start_ns": self._last_start_ns}},
                )
                return
            self._rebalance(START, skip=frozenset(str(coin) for coin in missing))
        except Exception:
            log.exception("start rebalance failed", extra={"fields": {"code": "portfolio_run_failed"}})

    def _on_gate_poll(self, event: object) -> None:
        blocked = self._gate.paused or self._gate.halted
        was, self._was_blocked = self._was_blocked, blocked
        if was and not blocked and self._started and self.plan is not None:
            try:
                self._rebalance(RESUME)
            except Exception:
                log.exception("resume rebalance failed", extra={"fields": {"code": "portfolio_run_failed"}})

    def _eligible_coins(self) -> list[InstrumentId]:
        need = self._params.min_history_days
        return [coin for coin in self._coins if len(self._closes[str(coin)]) >= need]

    # Plan -----------------------------------------------------------------------------------

    def _make_plan(self, day: date) -> bool:
        """Plan from the closes up to ``day``; log the targets and store the plan. False when nothing was planned."""
        equity = self._equity()
        if equity is None or equity <= 0:
            log.warning(
                "portfolio run skipped, equity unknown",
                extra={"fields": {"code": "equity_unknown", "equity": None if equity is None else str(equity)}},
            )
            return False
        try:
            plan = portfolio_plan(self.closes_until(day), equity, self._params)
        except PortfolioError:
            log.exception("portfolio plan failed", extra={"fields": {"code": "plan_empty"}})
            return False
        if plan.n == 0:
            log.warning("no coin can be planned", extra={"fields": {"code": "plan_empty", "day": day.isoformat()}})
            return False
        self.plan = plan
        self.plan_day = day
        for coin in self._coins:
            item = plan.coins[str(coin)]
            log.info(
                "portfolio target",
                extra={
                    "fields": {
                        "code": "portfolio_targets",
                        "instrument": str(coin),
                        "close_date": None if item.close_date is None else item.close_date.isoformat(),
                        "eligible": item.eligible,
                        "signal": item.signal,
                        "vol": None if item.vol is None else str(item.vol),
                        "weight": str(item.weight),
                        "target_notional": str(item.target_notional),
                        "current_qty": str(self._qty(coin)),
                    }
                },
            )
        return True

    # Rebalance ------------------------------------------------------------------------------

    def _rebalance(self, reason: str, skip: frozenset[str] = frozenset()) -> None:
        plan = self.plan
        if plan is None:
            return
        self._drop_round()
        self._reason = reason
        self._grows_sent = False
        self.failed = set()
        self.last_rebalance_ns = self.clock.timestamp_ns()
        self.last_reason = reason
        for coin in self._coins:
            key = str(coin)
            if key in skip:
                continue
            item = plan.coins[key]
            if item.eligible and item.close_date != self.plan_day:
                log.warning(
                    "coin keeps its position, its close is missing",
                    extra={"fields": {"code": "close_missing", "instrument": key}},
                )
                continue
            self._step(coin, first=True)
        self._maybe_send_grows()

    def _step(self, coin: InstrumentId, *, first: bool) -> None:
        """Work out and act on one coin's delta; grows wait until every reduction is done."""
        key = str(coin)
        assert self.plan is not None
        target = self.plan.coins[key].target_notional
        instrument = self._instrument(coin)
        mid = guard.mid(self.cache, coin)
        current = self._qty(coin)
        if mid is None:
            self._order_failed(key, "stale_quote", retry=True)
            return
        delta = order_delta(
            target,
            mid,
            current,
            instrument.size_increment.as_decimal(),
            None if instrument.min_quantity is None else instrument.min_quantity.as_decimal(),
        )
        if delta is None:
            log.info("rebalance delta too small", extra={"fields": {"code": "rebalance_skip_small", "instrument": key}})
            return
        target_qty = current + delta
        if current != 0 and target_qty != 0 and (current > 0) != (target_qty > 0):
            leg = Leg.FLIP_CLOSE
        elif current != 0 and abs(target_qty) < abs(current) and (target_qty == 0 or (current > 0) == (target_qty > 0)):
            leg = Leg.REDUCE
        else:
            leg = Leg.GROW
        inputs = self._inputs(key, mid, current, target_qty, delta)
        if leg == Leg.GROW:
            if first and key not in self._decisions:
                self._decisions[key] = uuid.uuid7()
                self._pending_inputs[key] = inputs
            self._grows.append(key)
            if self._grows_sent:
                self._send_grow(key)
            return
        decision = self._decision(key, Intent.EXIT if leg == Leg.REDUCE else Intent.ADJUST, delta, inputs, None)
        if leg == Leg.FLIP_CLOSE:
            self._pending_opens.add(key)
            client_id = self._submit(coin, -current, reduce_only=True, tags=[f"{DECISION_TAG}{decision}"])
        else:
            client_id = self._submit(coin, delta, reduce_only=True, tags=[f"{DECISION_TAG}{decision}"])
        self._orders[client_id] = (key, leg)
        self._awaiting_reductions.add(client_id)

    def _maybe_send_grows(self) -> None:
        if self._grows_sent or self._awaiting_reductions:
            if not self._grows_sent and not self._timer(_GROWS):
                fallback = self.clock.timestamp_ns() + self._cfg.retry_wait_s * NS_PER_S
                self._alert(_GROWS, fallback, lambda _e: self._send_all_grows())
            return
        self._send_all_grows()

    def _send_all_grows(self) -> None:
        self._cancel_timer(_GROWS)
        if self._grows_sent:
            return
        self._grows_sent = True
        for key in list(self._grows):
            self._send_grow(key)

    def _send_grow(self, key: str) -> None:
        """Size again at the mid and send one opening or growing order through the gate and the check."""
        if key in self._grows:
            self._grows.remove(key)
        assert self.plan is not None
        coin = InstrumentId.from_str(key)
        instrument = self._instrument(coin)
        quote = self.cache.quote_tick(coin)
        current = self._qty(coin)
        if quote is None:
            sign = Decimal(1) if self.plan.coins[key].target_notional >= current else Decimal(-1)
            self._grow_refused(key, "stale_quote", self._pending_inputs.get(key, {}), sign, retry=True)
            return
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        step = instrument.size_increment.as_decimal()
        target_qty = target_quantity(self.plan.coins[key].target_notional, mid, step)
        delta = target_qty - current
        inputs = self._inputs(key, mid, current, target_qty, delta)
        minimum = max(step, Decimal(0) if instrument.min_quantity is None else instrument.min_quantity.as_decimal())
        if abs(delta) < minimum or (current != 0 and (delta > 0) != (current > 0)):
            log.info("rebalance delta too small", extra={"fields": {"code": "rebalance_skip_small", "instrument": key}})
            return
        held = self._held_reason()
        if held == BlockReason.ORDER_UNRESOLVED:
            # Only an order still waiting for its answer (often this rebalance's own, spec 0023 AC-14): try the
            # grow again after the retry wait instead of holding it until a resume that never comes.
            log.info(
                "rebalance grow waits for an unanswered order",
                extra={"fields": {"code": "rebalance_deferred", "instrument": key}},
            )
            self._order_failed(key, BlockReason.ORDER_UNRESOLVED, retry=True)
            return
        if held is not None:
            log.info(
                "rebalance grow held",
                extra={"fields": {"code": "rebalance_held", "instrument": key, "reason": held}},
            )
            self._decision(key, Intent.ENTER, delta, inputs, f"{HELD_PREFIX}{held}")
            return
        try:
            book = guard.open_notional(self.cache) + sum(self._inflight_grow.values(), Decimal(0))
            equity = self._equity()
            if equity is None:
                raise InvalidRiskInput("equity unknown")
            request = RebalanceRequest(
                side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                quantity=abs(delta),
                mid=mid,
                quote_age_s=Decimal(max(0, self.clock.timestamp_ns() - quote.ts_event)) / NS_PER_S,
                equity=equity,
                book_exposure=book,
                current_qty=current,
                size_step=step,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                stale_quote_s=self._cfg.stale_quote_s,
            )
            verdict = check_rebalance(request, self._cfg.risk_limits())
        except (InvalidRiskInput, InvalidRebalanceInput) as exc:
            log.warning(
                "rebalance check could not judge", extra={"fields": {"code": "risk_input_invalid", "why": str(exc)}}
            )
            self._grow_refused(key, "risk_input_invalid", inputs, delta, retry=True)
            return
        inputs = {**inputs, "liq_px": verdict.liq_px, "liq_distance_pct": verdict.liq_distance_pct}
        if not verdict.approved:
            reason = verdict.reason or "refused"
            self._grow_refused(key, reason, inputs, delta, retry=reason == STALE_QUOTE)
            return
        quantity = verdict.quantity
        if verdict.clips:
            log.info(
                "rebalance order clipped",
                extra={
                    "fields": {
                        "code": "rebalance_clipped",
                        "instrument": key,
                        "wanted": str(abs(delta)),
                        "sent": str(quantity),
                        "clips": list(verdict.clips),
                    }
                },
            )
            inputs = {**inputs, "clip": {"wanted": abs(delta), "sent": quantity, "by": list(verdict.clips)}}
        signed = quantity if delta > 0 else -quantity
        decision = self._decision(key, Intent.ENTER, signed, inputs, None)
        client_id = self._submit(coin, signed, reduce_only=False, tags=[f"{DECISION_TAG}{decision}"])
        self._orders[client_id] = (key, Leg.GROW)
        self._inflight_grow[client_id] = quantity * mid

    def _grow_refused(self, key: str, reason: str, inputs: JsonObject, delta: Decimal, *, retry: bool) -> None:
        # Recorded once per rebalance (the first refusal); a liquidation distance refusal is never retried.
        self._decision(key, Intent.ENTER, delta, inputs, reason)
        self._order_failed(key, reason, retry=retry and reason != LIQ_DISTANCE)

    # Order events ---------------------------------------------------------------------------

    def on_order_filled(self, event: OrderFilled) -> None:
        """A filled reduction may let the grows go; a filled flip close opens the new side."""
        tracked = self._orders.get(event.client_order_id)
        if tracked is None:
            return
        order = self.cache.order(event.client_order_id)
        if order is not None and not order.is_closed:
            return  # partly filled: wait for the rest
        key, leg = tracked
        self._inflight_grow.pop(event.client_order_id, None)
        self._attempts.pop(key, None)
        if leg == Leg.FLIP_CLOSE and key in self._pending_opens:
            self._pending_opens.discard(key)
            self._grows.append(key)
        self._reduction_done(event.client_order_id)
        if leg == Leg.FLIP_CLOSE and self._grows_sent and key in self._grows:
            self._send_grow(key)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """A rejected order is retried for its coin (AC-6)."""
        self._order_refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        """A denied order is handled like a rejection (AC-5, AC-6)."""
        self._order_refused(event.client_order_id, f"denied: {event.reason}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """A cancel this strategy did not ask for is handled like a rejection."""
        self._order_refused(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired) -> None:
        """Like a cancel."""
        self._order_refused(event.client_order_id, "expired")

    def on_position_closed(self, event: PositionClosed) -> None:
        """A closed coin is no longer being flattened."""
        self._closing.discard(str(event.instrument_id))

    def _order_refused(self, client_order_id: ClientOrderId, why: str) -> None:
        tracked = self._orders.pop(client_order_id, None)
        self._inflight_grow.pop(client_order_id, None)
        if tracked is None:
            return
        key, leg = tracked
        log.warning(
            "rebalance order refused",
            extra={"fields": {"code": "rebalance_order_refused", "instrument": key, "leg": leg.value, "why": why}},
        )
        if leg == Leg.FLIP_CLOSE:
            self._pending_opens.discard(key)
        self._reduction_done(client_order_id)
        self._order_failed(key, why, retry=True)

    def _reduction_done(self, client_order_id: ClientOrderId) -> None:
        if client_order_id in self._awaiting_reductions:
            self._awaiting_reductions.discard(client_order_id)
            if not self._awaiting_reductions:
                self._send_all_grows()

    def _order_failed(self, key: str, why: str, *, retry: bool) -> None:
        tries = self._attempts.get(key, 0)
        if retry and tries < self._cfg.retry_max:
            self._attempts[key] = tries + 1
            at = self.clock.timestamp_ns() + self._cfg.retry_wait_s * NS_PER_S
            self._alert(f"{_RETRY_PREFIX}{key}", at, functools.partial(self._on_retry, key))
            return
        self.failed.add(key)
        log.warning(
            "rebalance coin failed",
            extra={"fields": {"code": "rebalance_coin_failed", "instrument": key, "why": why, "tries": tries}},
        )

    def _on_retry(self, key: str, event: object) -> None:
        try:
            self._step(InstrumentId.from_str(key), first=False)
        except Exception:
            log.exception(
                "rebalance retry failed", extra={"fields": {"code": "portfolio_run_failed", "instrument": key}}
            )

    # Helpers --------------------------------------------------------------------------------

    def _drop_round(self) -> None:
        for name in list(self.clock.timer_names):
            if name.startswith(_RETRY_PREFIX) or name == _GROWS:
                self.clock.cancel_timer(name)
        closing = self._closing
        self._reset_round()
        self._closing = closing

    def _cancel_all_timers(self) -> None:
        for name in list(self.clock.timer_names):
            if name.startswith(_RETRY_PREFIX) or name in (_RUN, _START, _GATE, _GROWS):
                self.clock.cancel_timer(name)

    def _held_reason(self) -> str | None:
        if self._gate.halted:
            return "halted"
        if self._gate.paused:
            return "paused"
        # ``order_unresolved`` counts only when no other block holds the grow (see ``_send_grow``).
        active = sorted(str(reason) for reason in self._blocks.active())
        others = [reason for reason in active if reason != BlockReason.ORDER_UNRESOLVED]
        return others[0] if others else (active[0] if active else None)

    def _inputs(self, key: str, mid: Decimal, current: Decimal, target_qty: Decimal, delta: Decimal) -> JsonObject:
        assert self.plan is not None
        item = self.plan.coins[key]
        return {
            "reason": self._reason,
            "close_date": None if self.plan_day is None else self.plan_day.isoformat(),
            "signal": item.signal,
            "vol": item.vol,
            "weight": item.weight,
            "n": self.plan.n,
            "gross": self.plan.gross,
            "gross_scale": self.plan.gross_scale,
            "equity": self.plan.equity,
            "target_notional": item.target_notional,
            "current_qty": current,
            "target_qty": target_qty,
            "delta": delta,
            "mid": mid,
        }

    def _decision(self, key: str, intent: Intent, delta: Decimal, inputs: JsonObject, refused: str | None) -> UUID:
        decision = self._decisions.get(key)
        if decision is None:
            decision = uuid.uuid7()
            self._decisions[key] = decision
        if key in self._written:
            return decision  # one row per coin per rebalance; retries reuse it
        self._written.add(key)
        row = DecisionRow(
            id=decision,
            env=self._env,
            run_id=self._run_id,
            instrument_id=key,
            intent=intent,
            side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
            reason=self._reason,
            inputs=inputs,
            risk_verdict=RiskVerdict.APPROVED if refused is None else RiskVerdict.REJECTED,
            risk_reason=refused,
            decided_ns=self.clock.timestamp_ns(),
        )
        self._record(WriteItem(WriteCall.RECORD_DECISION, row))
        return decision

    def _submit(self, coin: InstrumentId, signed_qty: Decimal, *, reduce_only: bool, tags: list[str]) -> ClientOrderId:
        instrument = self._instrument(coin)
        order = self.order_factory.market(
            coin,
            NautilusOrderSide.BUY if signed_qty > 0 else NautilusOrderSide.SELL,
            instrument.make_qty(abs(signed_qty)),
            time_in_force=TimeInForce.GTC,
            reduce_only=reduce_only,
            tags=tags,
        )
        client_id: ClientOrderId = order.client_order_id
        self.submit_order(order)
        return client_id

    def _qty(self, coin: InstrumentId) -> Decimal:
        total = Decimal(0)
        for position in self.cache.positions_open(instrument_id=coin, strategy_id=self.id):
            total += position.signed_decimal_qty()
        return total

    def _instrument(self, coin: InstrumentId) -> Instrument:
        instrument = self.cache.instrument(coin)
        if instrument is None:
            raise LookupError(f"instrument {coin} not in the cache")
        return instrument

    def _equity(self) -> Decimal | None:
        # The account balance plus every open position's unrealized P&L at its mid; None when unknown.
        account = self.portfolio.account(self._coins[0].venue)
        if account is None:
            return None
        balance = account.balance_total(USD)
        if balance is None:
            return None
        try:
            equity: Decimal = balance.as_decimal() + guard.unrealized(self.cache)
        except InvalidRiskInput:
            return None
        return equity

    def _timer(self, name: str) -> bool:
        return name in self.clock.timer_names

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

    # The snapshot ---------------------------------------------------------------------------

    def plan_snapshot(self) -> Mapping[str, object]:
        """``portfolio_plan``'s data (AC-13, AC-20, AC-21): each coin's plan, the book, the schedule, ``failed``."""
        plan = self.plan
        coins: list[JsonObject] = []
        for coin in self._coins:
            key = str(coin)
            item = None if plan is None else plan.coins.get(key)
            mid = guard.mid(self.cache, coin)
            instrument = self.cache.instrument(coin)
            target_qty = None
            if item is not None and mid is not None and mid > 0 and instrument is not None:
                target_qty = str(target_quantity(item.target_notional, mid, instrument.size_increment.as_decimal()))
            coins.append(
                {
                    "instrument_id": key,
                    "eligible": None if item is None else item.eligible,
                    "signal": None if item is None else item.signal,
                    "vol": None if item is None or item.vol is None else str(item.vol),
                    "weight": None if item is None else str(item.weight),
                    "target_notional": None if item is None else str(item.target_notional),
                    "target_qty": target_qty,
                    "close_date": None if item is None or item.close_date is None else item.close_date.isoformat(),
                    "failed": key in self.failed,
                }
            )
        return {
            "coins": coins,
            "n": None if plan is None else plan.n,
            "gross": None if plan is None else str(plan.gross),
            "gross_scale": None if plan is None else str(plan.gross_scale),
            "equity": None if plan is None else str(plan.equity),
            "plan_day": None if self.plan_day is None else self.plan_day.isoformat(),
            "last_rebalance_at": self.last_rebalance_ns,
            "last_reason": self.last_reason,
            "failed": sorted(self.failed),
            "run_at_utc": self._cfg.run_at_utc,
            "rebalance_weekday": self._cfg.rebalance_weekday,
        }
