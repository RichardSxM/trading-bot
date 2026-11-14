"""covers: spec 0030 AC-3 (flatten across coins, ``has_open_positions``, ``resume_position``), AC-4 (the schedule,
the start cooldown, ``close_missing``, the resume rebalance), AC-5 (reductions first, flips, clips, the
liquidation refusal, held grows, denials), AC-6 (retries and ``failed``), AC-7 (one decision per coin, fills
linked), AC-9 (adopting a position).

Runs ``TrendPortfolio`` in a BacktestEngine with two coins, BTC (``PF_XBTUSD``, mid 80000) and ETH
(``PF_ETHUSD``, mid 3000), each with a trade and a quote every 10 s. A scripted subclass replaces the rule with
fixed target notionals, so each test names the book it wants; later rounds rebalance at set minutes. Needs the
``node`` extra.
"""

import asyncio
import functools
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest

pytest.importorskip("nautilus_trader")

import itertools

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderSide
from nautilus_trader.model.identifiers import TradeId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_bar_rule import INSTRUMENT
from test_node_six_vote_trend import COIN, COIN_ID

from trading_core.control.pause import PauseState
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_portfolio import WEEKLY, Leg, TrendPortfolio, TrendPortfolioConfig
from trading_core.recording.enums import ControlSource, Intent, RiskVerdict
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.trend_portfolio import CoinPlan, PortfolioPlan

D = Decimal
NS_PER_S = 1_000_000_000
MINUTE_NS = 60 * NS_PER_S
BTC = str(INSTRUMENT.id)
ETH = str(COIN_ID)
COINS = tuple(sorted((BTC, ETH)))
PRICES = {BTC: 80000, ETH: 3000}
START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # a Sunday
HALT_ID = uuid.UUID(int=11)


def _ns(at):
    return int(at.timestamp() * NS_PER_S)


def _ticks(coin, start, minutes, every_s=10):
    data, at = [], start
    for step in range(int(minutes * 60 / every_s)):
        px, ts = PRICES[coin], _ns(at)
        iid = INSTRUMENT.id if coin == BTC else COIN_ID
        q = Quantity.from_str("5.0000")
        data.append(TradeTick(iid, Price(px, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER,
                              TradeId(f"{iid.symbol}-{step}"), ts, ts))  # fmt: skip
        data.append(QuoteTick(iid, Price(px - 1, 0), Price(px + 1, 0), q, q, ts, ts))
        at += timedelta(seconds=every_s)
    return data


def _fields(**overrides):
    return {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": BTC,
        "instruments": COINS,
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
        "quote_wait_s": 5,
    } | overrides


class _Planned(TrendPortfolio):
    """The node with fixed targets: ``first`` for the first plan, then ``later`` as (minute, targets or a call).

    ``stale`` coins get a close date a day before the plan's, as when their daily bar is missing.
    """

    first: ClassVar[dict[str, int]] = {}
    later: ClassVar[tuple[tuple[float, object], ...]] = ()
    stale: frozenset[str] = frozenset()
    t0_ns = _ns(START)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.targets = dict(type(self).first)
        self.sent = []

    def on_start(self):
        super().on_start()
        for i, (minute, action) in enumerate(type(self).later):
            at = type(self).t0_ns + int(minute * MINUTE_NS)
            self.clock.set_time_alert_ns(f"script{i}", at, functools.partial(self._script, action))

    def _script(self, action, event):
        if callable(action):
            action(self)
            return
        self.targets = dict(action)
        self._make_plan(self.plan_day or date(2026, 10, 3))
        self._rebalance(WEEKLY)

    def _make_plan(self, day):
        equity = D(10000)
        coins = {}
        for coin in COINS:
            target = D(self.targets.get(coin, 0))
            when = day - timedelta(days=1) if coin in type(self).stale else day
            signal = 1 if target > 0 else -1 if target < 0 else 0
            coins[coin] = CoinPlan(True, signal, D("0.02"), target / equity, target, when)
        gross = sum((abs(c.weight) for c in coins.values()), D(0))
        self.plan = PortfolioPlan(coins, len(coins), gross, D(1), equity, day)
        self.plan_day = day
        return True

    def submit_order(self, order, *args, **kwargs):
        self.sent.append(_Sent(self.clock.timestamp_ns(), str(order.instrument_id), order.side,
                               order.quantity.as_decimal(), order.is_reduce_only, order))  # fmt: skip
        super().submit_order(order, *args, **kwargs)


class _Sent(SimpleNamespace):
    def __init__(self, ts, coin, side, qty, reduce_only, order):
        super().__init__(ts=ts, coin=coin, side=side, qty=qty, reduce_only=reduce_only, order=order)


def _plan(name, base=_Planned, **attrs):
    return type(name, (base,), attrs)


def _run(cls, minutes=4, start=START, gate=None, last_start_ns=None, **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(10000, USD)],
        base_currency=USD,
        trade_execution=False,
        bar_execution=False,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_instrument(COIN)
    for coin in COINS:
        engine.add_data(_ticks(coin, start, minutes))
    if gate is None:
        gate = PauseGate()
        gate.unpause()  # the pause key was read: no pause
    records: list[WriteItem] = []
    cls.t0_ns = _ns(start)
    strategy = cls(TrendPortfolioConfig(**_fields(**overrides)), gate, EntryBlocks(), records.append, None,
                   last_start_ns)  # fmt: skip
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def _logs(caplog, code):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("code") == code]


def _position(engine, coin):
    total = D(0)
    for position in engine.cache.positions_open():
        if str(position.instrument_id) == coin:
            total += position.signed_decimal_qty()
    return total


def _after(strategy, minute):
    return [s for s in strategy.sent if s.ts >= type(strategy).t0_ns + int(minute * MINUTE_NS)]


# Orders (AC-5, AC-7) ------------------------------------------------------------------------


def test_the_start_rebalance_buys_each_target_and_links_each_fill_to_its_decision():
    engine, strategy, rows = _run(_plan("P", first={BTC: 2000, ETH: -1500}))

    assert [(s.coin, s.side, s.qty, s.reduce_only) for s in strategy.sent] == [
        (ETH, OrderSide.SELL, D("0.5000"), False),
        (BTC, OrderSide.BUY, D("0.0250"), False),
    ]
    assert (_position(engine, BTC), _position(engine, ETH)) == (D("0.025"), D("-0.5"))
    assert {r.instrument_id: r.reason for r in rows} == {BTC: "start", ETH: "start"}
    for sent in strategy.sent:
        [row] = [r for r in rows if r.instrument_id == sent.coin]
        assert row.risk_verdict == RiskVerdict.APPROVED
        assert sent.order.tags == [f"{DECISION_TAG}{row.id}"]
        assert D(row.inputs["target_qty"]) in (D("0.0250"), D("-0.5000"))


def test_reductions_go_first_and_the_grows_wait_for_their_fills():
    cls = _plan("P", first={BTC: 2000}, later=((1, {BTC: 800, ETH: 1500}),))
    engine, strategy, rows = _run(cls)

    reduce_btc, grow_eth = _after(strategy, 1)
    assert (reduce_btc.coin, reduce_btc.side, reduce_btc.qty, reduce_btc.reduce_only) == (
        BTC,
        OrderSide.SELL,
        D("0.0150"),
        True,
    )
    assert (grow_eth.coin, grow_eth.side, grow_eth.qty, grow_eth.reduce_only) == (
        ETH,
        OrderSide.BUY,
        D("0.5000"),
        False,
    )
    filled = engine.cache.order(reduce_btc.order.client_order_id)
    assert filled.is_closed
    assert grow_eth.ts >= filled.ts_last
    assert (_position(engine, BTC), _position(engine, ETH)) == (D("0.01"), D("0.5"))
    weekly = [r for r in rows if r.reason == WEEKLY]
    assert sorted(r.instrument_id for r in weekly) == sorted([BTC, ETH])


def test_a_flip_closes_reduce_only_then_opens_the_other_side_at_the_new_mid():
    cls = _plan("P", first={BTC: 2000}, later=((1, {BTC: -2000}),))
    engine, strategy, _ = _run(cls)

    close, open_ = _after(strategy, 1)
    assert (close.side, close.qty, close.reduce_only) == (OrderSide.SELL, D("0.0250"), True)
    assert (open_.side, open_.qty, open_.reduce_only) == (OrderSide.SELL, D("0.0250"), False)
    assert _position(engine, BTC) == D("-0.025")


def test_a_grow_over_the_order_cap_is_clipped_and_logged(caplog):
    with caplog.at_level(logging.INFO):
        _engine, strategy, rows = _run(_plan("P", first={BTC: 2000}), max_order_notional_usd=D(1000))

    [sent] = strategy.sent
    assert sent.qty == D("0.0125")  # 1000 / 80000 (the mid) floored to the step
    [clipped] = _logs(caplog, "rebalance_clipped")
    assert clipped.fields["sent"] == "0.0125"
    [row] = rows
    assert D(row.inputs["clip"]["sent"]) == D("0.0125")


def test_a_liquidation_distance_refusal_fails_the_coin_at_once(caplog):
    with caplog.at_level(logging.INFO):
        _engine, strategy, rows = _run(
            _plan("P", first={BTC: 45000}), min_liq_distance_pct=D(50)
        )  # 4.5x: liq about 21 % away

    assert strategy.sent == []
    [row] = rows
    assert (row.risk_verdict, row.risk_reason) == (RiskVerdict.REJECTED, "liq_distance")
    assert strategy.failed == {BTC}
    [failed] = _logs(caplog, "rebalance_coin_failed")
    assert failed.levelno == logging.WARNING


def _pause(strategy):
    strategy._gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))


def _unpause(strategy):
    strategy._gate.unpause()


def test_while_paused_reductions_go_grows_are_held_and_the_unpause_sends_them(caplog):
    cls = _plan("P", first={BTC: 2000}, later=((1, _pause), (1.5, {BTC: 800, ETH: 1500}), (2.5, _unpause)))
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls)

    paused = [s for s in _after(strategy, 1.5) if s.ts < cls.t0_ns + int(2.5 * MINUTE_NS)]
    assert [(s.coin, s.reduce_only) for s in paused] == [(BTC, True)]
    [held] = _logs(caplog, "rebalance_held")
    assert (held.fields["instrument"], held.fields["reason"]) == (ETH, "paused")
    assert [r.risk_reason for r in rows if r.instrument_id == ETH and r.reason == WEEKLY] == ["held:paused"]
    resumed = _after(strategy, 2.5)
    assert [(s.coin, s.side, s.reduce_only) for s in resumed] == [(ETH, OrderSide.BUY, False)]
    assert [r.instrument_id for r in rows if r.reason == "resume"] == [ETH]
    assert (_position(engine, BTC), _position(engine, ETH)) == (D("0.01"), D("0.5"))


def test_an_unpause_after_a_held_start_rebalance_sends_the_held_opens(caplog):
    gate = PauseGate()
    gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))
    cls = _plan("P", first={BTC: 2000}, later=((2, _unpause),))
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls, gate=gate)

    assert [r.risk_reason for r in rows if r.reason == "start"] == ["held:paused"]
    [sent] = strategy.sent
    assert sent.ts >= cls.t0_ns + 2 * MINUTE_NS
    assert _position(engine, BTC) == D("0.025")


# Failures and retries (AC-6) ----------------------------------------------------------------


class _Refuses(_Planned):
    """ETH's opening orders never reach the venue: denied at once inside ``submit_order``, or rejected 1 s later."""

    mode = "deny"

    def submit_order(self, order, *args, **kwargs):
        if str(order.instrument_id) != ETH or order.is_reduce_only:
            super().submit_order(order, *args, **kwargs)
            return
        self.sent.append(_Sent(self.clock.timestamp_ns(), ETH, order.side, order.quantity.as_decimal(), False, order))
        event = SimpleNamespace(client_order_id=order.client_order_id, reason="test says no")
        if type(self).mode == "deny":
            self.on_order_denied(event)
            return
        at = self.clock.timestamp_ns() + NS_PER_S
        self.clock.set_time_alert_ns(f"reject-{order.client_order_id}", at, lambda _e: self.on_order_rejected(event))


@pytest.mark.parametrize("mode", ["deny", "reject"])
def test_a_refused_order_retries_three_times_a_minute_apart_then_the_coin_fails(mode, caplog):
    cls = _plan("P", base=_Refuses, first={BTC: 2000, ETH: 1500}, mode=mode)
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls, minutes=6)

    tries = [s.ts for s in strategy.sent if s.coin == ETH]
    assert len(tries) == 4
    gaps = [(b - a) / NS_PER_S for a, b in itertools.pairwise(tries)]
    assert all(60 <= gap <= 62 for gap in gaps)
    assert strategy.failed == {ETH}
    [failed] = _logs(caplog, "rebalance_coin_failed")
    assert failed.fields["instrument"] == ETH
    assert _position(engine, BTC) == D("0.025")  # the other coin trades as normal
    assert len([r for r in rows if r.instrument_id == ETH]) == 1  # retries reuse the decision


def test_a_new_rebalance_cancels_pending_retries():
    cls = _plan("P", base=_Refuses, first={ETH: 1500}, later=((1.5, {ETH: 1500}),), mode="reject")
    _engine, strategy, _ = _run(cls, minutes=6)

    t0 = cls.t0_ns
    minutes = [round((s.ts - t0) / MINUTE_NS, 1) for s in strategy.sent]
    assert minutes[:3] == [0.0, 1.0, 1.5]
    assert 2.0 not in minutes  # the first round's second retry never came
    assert strategy.failed == {ETH}


# Schedule and start (AC-4) -----------------------------------------------------------------


def test_a_start_within_the_cooldown_skips_the_rebalance(caplog):
    with caplog.at_level(logging.INFO):
        _, strategy, rows = _run(_plan("P", first={BTC: 2000}), last_start_ns=_ns(START) - 10 * MINUTE_NS)

    assert strategy.sent == []
    assert rows == []
    assert _logs(caplog, "start_rebalance_cooldown")


def test_a_coin_whose_close_is_missing_keeps_its_position(caplog):
    cls = _plan("P", first={BTC: 2000, ETH: 1500}, stale=frozenset({BTC}))
    with caplog.at_level(logging.INFO):
        _engine, strategy, _ = _run(cls)

    assert [s.coin for s in strategy.sent] == [ETH]
    [missing] = _logs(caplog, "close_missing")
    assert missing.fields["instrument"] == BTC
    assert missing.levelno == logging.WARNING


class _Twice(_Planned):
    """The daily timer fires twice on the same date."""

    def _on_daily(self, event):
        super()._on_daily(event)
        super()._on_daily(event)


MONDAY = datetime(2026, 10, 5, 0, 4, tzinfo=UTC)
SUNDAY = datetime(2026, 10, 4, 0, 4, tzinfo=UTC)


def test_a_monday_run_rebalances_once_and_a_second_fire_that_day_does_nothing():
    cls = _plan("P", base=_Twice, first={BTC: 2000})
    _, strategy, rows = _run(cls, start=MONDAY, last_start_ns=_ns(MONDAY))

    [sent] = strategy.sent
    assert sent.ts >= _ns(MONDAY) + MINUTE_NS  # 00:05
    assert [(r.instrument_id, r.reason) for r in rows] == [(BTC, WEEKLY)]
    assert strategy.plan_day == date(2026, 10, 4)


def test_a_sunday_run_plans_and_sends_nothing():
    _, strategy, rows = _run(_plan("P", first={BTC: 2000}), start=SUNDAY, last_start_ns=_ns(SUNDAY))

    assert strategy.sent == []
    assert rows == []
    assert strategy.plan is not None
    assert strategy.plan_day == date(2026, 10, 3)


# Flatten and adopt (AC-3, AC-9) ------------------------------------------------------------


class _FlattenOnFlip(_Planned):
    """A stop lands the moment the flip's close fills, before the open leg goes."""

    def on_order_filled(self, event):
        tracked = self._orders.get(event.client_order_id)
        if tracked is not None and tracked[1] == Leg.FLIP_CLOSE:
            self.flatten(HALT_ID)
        super().on_order_filled(event)


def test_a_stop_during_a_flip_cancels_the_open_leg_and_closes_every_coin():
    cls = _plan("P", base=_FlattenOnFlip, first={BTC: 2000, ETH: 1500}, later=((1, {BTC: -2000, ETH: 1500}),))
    engine, strategy, _ = _run(cls)

    later = _after(strategy, 1)
    assert all(s.reduce_only for s in later)
    assert sorted(s.coin for s in later) == sorted([BTC, ETH])
    assert engine.cache.positions_open() == []
    assert not strategy.has_open_positions()


class _RefusesFirstClose(_Planned):
    """The first kill switch close never reaches the venue: denied inside ``submit_order``, or rejected 1 s later."""

    mode = "deny"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refused = False

    def submit_order(self, order, *args, **kwargs):
        if self.refused or not order.is_reduce_only or not any(t.startswith("halt:") for t in order.tags or []):
            super().submit_order(order, *args, **kwargs)
            return
        self.refused = True
        self.sent.append(_Sent(self.clock.timestamp_ns(), str(order.instrument_id), order.side,
                               order.quantity.as_decimal(), True, order))  # fmt: skip
        event = SimpleNamespace(client_order_id=order.client_order_id, reason="test says no")
        if type(self).mode == "deny":
            self.on_order_denied(event)
            return
        at = self.clock.timestamp_ns() + NS_PER_S
        self.clock.set_time_alert_ns(f"reject-{order.client_order_id}", at, lambda _e: self.on_order_rejected(event))


def _flatten(strategy):
    strategy.flatten(HALT_ID)


@pytest.mark.parametrize("mode", ["deny", "reject"])
def test_a_refused_kill_switch_close_is_sent_again_on_the_next_flatten(mode):
    cls = _plan("P", base=_RefusesFirstClose, first={BTC: 2000}, later=((1, _flatten), (1.5, _flatten)), mode=mode)
    engine, strategy, _ = _run(cls)

    closes = [s for s in _after(strategy, 1) if s.reduce_only]
    assert [(s.coin, s.side, s.qty) for s in closes] == [(BTC, OrderSide.SELL, D("0.0250"))] * 2
    assert closes[1].ts >= cls.t0_ns + int(1.5 * MINUTE_NS)
    assert engine.cache.positions_open() == []


def _flatten_then_late_grow(strategy):
    # A grow already in flight fills after the close was sized from the cache, so the close leaves a rest.
    grow = strategy.order_factory.market(INSTRUMENT.id, OrderSide.BUY, INSTRUMENT.make_qty(D("0.01")))
    TrendPortfolio.submit_order(strategy, grow)
    strategy._cancel_entries = lambda: 0  # the grow is past cancelling
    strategy.flatten(HALT_ID)
    del strategy._cancel_entries


def test_a_kill_switch_close_left_short_is_topped_up_on_the_next_flatten():
    cls = _plan("P", first={BTC: 2000}, later=((1, _flatten_then_late_grow), (1.5, _flatten)))
    engine, strategy, _ = _run(cls)

    closes = [s for s in _after(strategy, 1) if s.reduce_only]
    assert [(s.coin, s.qty) for s in closes] == [(BTC, D("0.0250")), (BTC, D("0.0100"))]
    assert engine.cache.positions_open() == []


def test_resume_position_adopts_the_position_with_nothing_sent(caplog):
    engine, strategy, _ = _run(_plan("P", first={BTC: 2000}))
    [position] = engine.cache.positions_open()
    before = len(strategy.sent)

    with caplog.at_level(logging.INFO):
        strategy.resume_position(position, None)

    [adopted] = _logs(caplog, "portfolio_position_adopted")
    assert adopted.fields == {"code": "portfolio_position_adopted", "instrument": BTC, "quantity": "0.0250"}
    assert len(strategy.sent) == before


# Review 2026-11-13 minors ------------------------------------------------------------------


def test_a_start_within_the_cooldown_rebalances_once_the_cooldown_has_passed(caplog):
    cls = _plan("P", first={BTC: 2000})
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls, last_start_ns=_ns(START) - 58 * MINUTE_NS)  # due at minute 2

    [sent] = strategy.sent
    assert sent.ts >= cls.t0_ns + 2 * MINUTE_NS
    assert [(r.instrument_id, r.reason) for r in rows] == [(BTC, "start")]
    assert _position(engine, BTC) == D("0.025")
    [waits] = _logs(caplog, "start_rebalance_cooldown")
    assert waits.levelno == logging.WARNING


def test_a_start_after_the_cooldown_under_a_pause_is_left_to_the_resume_rebalance():
    gate = PauseGate()
    gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))
    cls = _plan("P", first={BTC: 2000}, later=((3, _unpause),))
    engine, strategy, rows = _run(cls, gate=gate, last_start_ns=_ns(START) - 58 * MINUTE_NS)

    [sent] = strategy.sent
    assert sent.ts >= cls.t0_ns + 3 * MINUTE_NS
    assert [(r.instrument_id, r.reason) for r in rows] == [(BTC, "resume")]
    assert _position(engine, BTC) == D("0.025")


class _Crashes(_Planned):
    """ETH raises inside its step (``where`` ``step``) or inside its grow (``where`` ``grow``)."""

    where = "step"

    def _step(self, coin, *, first):
        if type(self).where == "step" and str(coin) == ETH:
            raise LookupError("instrument gone")
        super()._step(coin, first=first)

    def _send_grow(self, key):
        if type(self).where == "grow" and key == ETH:
            raise LookupError("instrument gone")
        super()._send_grow(key)


@pytest.mark.parametrize("where", ["step", "grow"])
def test_a_coin_that_raises_fails_alone_and_the_rest_of_the_rebalance_goes_on(where, caplog):
    # ETH sorts before BTC, so the crash comes first and BTC must still be stepped and grown.
    cls = _plan("P", base=_Crashes, first={BTC: 2000, ETH: 1500}, where=where)
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _run(cls)

    assert [s.coin for s in strategy.sent] == [BTC]
    assert _position(engine, BTC) == D("0.025")
    assert strategy.failed == {ETH}
    [crash] = _logs(caplog, "portfolio_run_failed")
    assert (crash.fields["instrument"], crash.levelno) == (ETH, logging.ERROR)
    assert crash.exc_info is not None
    [failed] = _logs(caplog, "rebalance_coin_failed")
    assert failed.fields["why"] == "error"


def _block(strategy):
    strategy._blocks.add(BlockReason.RECORDING_UNHEALTHY)


def _unblock(strategy):
    strategy._blocks.clear(BlockReason.RECORDING_UNHEALTHY)


def test_a_grow_held_by_an_entry_block_is_sent_once_the_block_clears(caplog):
    cls = _plan("P", first={}, later=((1, _block), (1.5, {BTC: 2000}), (2.5, _unblock)))
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls)

    [sent] = strategy.sent
    assert sent.ts >= cls.t0_ns + int(2.5 * MINUTE_NS)
    assert [r.risk_reason for r in rows if r.reason == WEEKLY] == ["held:recording_unhealthy"]
    [resume] = [r for r in rows if r.reason == "resume"]
    assert (resume.instrument_id, resume.risk_verdict) == (BTC, RiskVerdict.APPROVED)
    assert sent.order.tags == [f"{DECISION_TAG}{resume.id}"]
    assert _position(engine, BTC) == D("0.025")


def test_a_block_that_held_nothing_does_not_rebalance_when_it_clears():
    cls = _plan("P", first={BTC: 2000}, later=((1, _block), (1.5, _unblock)))
    _engine, strategy, rows = _run(cls)

    assert len(strategy.sent) == 1
    assert [r.reason for r in rows] == ["start"]


def test_a_flip_writes_one_row_for_the_close_and_one_for_the_open():
    cls = _plan("P", first={BTC: 2000}, later=((1, {BTC: -2000}),))
    _engine, strategy, rows = _run(cls)

    close, open_ = _after(strategy, 1)
    close_row, open_row = [r for r in rows if r.reason == WEEKLY]
    assert (close_row.intent, open_row.intent) == (Intent.ADJUST, Intent.ENTER)
    assert close_row.id != open_row.id
    assert close.order.tags == [f"{DECISION_TAG}{close_row.id}"]
    assert open_.order.tags == [f"{DECISION_TAG}{open_row.id}"]
    assert D(open_row.inputs["current_qty"]) == 0


class _NoEquityOnce(_Planned):
    """The account cannot be read the first time a grow asks for the equity."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked = 0

    def _equity(self):
        self.asked += 1
        return None if self.asked == 1 else super()._equity()


def test_a_retry_with_a_new_verdict_gets_its_own_row_and_its_fill_links_to_it():
    _engine, strategy, rows = _run(_plan("P", base=_NoEquityOnce, first={BTC: 2000}))

    refused, approved = rows
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "risk_input_invalid")
    assert (approved.risk_verdict, approved.risk_reason) == (RiskVerdict.APPROVED, None)
    assert refused.id != approved.id
    [sent] = strategy.sent
    assert sent.order.tags == [f"{DECISION_TAG}{approved.id}"]
