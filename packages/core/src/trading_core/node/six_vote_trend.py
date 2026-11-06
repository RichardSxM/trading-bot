"""``SixVoteTrend``: ``SixVoteRule`` on 1 hour bars on the maker base, a demo forward test (spec 0029).

A demo only paper strategy with no gate pass: it trades only after a manual ``candidate → paper`` promote.
Its own module so its code hash is its own. It runs ``SixVoteRule`` on ``MakerBarRuleStrategy`` (imported
unchanged); r2's module is not imported, so the seed join and the spread check are written again here.

The BTC vote (AC-3): every coin other than BTC also subscribes to BTC's 1 hour bars and keeps BTC's EMA200
itself (``_BtcTrend``), mapping each BTC bar close time to ``sign(close - EMA200)`` (0 until 200 BTC closes
were fed). A coin bar uses the BTC sign of the same close time; when that BTC bar has not arrived yet the
coin bar is held for up to ``btc_wait_s`` and fed when it comes, else fed with the last known sign and an
INFO ``btc_bar_late``. The BTC instance uses its own bars.

The warmup seeds (AC-8): the coin's candles are held until the first live bar, which is partial and so is
ignored. If that bar closes more than two intervals after the seed's last close the seed is dropped as
stale; otherwise it is fed through the base's ATR and the rule, and any live bar at or before the seed's
last close is ignored too. The BTC seed goes through the BTC EMA, with the same staleness rule at the first
live BTC bar (a stale one is dropped, so V2 stays 0 until 200 live BTC bars). The seed never records a
decision or trades.

One entry filter (``_pre_entry``): r2's spread check, which skips an entry whose quote spread is wider than
``spread_max_atr`` ATR as ``spread_wide``. At every coin bar close once the rule is ready it logs INFO
``six_vote_values`` with every value of the rule (AC-8a).

The entry fallback (AC-4): when the base cancels the maker entry at its wait (and this node asked for it,
with no exit decided), or the venue rejects it as post only, the rest goes as a GTC market order under the
same entry decision, after the base's checks again (gate, blocks, a fresh quote, the mid not through the
stop, ``check_entry`` at the mid with the same stop). Any refusal logs ``entry_fallback_skipped`` and goes flat
when nothing is held. Nothing filled: the market becomes the entry, so its fills run the base's entry path.
Part filled: the stop follows the quantity. After any fallback fill the entry price is the position's.

The trailing stop (AC-5, off when ``trail_atr`` is ``None``): every quote tick in a position moves the best
price (the highest bid for a long, the lowest ask for a short, starting at the first fill), and the stop's
trigger moves to ``best -+ trail_atr x ATR`` when that is tighter than the stop's current trigger by
``trail_min_step_atr`` ATR (at least a tick), ``trail_min_interval_s`` after this coin's last move, with no
modify pending and no market close under way. It never loosens. ``trail_max_refusals`` refusals in a row
disable it for the trade. After a restart (AC-9) it waits for the ATR (the first live bar close) and starts
the best price at the current quote.

Coupling: this module reads private state of the maker base (``_entry_cancel_asked``, ``_entry_id``,
``_retired``, ``_exit_decision``, ``_exit_id``, ``_stop_id``, ``_stop_px``, ``_atr``, ``_side``) and calls its
private helpers. That is safe only while ``bar_rule_maker`` stays frozen; if the base is ever replaced,
check this node again (spec 0029 AC-11).
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final, Literal

from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import OrderStatus as NautilusOrderStatus
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled, OrderModifyRejected, OrderUpdated
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.node import guard
from trading_core.node.bar_rule_maker import MakerBarRuleConfig, MakerBarRuleStrategy, Phase, order_side
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, OrderSide, RiskEventKind
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.risk.check import EntryRequest, InvalidRiskInput, check_entry, refusal_severity
from trading_core.risk.funding import funding_periods
from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.six_vote import EMA_TREND, EwmEma, SixVoteRule, sign

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "six_vote_trend"
BAR_SPEC: Final = "1-HOUR-LAST"
BTC_INSTRUMENT_ID: Final = "PF_XBTUSD.KRAKEN"
SPREAD_WIDE: Final = "spread_wide"
SEED_FAILED: Final = "warmup_seed_failed"
SEED_STALE: Final = "seed_stale"
BTC_BAR_LATE: Final = "btc_bar_late"
VALUES_LOG: Final = "six_vote_values"
NS_PER_S: Final = 1_000_000_000
# More than this many intervals between the seed's last close and the first live bar's means more than
# one bar is missing between them.
SEED_JOIN_INTERVALS: Final = 2
# BTC signs kept by close time: the seed's 300 plus a margin.
BTC_SIGNS_KEPT: Final = 400

_BTC_WAIT: Final = "six_vote_btc_wait"

type FallbackWhy = Literal["timeout", "remainder", "post_only_rejected"]


def trail_level(side: Side, best: Decimal, atr: Decimal, trail_atr: Decimal, tick: Decimal) -> Decimal:
    """The trail trigger (AC-5): ``best - trail_atr x atr`` rounded down for a long, plus and rounded up for a short."""
    if side == Side.LONG:
        raw, rounding = best - trail_atr * atr, ROUND_FLOOR
    else:
        raw, rounding = best + trail_atr * atr, ROUND_CEILING
    return ((raw / tick).to_integral_value(rounding=rounding) * tick).quantize(tick)


def trail_moves(side: Side, level: Decimal, current: Decimal, step: Decimal) -> bool:
    """True when ``level`` is tighter than the ``current`` trigger by at least ``step`` (never a loosening)."""
    return level - current >= step if side == Side.LONG else current - level >= step


class SixVoteTrendConfig(MakerBarRuleConfig, frozen=True, kw_only=True):
    """``six_vote_trend``'s settings on top of the maker base's (AC-2)."""

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 24
    entry_offset_atr: Decimal = Decimal(0)
    entry_wait_bars: int = 1
    exit_wait_bars: int = 1
    spread_max_atr: Decimal | None = Decimal("0.1")
    trail_atr: Decimal | None = Decimal(3)
    trail_min_step_atr: Decimal = Decimal("0.1")
    trail_min_interval_s: int = 5
    trail_max_refusals: int = 5
    btc_instrument_id: str = BTC_INSTRUMENT_ID
    btc_wait_s: int = 3

    def __post_init__(self) -> None:
        """The base's checks, then this strategy's."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.spread_max_atr is not None and self.spread_max_atr <= 0:
            raise ValueError(f"spread_max_atr must be above 0, got {self.spread_max_atr}")
        if self.trail_atr is not None and self.trail_atr <= 0:
            raise ValueError(f"trail_atr must be above 0 when set, got {self.trail_atr}")
        if self.trail_min_step_atr < 0:
            raise ValueError(f"trail_min_step_atr must be at least 0, got {self.trail_min_step_atr}")
        if self.trail_min_interval_s < 1:
            raise ValueError(f"trail_min_interval_s must be at least 1, got {self.trail_min_interval_s}")
        if self.trail_max_refusals < 1:
            raise ValueError(f"trail_max_refusals must be at least 1, got {self.trail_max_refusals}")
        if self.btc_wait_s < 0:
            raise ValueError(f"btc_wait_s must be at least 0, got {self.btc_wait_s}")


class _BtcTrend:
    """BTC's EMA200 trend by bar close time (AC-3): 0 until ``EMA_TREND`` closes were fed."""

    def __init__(self) -> None:
        self._ema = EwmEma(EMA_TREND)
        self._fed = 0
        self._signs: dict[int, int] = {}
        self.last = 0

    def feed(self, ts_ns: int, close: Decimal) -> int:
        ema = self._ema.update(close)
        self._fed += 1
        trend = sign(close - ema) if self._fed >= EMA_TREND else 0
        self._signs[ts_ns] = trend
        if len(self._signs) > BTC_SIGNS_KEPT:
            del self._signs[next(iter(self._signs))]
        self.last = trend
        return trend

    def sign_at(self, ts_ns: int) -> int | None:
        return self._signs.get(ts_ns)

    @property
    def ready(self) -> bool:
        return self._fed >= EMA_TREND


class SixVoteTrend(MakerBarRuleStrategy):
    """``MakerBarRuleStrategy`` with ``SixVoteRule`` on 1 hour bars, BTC's trend, a seed, and the spread check."""

    def __init__(
        self,
        config: SixVoteTrendConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Callable[[WriteItem], None],
        seed: Sequence[BarInput] = (),
        btc_seed: Sequence[BarInput] = (),
    ) -> None:
        rule = SixVoteRule()
        super().__init__(config, gate, blocks, record, rule)
        self._six_rule = rule
        self._six = config
        self._is_btc = config.instrument_id == config.btc_instrument_id
        self._btc_bar_type = BarType.from_str(f"{config.btc_instrument_id}-{config.bar_spec}-INTERNAL")
        self._seed: tuple[BarInput, ...] = tuple(seed)
        self._seed_pending = bool(self._seed)
        self._seed_last_ns: int | None = self._seed[-1].ts_ns if self._seed else None
        # The BTC seed goes through the BTC EMA now, so the coin seed can read its signs; the first live
        # BTC bar checks its staleness (AC-8). The BTC instance reads its own bars and seed instead.
        self._btc = _BtcTrend()
        btc = () if self._is_btc else tuple(btc_seed)
        for closed in btc:
            self._btc.feed(closed.ts_ns, closed.close)
        self._btc_seed_pending = bool(btc)
        self._btc_seed_last_ns: int | None = btc[-1].ts_ns if btc else None
        self._held: Bar | None = None
        self._trail_last_ns = 0  # this coin's last trail modify; outlives the round trip (AC-5)

    def _reset(self) -> None:
        """The base's trade state, plus the fallback's and the trail's (both reset with every round trip)."""
        super()._reset()
        self._fallback_id: ClientOrderId | None = None
        self._best: Decimal | None = None
        self._trail_pending = False
        self._trail_old: Decimal | None = None
        self._trail_refusals = 0
        self._trail_disabled = False

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """The base's subscriptions, plus BTC's 1 hour bars for every coin other than BTC (AC-3)."""
        super().on_start()
        if not self._is_btc:
            self.subscribe_bars(self._btc_bar_type)

    def resume_position(self, position: Position, stop_order: Order | None) -> None:
        """The base's resume (AC-9); the trail then restarts from the resting stop, best price at the next quote."""
        super().resume_position(position, stop_order)
        self._best = None

    # Bars -----------------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """A BTC bar feeds the BTC trend; a coin bar joins the seed, gets its BTC sign, then the base's step."""
        if not self._is_btc and bar.bar_type == self._btc_bar_type:
            self._on_btc_bar(bar)
            return
        if bar.bar_type != self.bar_type:
            return
        if self._seed_pending:
            self._seed_pending = False
            self._join_seed(bar.ts_event)
            return  # the first live bar covers only part of its interval
        if self._seed_last_ns is not None and bar.ts_event <= self._seed_last_ns:
            return
        if self._is_btc:
            self._six_rule.set_btc_trend(self._btc.feed(bar.ts_event, bar.close.as_decimal()))
            self._feed(bar)
            return
        if self._held is not None:
            self._release_held(late=True)  # an hour late: never expected
        trend = self._btc.sign_at(bar.ts_event)
        if trend is None:
            self._held = bar
            wait_ns = self._six.btc_wait_s * NS_PER_S
            self._alert(_BTC_WAIT, self.clock.timestamp_ns() + wait_ns, self._on_btc_wait)
            return
        self._six_rule.set_btc_trend(trend)
        self._feed(bar)

    def _on_btc_bar(self, bar: Bar) -> None:
        ts = bar.ts_event
        if self._btc_seed_pending:
            self._btc_seed_pending = False
            self._join_btc_seed(ts)
            return  # the first live BTC bar covers only part of its interval
        if self._btc_seed_last_ns is not None and ts <= self._btc_seed_last_ns:
            return
        self._btc.feed(ts, bar.close.as_decimal())
        if self._held is not None and self._held.ts_event == ts:
            self._release_held(late=False)

    def _on_btc_wait(self, event: object) -> None:
        if self._held is not None:
            self._release_held(late=True)

    def _release_held(self, *, late: bool) -> None:
        bar, self._held = self._held, None
        assert bar is not None  # checked by the caller
        self._cancel_timer(_BTC_WAIT)
        trend = self._btc.sign_at(bar.ts_event)
        if trend is None:
            trend = self._btc.last
            if late:
                log.info(
                    "BTC bar late, using the last BTC trend",
                    extra={"fields": {"code": BTC_BAR_LATE, "bar_close_ns": bar.ts_event, "v_btc": trend}},
                )
        self._six_rule.set_btc_trend(trend)
        self._feed(bar)

    def _feed(self, bar: Bar) -> None:
        super().on_bar(bar)
        if self._six_rule.ready():
            values = {
                key: (str(value) if isinstance(value, Decimal) else value) for key, value in self.rule.values().items()
            }
            log.info(
                "six vote values",
                extra={
                    "fields": {
                        "code": VALUES_LOG,
                        "instrument": self._six.instrument_id,
                        "bar_close_ns": bar.ts_event,
                        **values,
                    }
                },
            )

    # Warmup seeds ---------------------------------------------------------------------------

    def _join_seed(self, first_close_ns: int) -> None:
        assert self._seed_last_ns is not None  # a pending seed is not empty
        gap = first_close_ns - self._seed_last_ns
        seed, self._seed = self._seed, ()
        if gap > SEED_JOIN_INTERVALS * self._six.bar_interval_ns:
            self._seed_last_ns = None
            log.warning(
                "warmup seed dropped, too old for the first live bar",
                extra={"fields": {"code": SEED_FAILED, "reason": SEED_STALE, "gap_s": gap // NS_PER_S}},
            )
            return
        try:
            for closed in seed:
                if self._is_btc:
                    trend = self._btc.feed(closed.ts_ns, closed.close)
                else:
                    known = self._btc.sign_at(closed.ts_ns)
                    trend = 0 if known is None else known
                self._six_rule.set_btc_trend(trend)
                atr = self._atr.update(closed.high, closed.low, closed.close)
                self.rule.update(closed, atr)
            if self.rule.entry() is not None:
                self.rule.consumed()  # the seed never trades
        except Exception:
            # Fail safe: warm up from live bars on whatever was fed; nothing was decided.
            log.exception("warmup seed feed failed", extra={"fields": {"code": SEED_FAILED, "reason": "seed_feed"}})
            return
        log.info(
            "warmup seed fed",
            extra={"fields": {"code": "warmup_seed_fed", "bars": len(seed), "ready": self.rule.ready()}},
        )

    def _join_btc_seed(self, first_close_ns: int) -> None:
        assert self._btc_seed_last_ns is not None  # a pending seed is not empty
        gap = first_close_ns - self._btc_seed_last_ns
        if gap > SEED_JOIN_INTERVALS * self._six.bar_interval_ns:
            self._btc = _BtcTrend()
            self._btc_seed_last_ns = None
            log.warning(
                "BTC warmup seed dropped, too old for the first live BTC bar",
                extra={"fields": {"code": SEED_FAILED, "reason": SEED_STALE, "btc": True, "gap_s": gap // NS_PER_S}},
            )
            return
        log.info("BTC warmup seed joined", extra={"fields": {"code": "btc_seed_fed", "ready": self._btc.ready}})

    # Entry filter ---------------------------------------------------------------------------

    def _pre_entry(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> bool:
        """r2's spread check; False after recording the refusal."""
        limit_atr = self._six.spread_max_atr
        quote = self.cache.quote_tick(self._instrument_id)
        if limit_atr is None or quote is None:
            return True  # the base logs a missing quote
        bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        spread, limit = ask - bid, limit_atr * atr
        if spread <= limit:
            return True
        now = self.clock.timestamp_ns()
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "structural_stop": signal.structural_stop,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_limit": limit,
            "mid": (bid + ask) / 2,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
        }
        self._record_decision(
            uuid.uuid7(), Intent.ENTER, order_side(signal.side), signal.reason, inputs, False, SPREAD_WIDE, now
        )
        log.info(
            "entry skipped, spread wide",
            extra={"fields": {"code": "entry_skipped_spread", "spread": str(spread), "limit": str(limit)}},
        )
        return False

    # Entry fallback (AC-4) ------------------------------------------------------------------

    def _canceled(self, client_order_id: ClientOrderId, why: str) -> None:
        """A lost fallback market, or the entry's own wait cancel, before the base; else the base's."""
        if client_order_id == self._fallback_id:
            self._fallback_lost(client_order_id, why)
            return
        if why == "canceled" and self._fallback_may_fire(client_order_id) and self._entry_cancel_asked:
            limit = self.cache.order(client_order_id)
            part = limit is not None and limit.filled_qty.as_decimal() > 0
            self._entry_fallback(client_order_id, "remainder" if part else "timeout")
            return
        super()._canceled(client_order_id, why)

    def _refused(self, client_order_id: ClientOrderId, why: str, *, post_only: bool) -> None:
        """A post only rejection of the entry falls back to market; a refused part fill fallback is reported."""
        if post_only and self._fallback_may_fire(client_order_id) and self._position() is None:
            self._entry_fallback(client_order_id, "post_only_rejected")
            return
        if client_order_id == self._fallback_id and client_order_id != self._entry_id:
            # The part filled case: the base does not know this order; the part keeps its stop.
            self._entry_failed(client_order_id, why)
            self._fallback_id = None
            return
        super()._refused(client_order_id, why, post_only=post_only)

    def _fallback_may_fire(self, client_order_id: ClientOrderId) -> bool:
        return (
            client_order_id == self._entry_id
            and self._fallback_id is None
            and client_order_id not in self._retired
            and self._exit_decision is None
            and self._exit_id is None
            and self.phase in (Phase.ENTERING, Phase.PROTECTED)
        )

    def _entry_fallback(self, limit_id: ClientOrderId, why: FallbackWhy) -> None:
        # The base's checks again, then the rest at market under the same decision.
        held = self._position() is not None
        skip = self._fallback_check()
        if isinstance(skip, str):
            self._fallback_skipped(skip, held)
            return
        mid, approved = skip
        limit = self.cache.order(limit_id)
        if limit is None:
            self._fallback_skipped("no_order", held)
            return
        instrument = self._instrument()
        quantity = min(limit.leaves_qty.as_decimal(), approved)
        if quantity < instrument.size_increment.as_decimal():
            self._fallback_skipped("zero_quantity", held)
            return
        assert self._side is not None  # set with the entry
        order = self.order_factory.market(
            self._instrument_id,
            NautilusOrderSide.BUY if self._side == Side.LONG else NautilusOrderSide.SELL,
            instrument.make_qty(quantity),
            time_in_force=TimeInForce.GTC,
            tags=[f"{DECISION_TAG}{self.entry_decision}"],
        )
        self._fallback_id = order.client_order_id
        self._entry_cancel_asked = True  # the base's entry wait never cancels the market
        if not held:
            self._entry_id = order.client_order_id  # its fills run the base's entry path
        log.info(
            "maker entry replaced by market",
            extra={
                "fields": {
                    "code": "entry_fallback_market",
                    "why": why,
                    "quantity": str(quantity),
                    "mid": str(mid),
                    "client_order_id": str(order.client_order_id),
                }
            },
        )
        self.submit_order(order)

    def _fallback_check(self) -> str | tuple[Decimal, Decimal]:
        # The skip reason, or the mid and the approved quantity at it.
        if self._gate.halted:
            return "halted"
        if self._gate.paused:
            return "paused"
        blocks = self._entry_blocks()
        if blocks:
            return f"blocked:{','.join(blocks)}"
        quote = self.cache.quote_tick(self._instrument_id)
        now = self.clock.timestamp_ns()
        if quote is None or now - quote.ts_event > self._cfg.stale_quote_s * NS_PER_S:
            return "stale_quote"
        assert self._side is not None  # set with the entry
        assert self._stop_px is not None
        mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        if (mid <= self._stop_px) if self._side == Side.LONG else (mid >= self._stop_px):
            return "stop_through"
        instrument = self._instrument()
        try:
            request = EntryRequest(
                side=OrderSide.BUY if self._side == Side.LONG else OrderSide.SELL,
                mid=mid,
                equity=self._equity(),
                open_notional=guard.open_notional(self.cache),
                size_step=instrument.size_increment.as_decimal(),
                taker_fee=self._cfg.taker_fee,
                instrument_maintenance_rate=guard.margin_maint(instrument),
                funding_periods=funding_periods(now, self._hold_ns),
                funding_rate=self._funding_rate(),
                stop_px=self._stop_px,
                has_position=False,
                paused=self._gate.paused,
                halted=self._gate.halted,
                blocks=blocks,
            )
            verdict = check_entry(request, self._limits)
        except InvalidRiskInput:
            log.exception("risk check could not judge the fallback", extra={"fields": {"code": "risk_input_invalid"}})
            return "risk_input_invalid"
        if not verdict.approved:
            reason = verdict.reason or ""
            self._risk_event(
                f"limit_hit:{self.entry_decision}:fallback",
                RiskEventKind.LIMIT_HIT,
                refusal_severity(reason),
                f"entry fallback refused: {reason}",
                self.entry_decision,
                {"reason": reason},
            )
            return reason
        return mid, verdict.quantity

    def _fallback_skipped(self, why: str, held: bool) -> None:
        log.info("entry fallback skipped", extra={"fields": {"code": "entry_fallback_skipped", "why": why}})
        if not held:
            self._back_to_flat()

    def _fallback_lost(self, client_order_id: ClientOrderId, why: str) -> None:
        log.warning(
            "entry fallback market lost",
            extra={"fields": {"code": "entry_fallback_lost", "why": why, "client_order_id": str(client_order_id)}},
        )
        self._fallback_id = None
        if self._position() is None and self.phase == Phase.ENTERING:
            self._back_to_flat()

    def on_order_filled(self, event: OrderFilled) -> None:
        """The base's fill paths; a fallback fill also sets the entry price and moves the stop (AC-4, AC-5)."""
        part_fallback = event.client_order_id == self._fallback_id and event.client_order_id != self._entry_id
        super().on_order_filled(event)
        if self._best is None and event.client_order_id == self._entry_id:
            self._best = event.last_px.as_decimal()  # the first fill starts the trail
        if event.client_order_id != self._fallback_id:
            return
        if part_fallback and event.client_order_id not in self._retired:
            self._sync_stop()  # the base does not know this order: the stop follows the quantity
        position = self._position()
        if position is not None:
            self._entry_px = Decimal(str(position.avg_px_open))

    # Trailing stop (AC-5) -------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """The base's quote handling, then the trail."""
        super().on_quote_tick(tick)
        if tick.instrument_id != self._instrument_id:
            return
        try:
            self._trail(tick)
        except Exception:
            # Fail safe: the stop stays where it rests.
            log.exception("trail step failed", extra={"fields": {"code": "stop_trail_failed"}})

    def _trail(self, tick: QuoteTick) -> None:
        trail_atr = self._six.trail_atr
        if trail_atr is None or self._trail_disabled:
            return
        position = self._position()
        if position is None or self._closing_at_market() or self._kill_switch_tags is not None:
            return
        side = Side.LONG if position.is_long else Side.SHORT
        touch = tick.bid_price.as_decimal() if side == Side.LONG else tick.ask_price.as_decimal()
        if self._best is None:
            self._best = touch
        else:
            self._best = max(self._best, touch) if side == Side.LONG else min(self._best, touch)
        atr = self._atr.value
        stop = self._stop_order()
        if atr is None or stop is None or self._trail_pending:
            return
        if (
            stop.status != NautilusOrderStatus.ACCEPTED
            or stop.is_inflight
            or stop.is_pending_update
            or stop.is_pending_cancel
        ):
            return
        now = self.clock.timestamp_ns()
        if now - self._trail_last_ns < self._six.trail_min_interval_s * NS_PER_S:
            return
        instrument = self._instrument()
        tick_size = instrument.price_increment.as_decimal()
        level = trail_level(side, self._best, atr, trail_atr, tick_size)
        current = stop.trigger_price.as_decimal()
        if not trail_moves(side, level, current, max(self._six.trail_min_step_atr * atr, tick_size)):
            return
        self._trail_pending = True
        self._trail_old = current
        self._trail_last_ns = now
        self.modify_order(stop, trigger_price=instrument.make_price(level))

    def on_order_updated(self, event: OrderUpdated) -> None:
        """A moved stop: ``_stop_px`` follows its new trigger, and a trail modify is no longer pending."""
        if event.client_order_id != self._stop_id:
            return
        if self._trail_pending:
            self._trail_pending = False
            self._trail_refusals = 0
        if event.trigger_price is None:
            return
        new = event.trigger_price.as_decimal()
        if new == self._stop_px:
            return
        old, self._stop_px = self._stop_px, new
        log.info(
            "stop trailed",
            extra={"fields": {"code": "stop_trailed", "old": str(old), "new": str(new), "best": str(self._best)}},
        )

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        """The base's log, then a refused trail modify is counted; enough in a row disable the trail."""
        super().on_order_modify_rejected(event)
        if event.client_order_id != self._stop_id or not self._trail_pending:
            return
        self._trail_pending = False
        self._trail_refusals += 1
        log.info(
            "stop trail refused",
            extra={"fields": {"code": "stop_trail_rejected", "reason": str(event.reason), "old": str(self._trail_old)}},
        )
        if self._trail_refusals >= self._six.trail_max_refusals:
            self._trail_disabled = True
            log.warning(
                "stop trail disabled for this trade",
                extra={"fields": {"code": "stop_trail_disabled", "refusals": self._trail_refusals}},
            )
