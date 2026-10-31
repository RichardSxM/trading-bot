"""covers: spec 0026 AC-1 to AC-12 (the maker base ``MakerBarRuleStrategy``): the entry limit and its sizing, the
entry wait, partial fills and the stop that follows them, post only rejections, the maker exit and its market
fallback, the market only paths, one order in flight, the kill switch, and the restart.

Runs the base in a BacktestEngine on synthetic trades and quotes (``test_node_bar_rule``'s instrument and
scripted rule) on 1 minute bars, with a 2 bar ATR, filling on quotes only. Prices move 60 a minute, so the ATR
is about 60 and an entry limit rests about 30 behind the close. Small quote sizes with liquidity consumption
on give part fills. A few tests swap in a
subclass that misbehaves on purpose (a crossing exit price, a swallowed cancel). Needs the ``node`` extra.
"""

import asyncio
import itertools
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled, OrderUpdated
from nautilus_trader.model.identifiers import TradeId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_bar_rule import INSTRUMENT, INSTRUMENT_ID, START, Script, _at, _ns

from trading_core.node.bar_rule_maker import (
    MakerBarRuleConfig,
    MakerBarRuleStrategy,
    Phase,
    entry_limit_px,
    first_fill_through,
)
from trading_core.node.reconcile import resting_stop
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.mappers import DECISION_TAG, HALT_TAG
from trading_core.recording.rows import DecisionRow, RiskEventRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import Side
from trading_core.strategy.blocks import BlockReason, EntryBlocks

D = Decimal
HALT_ID = uuid.UUID(int=9)
LONG = {5: (Side.LONG, 100)}  # a long at bar 5 (closes 12:04), structural stop 100 below the close


def knots(*points):
    """A price path through ``(minute, price)`` points, straight between them, flat after the last."""

    def price(t):
        for (t0, p0), (t1, p1) in itertools.pairwise(points):
            if t0 <= t <= t1:
                return int(p0 + (p1 - p0) * (t - t0) / (t1 - t0))
        return points[-1][1]

    return price


RISE = knots((0, 85000), (30, 86800))  # a buy limit behind the close never fills
FILL = knots((0, 85000), (4, 85240), (4.5, 85190), (30, 86720))  # dips through the limit, then rises
FILL_FLAT = knots((0, 85000), (4, 85240), (4.5, 85190), (5, 85220), (40, 85220))  # an exit limit at the ask rests
FILL_DROP = knots((0, 85000), (4, 85240), (4.5, 85190), (5, 85220), (9, 85220), (9.2, 84900), (40, 84900))
TOUCH = knots((0, 85000), (4, 85240), (4.2, 85205), (4.4, 85205), (5, 85300), (30, 86800))  # a brief touch


def _data(prices, minutes, size="5.0000", every_s=10, quotes_until=None, quote_shift=0):
    """A trade and a quote (1 either side of the mid, moved by ``quote_shift``) every ``every_s`` seconds."""
    data, at = [], START
    for step in range(int(minutes * 60 / every_s)):
        t = step * every_s / 60
        mid, ts = prices(t), _ns(at)
        size_in = Quantity.from_str(size)
        data.append(TradeTick(INSTRUMENT_ID, Price(mid, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER,
                              TradeId(f"T{step}"), ts, ts))  # fmt: skip
        if quotes_until is None or t < quotes_until:
            bid, ask = mid - 1 + quote_shift, mid + 1 + quote_shift
            data.append(QuoteTick(INSTRUMENT_ID, Price(bid, 0), Price(ask, 0), size_in, size_in, ts, ts))
        at += timedelta(seconds=every_s)
    return data


def _fields(**overrides):
    return {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "1-MINUTE-LAST",
        "atr_period": 2,
        "time_stop_bars": 10,
        "use_market_context": False,
        "max_order_notional_usd": D(50000),
        "max_leverage": D(5),
        "max_total_exposure_usd": D(50000),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
        "stale_quote_s": D(600),
    } | overrides


def _run(
    rule, prices=FILL, minutes=20, strategy_cls=MakerBarRuleStrategy, data=None, gate=None, consume=False, **overrides
):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(  # fills on quotes only, as in the candle lab and the demo sandbox
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(10000, USD)],
        base_currency=USD,
        trade_execution=False,
        bar_execution=False,
        liquidity_consumption=consume,  # on: each quote's size fills once, so small sizes give part fills
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(data if data is not None else _data(prices, minutes))
    records: list[WriteItem] = []
    values: dict[str, Any] = _fields(**overrides)
    strategy = strategy_cls(MakerBarRuleConfig(**values), gate or PauseGate(), EntryBlocks(), records.append, rule)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows):
    return [r for r in rows if isinstance(r, DecisionRow)]


def _risk_events(rows):
    return [r for r in rows if isinstance(r, RiskEventRow)]


def _orders(engine, order_type):
    return [o for o in engine.cache.orders() if o.order_type == order_type]


def _entry(engine):
    return next(o for o in _orders(engine, OrderType.LIMIT) if not o.is_reduce_only)


def _exit_limits(engine):
    return [o for o in _orders(engine, OrderType.LIMIT) if o.is_reduce_only]


def _codes(caplog):
    return [(f.get("code"), f.get("why")) for r in caplog.records if (f := getattr(r, "fields", {})).get("code")]


def _filled_ns(order):
    return [e.ts_event for e in order.events if isinstance(e, OrderFilled)]


def _closed_ns(order):
    return order.events[-1].ts_event


# The entry limit (AC-2, AC-3) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "close", "tick", "expected"),
    [
        (Side.LONG, D(100), D("0.5"), D("99.0")),
        (Side.SHORT, D(100), D("0.5"), D("101.0")),
        (Side.LONG, D("100.3"), D("0.5"), D("99.0")),  # 99.3 rounds down, away from the market
        (Side.SHORT, D("100.3"), D("0.5"), D("101.5")),  # 101.3 rounds up
    ],
)
def test_the_entry_limit_is_half_an_atr_behind_the_close_rounded_away_from_the_market(side, close, tick, expected):
    assert entry_limit_px(side, close, D(2), D("0.5"), tick) == expected


def test_an_approved_entry_is_a_post_only_gtc_limit_sized_from_its_own_price():
    engine, _, rows = _run(Script(entries=LONG), prices=RISE)

    [enter] = _decisions(rows)
    inputs = enter.inputs
    assert (enter.intent, enter.risk_verdict) == (Intent.ENTER, RiskVerdict.APPROVED)
    limit_px = D(str(inputs["limit_px"]))
    expected = entry_limit_px(Side.LONG, D(str(inputs["bar_close"])), D(str(inputs["atr"])), D("0.5"), D(1))
    assert limit_px == expected
    assert D(str(inputs["entry_px"])) == limit_px  # check_entry sized it from the limit, not the mid
    assert D(str(inputs["mid"])) > limit_px
    assert D(str(inputs["entry_offset_atr"])) == D("0.5")
    assert {"stop", "structural_stop", "floor_applied", "stop_px"} <= set(inputs)
    entry = _entry(engine)
    assert (entry.is_post_only, entry.time_in_force, entry.price.as_decimal()) == (True, TimeInForce.GTC, limit_px)
    assert entry.quantity.as_decimal() == D(str(inputs["quantity"]))
    assert entry.tags == [f"{DECISION_TAG}{enter.id}"]


def test_a_limit_not_above_a_long_s_stop_is_refused_as_bad_signal_with_no_order():
    engine, _, rows = _run(Script(entries=LONG), prices=RISE, entry_offset_atr=D(5))

    [refused] = _decisions(rows)
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "bad_signal")
    assert D(str(refused.inputs["limit_px"])) <= D(str(refused.inputs["stop"]))
    assert engine.cache.orders() == []


# The entry wait (AC-4) ----------------------------------------------------------------------


def test_an_unfilled_entry_is_cancelled_at_its_third_bar_close_and_the_next_signal_can_enter(caplog):
    rule = Script(entries={5: (Side.LONG, 100), 9: (Side.LONG, 100)})
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(rule, prices=RISE)

    first, second = (o for o in _orders(engine, OrderType.LIMIT))
    assert first.status == OrderStatus.CANCELED
    assert _closed_ns(first) == _at(8)  # submitted at bar 5, cancelled at the start of bar 8
    assert ("maker_entry_timeout", None) in _codes(caplog)
    assert _risk_events(rows) == []
    assert [d.decided_ns for d in _decisions(rows)] == [_at(5), _at(9)]
    assert second.status == OrderStatus.CANCELED
    assert _closed_ns(second) == _at(12)
    assert strategy.phase == Phase.FLAT


def test_the_wait_follows_entry_wait_bars():
    engine, _, _ = _run(Script(entries=LONG), prices=RISE, entry_wait_bars=1)
    assert _closed_ns(_entry(engine)) == _at(6)


# Fills and the stop (AC-5) ------------------------------------------------------------------


def test_a_filled_entry_rests_a_reduce_only_stop_market_for_its_quantity():
    engine, strategy, rows = _run(Script(entries=LONG), minutes=8)

    entry = _entry(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    enter = _decisions(rows)[0]
    assert entry.status == OrderStatus.FILLED
    assert (stop.is_reduce_only, stop.quantity, stop.status) == (True, entry.quantity, OrderStatus.ACCEPTED)
    assert stop.trigger_price.as_decimal() == D(str(enter.inputs["stop"]))
    assert strategy.phase == Phase.PROTECTED


def test_partial_fills_move_the_stop_to_the_position_s_quantity():
    engine, _, _ = _run(Script(entries=LONG), data=_data(FILL, 10, size="0.0500"), consume=True)

    entry = _entry(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    fills = [e for e in entry.events if isinstance(e, OrderFilled)]
    assert len(fills) > 2
    assert stop.quantity == entry.filled_qty
    assert [e for e in stop.events if isinstance(e, OrderUpdated)]
    assert stop.status == OrderStatus.ACCEPTED


def test_a_part_filled_entry_keeps_its_part_at_the_wait_with_no_risk_event(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(Script(entries=LONG), data=_data(TOUCH, 10, size="0.0500"), consume=True)

    entry = _entry(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    [position] = engine.cache.positions_open()
    assert entry.status == OrderStatus.CANCELED
    assert _closed_ns(entry) == _at(8)
    assert D(0) < entry.filled_qty.as_decimal() < entry.quantity.as_decimal()
    assert position.quantity == entry.filled_qty == stop.quantity
    assert _risk_events(rows) == []
    assert ("maker_entry_timeout", None) not in _codes(caplog)
    assert strategy.phase == Phase.PROTECTED


def test_bars_held_counts_from_the_first_fill_so_the_time_stop_runs_from_there():
    engine, _, rows = _run(Script(entries=LONG), prices=FILL_FLAT, minutes=25, time_stop_bars=4)

    first_fill = _filled_ns(_entry(engine))[0]
    exit_ = _decisions(rows)[-1]
    assert (exit_.intent, exit_.reason, exit_.inputs["bars_held"]) == (Intent.EXIT, "time_stop", 4)
    # Filled after bar 5 closed (12:04): the 4th bar close after it is bar 9, not bar 5 + 4 counted from the submit.
    assert _at(5) < first_fill < _at(6)
    assert exit_.decided_ns == _at(9)


# Post only rejections (AC-6) ----------------------------------------------------------------


def _crossing():
    # Quotes 6 below the trades: a buy limit at the close (no offset) is above the ask.
    return _data(RISE, 20, quote_shift=-6)


def test_a_post_only_rejected_entry_goes_flat_with_no_risk_event_and_no_retry(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(Script(entries=LONG), data=_crossing(), entry_offset_atr=D(0))

    [entry] = engine.cache.orders()
    assert entry.status == OrderStatus.REJECTED
    assert ("maker_entry_rejected", None) in _codes(caplog)
    assert _risk_events(rows) == []
    assert len(_decisions(rows)) == 1
    assert strategy.phase == Phase.FLAT


class _NoPostOnlyFlag(MakerBarRuleStrategy):
    """A venue that does not set ``due_post_only`` on its rejections."""

    def on_order_rejected(self, event):
        self._refused(event.client_order_id, f"rejected: {event.reason}", post_only=False)


def test_a_rejection_without_the_post_only_flag_raises_the_warning_risk_event():
    _, strategy, rows = _run(
        Script(entries=LONG), data=_crossing(), entry_offset_atr=D(0), strategy_cls=_NoPostOnlyFlag
    )

    [event] = _risk_events(rows)
    assert (event.kind, event.severity) == (RiskEventKind.ORDER_REJECTED, Severity.WARNING)
    assert strategy.phase == Phase.FLAT


# The maker exit (AC-7) ----------------------------------------------------------------------


def test_a_planned_exit_rests_a_reduce_only_post_only_limit_at_the_ask_with_the_stop_still_resting():
    engine, strategy, rows = _run(Script(entries=LONG, exits={8}))

    exit_ = _decisions(rows)[-1]
    [limit] = _exit_limits(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    assert (exit_.intent, exit_.reason, exit_.decided_ns) == (Intent.EXIT, "target", _at(8))
    assert exit_.inputs["exit_mode"] == "maker"
    assert limit.price.as_decimal() == D(str(exit_.inputs["exit_limit_px"]))
    assert D(str(exit_.inputs["exit_limit_px"])) > D(str(exit_.inputs["mid"]))  # the ask, for a long
    assert (limit.is_post_only, limit.is_reduce_only, limit.status) == (True, True, OrderStatus.FILLED)
    assert limit.tags == [f"{DECISION_TAG}{exit_.id}"]
    assert stop.status == OrderStatus.CANCELED
    assert _closed_ns(stop) >= _filled_ns(limit)[-1]  # cancelled only once the position closed
    assert _orders(engine, OrderType.MARKET) == []
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_part_filled_exit_limit_shrinks_the_stop_to_the_rest():
    engine, _, _ = _run(Script(entries=LONG, exits={8}), data=_data(FILL, 20, size="0.0500"), consume=True)

    [limit] = _exit_limits(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    updates = [e.quantity for e in stop.events if isinstance(e, OrderUpdated)]
    assert len(_filled_ns(limit)) > 1
    assert updates[-1] < limit.quantity  # the stop followed the exit limit's part fills down
    assert engine.cache.positions_open() == []


def test_a_stale_quote_sends_the_planned_exit_at_market():
    data = _data(FILL, 20, quotes_until=6.5)
    engine, _, rows = _run(Script(entries=LONG, exits={9}), data=data, stale_quote_s=D(60))

    exit_ = _decisions(rows)[-1]
    assert (exit_.inputs["exit_mode"], exit_.inputs["why"]) == ("market", "stale_quote")
    assert _exit_limits(engine) == []
    [market] = _orders(engine, OrderType.MARKET)
    assert market.is_reduce_only
    assert market.tags == [f"{DECISION_TAG}{exit_.id}"]


# The exit fallback (AC-8) -------------------------------------------------------------------


def test_an_exit_limit_unfilled_after_three_bars_is_replaced_by_market_under_the_same_decision(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT)

    exits = [d for d in _decisions(rows) if d.intent == Intent.EXIT]
    [limit] = _exit_limits(engine)
    [market] = _orders(engine, OrderType.MARKET)
    assert len(exits) == 1
    assert limit.status == OrderStatus.CANCELED
    assert _closed_ns(limit) == _at(11)
    assert market.status == OrderStatus.FILLED
    assert market.is_reduce_only
    assert market.tags == [f"{DECISION_TAG}{exits[0].id}"]
    assert ("exit_fallback_market", "timeout") in _codes(caplog)
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


class _CrossingExit(MakerBarRuleStrategy):
    """Prices its exit limit at the bid, so a post only exit would take."""

    def _submit_exit_limit(self, position, limit_px):
        quote = self.cache.quote_tick(self._instrument_id)
        super()._submit_exit_limit(position, quote.bid_price.as_decimal())


def test_a_post_only_rejected_exit_limit_goes_to_market_at_once(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, rows = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_CrossingExit)

    [limit] = _exit_limits(engine)
    [market] = _orders(engine, OrderType.MARKET)
    assert limit.status == OrderStatus.REJECTED
    assert _filled_ns(market) == [_at(8)]
    assert ("exit_fallback_market", "post_only_rejected") in _codes(caplog)
    assert len([d for d in _decisions(rows) if d.intent == Intent.EXIT]) == 1


class _VenueCancelsExit(MakerBarRuleStrategy):
    """The venue cancels the exit limit as soon as it is accepted (a cancel this strategy did not ask for)."""

    def on_order_accepted(self, event):
        super().on_order_accepted(event)
        if event.client_order_id == self._exit_limit_id:
            self.cancel_order(self.cache.order(event.client_order_id))


def test_an_exit_limit_cancelled_without_asking_goes_to_market_at_once(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, _ = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_VenueCancelsExit)

    [market] = _orders(engine, OrderType.MARKET)
    assert _filled_ns(market) == [_at(8)]
    assert ("exit_fallback_market", "canceled") in _codes(caplog)


class _CancelLost(MakerBarRuleStrategy):
    """The venue never confirms the exit limit's cancel."""

    def cancel_order(self, order, client_id=None, params=None):
        if order.client_order_id == self._exit_limit_id:
            return
        super().cancel_order(order, client_id, params)


def test_with_no_cancel_confirmation_the_market_goes_after_the_five_second_guard(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, _ = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_CancelLost)

    [market] = _orders(engine, OrderType.MARKET)
    assert market.ts_init == _at(11) + 5_000_000_000
    assert ("exit_fallback_market", "timeout") in _codes(caplog)
    assert engine.cache.positions_open() == []


def test_a_stop_fill_while_the_exit_limit_rests_closes_with_no_market_order():
    engine, strategy, _ = _run(Script(entries=LONG, exits={8}), prices=FILL_DROP)

    [limit] = _exit_limits(engine)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    assert stop.status == OrderStatus.FILLED
    assert limit.status == OrderStatus.CANCELED
    assert _orders(engine, OrderType.MARKET) == []
    assert strategy.phase == Phase.FLAT


# Market only paths and one order in flight (AC-9, AC-10) ------------------------------------


class _StopAboveTheFill(MakerBarRuleStrategy):
    """The stop moves above the entry limit after the entry goes out, so the first fill is through it."""

    def _decide_entry(self, bar, atr, signal):
        super()._decide_entry(bar, atr, signal)
        if self._entry_id is not None:
            self._stop_px = bar.close + 100


def test_a_first_fill_through_the_stop_exits_at_market_as_stop_through_with_no_stop():
    engine, _, rows = _run(Script(entries=LONG), strategy_cls=_StopAboveTheFill)

    exit_ = _decisions(rows)[-1]
    assert (exit_.intent, exit_.reason, exit_.inputs["exit_mode"]) == (Intent.EXIT, "stop_through", "market")
    assert _orders(engine, OrderType.STOP_MARKET) == []
    assert _exit_limits(engine) == []
    [market] = _orders(engine, OrderType.MARKET)
    assert market.status == OrderStatus.FILLED


# The stop already through, and the rejected stop settled by a quote (AC-5, amended 2026-10-31) -------
# On FILL the entry fills at 85210 at 12:04:20, on a quote the strategy has not seen yet (the venue fills
# before the cache updates): its cached bid is then 85222, and the next quote's bid is 85205.

TWO_LONGS = {5: (Side.LONG, 100), 8: (Side.LONG, 100)}  # the second shows whether entries are still allowed


@pytest.mark.parametrize(
    ("side", "fill", "stop", "quote", "through"),
    [
        (Side.LONG, D("99.0"), D("94.0"), (D("93.5"), D("99.5")), True),  # the bid is through
        (Side.LONG, D("99.0"), D("94.0"), None, False),  # no quote: the fill alone decides
        (Side.LONG, D("94.0"), D("94.0"), None, True),
        (Side.LONG, D("99.0"), D("94.0"), (D("98.5"), D("99.5")), False),
        (Side.SHORT, D("99.0"), D("104.0"), (D("98.5"), D("104.0")), True),  # the ask is through
        (Side.SHORT, D("99.0"), D("104.0"), None, False),
        (Side.SHORT, D("104.5"), D("104.0"), (D("98.5"), D("99.5")), True),
    ],
)
def test_first_fill_through_reads_the_fill_and_the_cached_quote(side, fill, stop, quote, through):
    assert first_fill_through(side, fill, stop, quote) is through


def _wide_before_the_fill(data):
    """FILL's data with the quote just before the fill (12:04:10) wide: bid 85150, ask 85230."""
    at = _ns(START + timedelta(seconds=250))
    size = Quantity.from_str("5.0000")
    wide = QuoteTick(INSTRUMENT_ID, Price(85150, 0), Price(85230, 0), size, size, at, at)
    return [wide if isinstance(d, QuoteTick) and d.ts_event == at else d for d in data]


class _StopInTheSpread(MakerBarRuleStrategy):
    """The stop moves just under the entry limit (85207), above the cached bid of the wide quote."""

    def _decide_entry(self, bar, atr, signal):
        super()._decide_entry(bar, atr, signal)
        if self._entry_id is not None:
            self._stop_px = self.cache.order(self._entry_id).price.as_decimal() - 3


def test_a_first_fill_with_the_bid_through_the_stop_exits_at_market_and_entries_go_on(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(
            Script(entries=TWO_LONGS), data=_wide_before_the_fill(_data(FILL, 20)), strategy_cls=_StopInTheSpread
        )

    exit_ = next(d for d in _decisions(rows) if d.reason == "stop_through")
    assert D(exit_.inputs["bid"]) <= D(exit_.inputs["stop_px"]) < D(exit_.inputs["entry_px"])
    assert exit_.inputs["exit_mode"] == "market"
    assert all(o.ts_init > exit_.decided_ns for o in _orders(engine, OrderType.STOP_MARKET))  # the second's
    assert _risk_events(rows) == []
    assert not strategy._blocks.active()
    assert len(_approved_entries(rows)) == 2
    assert ("stop_through", None) in _codes(caplog)


class _StopRejectedAt(MakerBarRuleStrategy):
    """The venue rejects its stop (a trigger above the ask), while the strategy judges by ``bid + view``."""

    view = 0

    def _submit_stop(self, position):
        quote = self.cache.quote_tick(self._instrument_id)
        self._stop_px = quote.bid_price.as_decimal() + self.view
        order = self.order_factory.stop_market(
            self._instrument_id,
            OrderSide.SELL,
            position.quantity,
            trigger_price=Price(quote.ask_price.as_decimal() + 50, 0),
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
        )
        self._stop_id = order.client_order_id
        self.submit_order(order)


class _RejectedThroughNow(_StopRejectedAt):
    view = 1  # the cached bid (85222) is at or below the stop (85223)


class _RejectedThroughNext(_StopRejectedAt):
    view = -10  # the cached bid is above the stop (85212), the next one (85205) is through


class _RejectedNotThrough(_StopRejectedAt):
    view = -100  # no quote reaches the stop (85122)


def _approved_entries(rows):
    return [d for d in _decisions(rows) if d.intent == Intent.ENTER and d.risk_verdict == RiskVerdict.APPROVED]


def _stop_blocks(rows):
    return [e for e in _risk_events(rows) if e.event_key.startswith("stop_not_accepted:")]


def test_a_stop_rejected_with_the_cached_bid_through_exits_once_as_stop_through_with_no_block(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(Script(entries=TWO_LONGS), strategy_cls=_RejectedThroughNow)

    first_market = _orders(engine, OrderType.MARKET)[0]
    exit_ = next(d for d in _decisions(rows) if d.reason == "stop_through")
    assert first_market.status == OrderStatus.FILLED
    assert first_market.tags == [f"{DECISION_TAG}{_decisions(rows)[0].id}"]  # no exit decision of its own
    assert exit_.risk_verdict == RiskVerdict.APPROVED
    assert _stop_blocks(rows) == []
    assert not strategy._blocks.active()
    assert ("stop_rejected", None) in _codes(caplog)
    assert len(_approved_entries(rows)) == 2


def test_a_stop_rejected_with_the_next_quote_through_records_stop_through_and_entries_go_on():
    _, strategy, rows = _run(Script(entries=TWO_LONGS), strategy_cls=_RejectedThroughNext)

    [exit_] = [d for d in _decisions(rows) if d.reason == "stop_through"]
    assert D(exit_.inputs["bid"]) == 85205
    assert _stop_blocks(rows) == []
    assert not strategy._blocks.active()
    assert len(_approved_entries(rows)) == 2


def test_a_stop_rejected_with_no_quote_through_blocks_entries_with_a_critical_event():
    engine, strategy, rows = _run(Script(entries=TWO_LONGS), strategy_cls=_RejectedNotThrough)

    [event] = _stop_blocks(rows)
    assert (event.kind, event.severity) == (RiskEventKind.ORDER_REJECTED, Severity.CRITICAL)
    assert BlockReason.STOP_NOT_ACCEPTED in strategy._blocks.active()
    assert [d for d in _decisions(rows) if d.reason == "stop_through"] == []
    second = _decisions(rows)[-1]
    assert (second.intent, second.risk_reason) == (Intent.ENTER, "blocked:stop_not_accepted")
    [market] = _orders(engine, OrderType.MARKET)
    assert market.status == OrderStatus.FILLED


class _RejectedAfterTheQuotes(MakerBarRuleStrategy):
    """Its stop is rejected (by hand: the venue accepted it) at the first bar close in the position."""

    def on_bar(self, bar):
        if self._stop_id is not None and self.bars_held >= 1:
            self._stop_px = Decimal(1)  # never through, but no quote comes to say so
            self._stop_rejected("stop rejected: by hand")
        super().on_bar(bar)


def test_a_rejected_stop_waiting_for_a_quote_refuses_entries_after_the_close_and_blocks_on_stop():
    data = _data(FILL_FLAT, 20, quotes_until=4.4)  # the fill's quote is the last one
    engine, strategy, rows = _run(Script(entries=TWO_LONGS), data=data, strategy_cls=_RejectedAfterTheQuotes)

    refused = _decisions(rows)[-1]
    assert (refused.intent, refused.risk_reason) == (Intent.ENTER, "blocked:stop_not_accepted")
    assert engine.cache.positions_open() == []
    [event] = _stop_blocks(rows)  # recorded by on_stop, after the refusal
    assert event.occurred_ns > refused.decided_ns
    assert BlockReason.STOP_NOT_ACCEPTED in strategy._blocks.active()


class _RejectedWhileExitRests(MakerBarRuleStrategy):
    """Its stop is rejected (by hand: the venue accepted it) just after the exit limit goes out."""

    def _submit_exit_limit(self, position, limit_px):
        super()._submit_exit_limit(position, limit_px)
        if self._stop_id is not None:
            self._stop_px = Decimal(1)  # never through, so the verdict is a block
            self._stop_rejected("stop rejected: by hand")


def test_a_stop_rejected_while_the_exit_limit_rests_cancels_it_and_sends_one_market(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(
            Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_RejectedWhileExitRests
        )

    [limit] = _exit_limits(engine)
    assert limit.status == OrderStatus.CANCELED
    [market] = _orders(engine, OrderType.MARKET)
    assert market.status == OrderStatus.FILLED
    assert "exit_fallback_market" not in [code for code, _ in _codes(caplog)]
    assert "maker_exit_cancel" not in strategy.clock.timer_names
    assert len(_stop_blocks(rows)) == 1


class _FlattenThenRejected(_RejectedNotThrough):
    """The kill switch closes the position just before the stop that will be rejected goes out."""

    def _submit_stop(self, position):
        self.flatten(HALT_ID)
        super()._submit_stop(position)


def test_a_stop_rejected_during_a_kill_switch_close_sends_no_second_market():
    engine, _, _ = _run(Script(entries=LONG), strategy_cls=_FlattenThenRejected)

    [market] = _orders(engine, OrderType.MARKET)
    assert f"{HALT_TAG}{HALT_ID}" in market.tags
    assert engine.cache.positions_open() == []


class _StopNeverSent(MakerBarRuleStrategy):
    """Its stop never reaches the venue, so it is not accepted in time."""

    def submit_order(self, order, position_id=None, client_id=None, params=None):
        if order.order_type == OrderType.STOP_MARKET:
            return
        super().submit_order(order, position_id, client_id, params)


def test_a_stop_not_accepted_in_time_still_blocks():
    engine, strategy, rows = _run(Script(entries=LONG), strategy_cls=_StopNeverSent)

    assert len(_stop_blocks(rows)) == 1
    assert BlockReason.STOP_NOT_ACCEPTED in strategy._blocks.active()
    [market] = _orders(engine, OrderType.MARKET)
    assert market.status == OrderStatus.FILLED


def test_both_stop_through_rows_carry_the_same_inputs():
    _, _, first_fill = _run(
        Script(entries=LONG), data=_wide_before_the_fill(_data(FILL, 20)), strategy_cls=_StopInTheSpread
    )
    _, _, rejected = _run(Script(entries=LONG), strategy_cls=_RejectedThroughNow)

    [at_fill] = [d for d in _decisions(first_fill) if d.reason == "stop_through"]
    [late] = [d for d in _decisions(rejected) if d.reason == "stop_through"]
    assert set(at_fill.inputs) - {"position_qty", "exit_mode"} == set(late.inputs)
    assert set(late.inputs) == {"stop_px", "bid", "ask", "mid", "entry_px", "bars_held", "bar_close"}


def test_a_signal_while_the_entry_rests_is_consumed_and_dropped():
    rule = Script(entries={5: (Side.LONG, 100), 6: (Side.LONG, 100)})
    engine, _, rows = _run(rule, prices=RISE)

    assert len(_decisions(rows)) == 1
    assert len(_orders(engine, OrderType.LIMIT)) == 1
    assert rule.consumed_at[:2] == [5, 6]


def test_an_exit_decided_stands_while_its_limit_rests():
    engine, _, rows = _run(Script(entries=LONG, exits={8, 9}), prices=FILL_FLAT)

    assert len([d for d in _decisions(rows) if d.intent == Intent.EXIT]) == 1
    assert len(_exit_limits(engine)) == 1


class _EntryCancelLost(MakerBarRuleStrategy):
    """The entry's cancel never reaches the venue, so its remainder can still fill after the exit."""

    def cancel_order(self, order, client_id=None, params=None):
        if order.client_order_id == self._entry_id:
            return
        super().cancel_order(order, client_id, params)


TOUCH_TWICE = knots((0, 85000), (4, 85240), (4.2, 85205), (4.4, 85205), (5, 85300), (7.2, 85300), (7.5, 85180),
                    (8, 85300), (30, 85300))  # fmt: skip


def test_an_exit_with_the_entry_part_filled_retires_it_and_a_late_fill_closes_at_market():
    data = _data(TOUCH_TWICE, 20, size="0.0500")
    engine, _, rows = _run(Script(entries=LONG, exits={7}), data=data, strategy_cls=_EntryCancelLost, consume=True)

    late = [d for d in _decisions(rows) if d.reason == "late_entry_fill"]
    assert late
    late_markets = [o for o in _orders(engine, OrderType.MARKET) if o.tags == [f"{DECISION_TAG}{late[0].id}"]]
    assert late_markets
    assert late_markets[0].is_reduce_only
    assert engine.cache.positions_open() == []


class _RejectedCancelLost(_RejectedNotThrough, _EntryCancelLost):
    """A part filled entry whose remainder cancel never arrives, under a rejected stop."""


def test_a_part_filled_entrys_remainder_after_a_rejected_stop_closes_as_a_late_fill():
    data = _data(TOUCH_TWICE, 20, size="0.0500")
    engine, _, rows = _run(Script(entries=LONG), data=data, strategy_cls=_RejectedCancelLost, consume=True)

    assert [d for d in _decisions(rows) if d.reason == "late_entry_fill"]
    assert engine.cache.positions_open() == []


# The kill switch (AC-11) --------------------------------------------------------------------


class _FlattenAt(MakerBarRuleStrategy):
    """Calls ``flatten`` right after the bar closes in ``at`` (every one of them, as the kill switch polls)."""

    at: tuple[int, ...] = ()

    def on_bar(self, bar):
        super().on_bar(bar)
        self.closes = getattr(self, "closes", 0) + 1
        if self.closes in type(self).at:
            self.flatten(HALT_ID)


def _kill_switch_markets(engine):
    return [o for o in _orders(engine, OrderType.MARKET) if f"{HALT_TAG}{HALT_ID}" in (o.tags or [])]


def test_flatten_with_an_exit_limit_resting_cancels_it_and_sends_one_market_close():
    _FlattenAt.at = (9, 10)
    engine, _, _ = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_FlattenAt)

    [limit] = _exit_limits(engine)
    assert limit.status == OrderStatus.CANCELED
    assert _closed_ns(limit) == _at(9)
    [close] = _kill_switch_markets(engine)
    assert close.status == OrderStatus.FILLED
    assert len(_orders(engine, OrderType.MARKET)) == 1
    assert engine.cache.positions_open() == []


def test_flatten_during_the_cancel_guard_sends_one_market_close_and_the_guard_none():
    class _FlattenInGuard(_CancelLost, _FlattenAt):
        at = (11,)

    engine, _, _ = _run(Script(entries=LONG, exits={8}), prices=FILL_FLAT, strategy_cls=_FlattenInGuard)

    [close] = _orders(engine, OrderType.MARKET)
    assert close.ts_init == _at(11)
    assert f"{HALT_TAG}{HALT_ID}" in close.tags


def test_flatten_with_the_entry_part_filled_cancels_the_rest_and_closes_with_no_timeout_log(caplog):
    _FlattenAt.at = (6,)
    with caplog.at_level(logging.INFO):
        data = _data(TOUCH, 20, size="0.0500")
        engine, _, _ = _run(Script(entries=LONG), data=data, strategy_cls=_FlattenAt, consume=True)

    entry = _entry(engine)
    assert entry.status == OrderStatus.CANCELED
    assert _closed_ns(entry) == _at(6)
    assert D(0) < entry.filled_qty.as_decimal() < entry.quantity.as_decimal()
    [close] = _kill_switch_markets(engine)
    assert close.quantity == entry.filled_qty
    assert ("maker_entry_timeout", None) not in _codes(caplog)
    assert engine.cache.positions_open() == []


def test_flatten_while_flat_with_an_entry_resting_cancels_it_and_leaves_no_tags():
    _FlattenAt.at = (6,)
    engine, strategy, _ = _run(Script(entries=LONG), prices=RISE, strategy_cls=_FlattenAt)

    assert _entry(engine).status == OrderStatus.CANCELED
    assert _orders(engine, OrderType.MARKET) == []
    assert strategy._kill_switch_tags is None
    assert strategy.phase == Phase.FLAT


# The restart (AC-12) ------------------------------------------------------------------------


def _min(minutes):
    return _ns(START + timedelta(minutes=minutes))


class _Restart(MakerBarRuleStrategy):
    """Loses its round trip state at ``forget_min`` and is handed the open position at ``resume_min``."""

    forget_min = 0.0
    resume_min = 0.0

    def on_start(self):
        super().on_start()
        self.clock.set_time_alert_ns("test_forget", _min(type(self).forget_min), lambda e: self._reset())
        self.clock.set_time_alert_ns("test_resume", _min(type(self).resume_min), self._resume)

    def _resume(self, _event):
        (position,) = self.cache.positions_open()
        self.resume_position(position, resting_stop(self.cache, position))


def test_a_resumed_position_has_its_exit_limit_cancelled_and_keeps_its_stop():
    _Restart.forget_min, _Restart.resume_min = 7.5, 7.6
    engine, _, rows = _run(Script(entries=LONG, exits={7}), prices=FILL_FLAT, strategy_cls=_Restart)

    first_limit = _exit_limits(engine)[0]
    assert first_limit.status == OrderStatus.CANCELED
    assert _closed_ns(first_limit) == _min(7.6)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    assert stop.status in (OrderStatus.ACCEPTED, OrderStatus.CANCELED)
    exits = [d for d in _decisions(rows) if d.intent == Intent.EXIT]
    assert exits[0].decided_ns == _at(7)
    assert _min(7.6) not in [d.decided_ns for d in exits]  # the cancel at the restart records no decision
    assert all(d.decided_ns > _min(7.6) for d in exits[1:])  # the exit step decides again from the next bar


def test_a_resumed_position_past_its_time_stop_exits_at_once_at_market():
    _Restart.forget_min, _Restart.resume_min = 6.5, 12.5
    engine, _, rows = _run(Script(entries=LONG), prices=FILL_FLAT, strategy_cls=_Restart, time_stop_bars=3)

    exit_ = _decisions(rows)[-1]
    assert (exit_.reason, exit_.decided_ns) == ("time_stop", _min(12.5))
    assert (exit_.inputs["exit_mode"], exit_.inputs["resumed"]) == ("market", True)
    assert engine.cache.positions_open() == []
    assert _exit_limits(engine) == []


# Config (AC-1) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("entry_offset_atr", D("-0.1"), "entry_offset_atr"),
        ("entry_wait_bars", 0, "entry_wait_bars"),
        ("exit_wait_bars", 0, "exit_wait_bars"),
        ("atr_min_bps", D(-1), "atr_min_bps"),
        ("time_stop_bars", 0, "time_stop_bars"),
        ("bar_spec", "1-TICK-LAST", "bar_spec"),
    ],
)
def test_the_maker_config_refuses_bad_values(field, value, problem):
    with pytest.raises(ValueError, match=problem):
        MakerBarRuleConfig(**_fields(**{field: value}))


def test_the_phases_carry_the_frozen_base_s_values():
    from trading_core.node.bar_rule import Phase as FrozenPhase

    assert [p.value for p in Phase] == [p.value for p in FrozenPhase]
