"""covers: spec 0029 AC-4 (the maker then market entry), AC-5 (the trailing stop and its switch), AC-6 (the time
exit), AC-9 (the restart), and the task 3 spike (a ``trigger_price`` modify of a resting stop market works in the
simulated exchange the demo sandbox runs).

Runs ``SixVoteTrend`` in ``test_node_six_vote_trend``'s two instrument harness on 1 minute bars, with a scripted
rule (``Script`` from ``test_node_bar_rule``: a long at bar 5, the structural stop 100 below the close), an ATR
of 2 bars (about 60, as prices move 60 a minute), and no BTC seed. The coin's price paths are the maker base
tests'. A few subclasses misbehave on purpose (a swallowed or refused modify, a paused gate). Needs the
``node`` extra.
"""

import dataclasses
import itertools
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderStatus, OrderType
from nautilus_trader.model.events import OrderFilled, OrderModifyRejected, OrderUpdated
from test_node_bar_rule import START, _at, _ns
from test_node_bar_rule_maker import FILL, FILL_FLAT, RISE, TOUCH
from test_node_six_vote_trend import COIN_ID, BtcScript, _decisions, _logs, _run, _ticks

from trading_core.control.pause import PauseState
from trading_core.node.bar_rule_maker import Phase
from trading_core.node.reconcile import resting_stop
from trading_core.node.six_vote_trend import SixVoteTrend, trail_level, trail_moves
from trading_core.recording.enums import ControlSource, Intent, RiskEventKind
from trading_core.recording.mappers import DECISION_TAG, PARENT_TAG
from trading_core.recording.rows import RiskEventRow
from trading_core.strategy.bar_indicators import Atr
from trading_core.strategy.bar_rule import Side

D = Decimal
HALT_ID = uuid.UUID(int=9)
LONG = {5: (Side.LONG, 100)}
NS_PER_S = 1_000_000_000


class _Scripted(SixVoteTrend):
    """The node with a scripted rule: a long at bar ``entries``."""

    entries = LONG

    def __init__(self, config, gate, blocks, record, seed=(), btc_seed=()):
        super().__init__(config, gate, blocks, record, seed, btc_seed)
        self.rule = self._six_rule = BtcScript(entries=dict(type(self).entries))  # type: ignore[assignment]


def _go(strategy_cls=_Scripted, prices=FILL, minutes=20, **kwargs):
    kwargs.setdefault("atr_period", 2)
    if "coin_data" not in kwargs:
        kwargs["coin_data"] = _ticks(COIN_ID, prices, minutes)
    return _run(strategy_cls, btc_seed=(), minutes=minutes, **kwargs)


def _orders(engine, order_type, reduce_only=None):
    return [
        o
        for o in engine.cache.orders(instrument_id=COIN_ID)
        if o.order_type == order_type and (reduce_only is None or o.is_reduce_only == reduce_only)
    ]


def _entry_limit(engine):
    return next(o for o in _orders(engine, OrderType.LIMIT, reduce_only=False))


def _risk_events(rows):
    return [r for r in rows if isinstance(r, RiskEventRow)]


def _codes(caplog):
    return [f.get("code") for r in caplog.records if (f := getattr(r, "fields", {})).get("code")]


def _updates(order):
    return [e for e in order.events if isinstance(e, OrderUpdated)]


# The market fallback (AC-4) -----------------------------------------------------------------


def test_an_unfilled_limit_falls_back_to_one_market_order_under_the_same_decision(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _go(prices=RISE, trail_atr=None)

    [enter] = _decisions(rows)
    limit = _entry_limit(engine)
    [market] = _orders(engine, OrderType.MARKET, reduce_only=False)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    [position] = engine.cache.positions_open(instrument_id=COIN_ID)
    assert (limit.status, limit.filled_qty.as_decimal()) == (OrderStatus.CANCELED, D(0))
    assert market.status == OrderStatus.FILLED
    assert market.tags == [f"{DECISION_TAG}{enter.id}"]
    assert market.quantity <= limit.quantity  # sized again at the later, higher mid
    [why] = [f["why"] for f in _logs(caplog, "entry_fallback_market")]
    assert why == "timeout"
    assert "maker_entry_timeout" not in _codes(caplog)
    assert stop.quantity == position.quantity == market.quantity
    assert f"{PARENT_TAG}{market.client_order_id}" in stop.tags
    assert stop.trigger_price.as_decimal() == D(str(enter.inputs["stop"]))
    assert strategy._entry_px == D(str(position.avg_px_open))
    assert strategy.phase == Phase.PROTECTED


def test_a_part_filled_limit_sends_the_rest_at_market_and_the_stop_follows(caplog):
    data = _ticks(COIN_ID, TOUCH, 20, quote_size="0.0500")
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _go(coin_data=data, consume=True)

    limit = _entry_limit(engine)
    [market] = _orders(engine, OrderType.MARKET, reduce_only=False)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    [position] = engine.cache.positions_open(instrument_id=COIN_ID)
    assert D(0) < limit.filled_qty.as_decimal() < limit.quantity.as_decimal()
    assert market.quantity.as_decimal() <= limit.quantity.as_decimal() - limit.filled_qty.as_decimal()
    assert [f["why"] for f in _logs(caplog, "entry_fallback_market")] == ["remainder"]
    assert position.quantity.as_decimal() == limit.filled_qty.as_decimal() + market.filled_qty.as_decimal()
    assert stop.quantity == position.quantity
    assert strategy._entry_px == D(str(position.avg_px_open))


def test_a_post_only_rejected_limit_falls_back_to_market_at_once(caplog):
    data = _ticks(COIN_ID, RISE, 20, quote_shift=-6)  # quotes below the trades: the buy limit crosses
    with caplog.at_level(logging.INFO):
        engine, _, rows = _go(coin_data=data)

    limit = _entry_limit(engine)
    [market] = _orders(engine, OrderType.MARKET, reduce_only=False)
    assert limit.status == OrderStatus.REJECTED
    assert [f["why"] for f in _logs(caplog, "entry_fallback_market")] == ["post_only_rejected"]
    assert market.status == OrderStatus.FILLED
    assert _risk_events(rows) == []
    assert engine.cache.positions_open(instrument_id=COIN_ID)


class _StopAbove(_Scripted):
    """The signal's stop is above the mid by the time the wait runs out."""

    def _wait_step(self):
        if self.phase == Phase.ENTERING:
            self._stop_px = D(10**6)
        super()._wait_step()


class _PausedAtWait(_Scripted):
    def _wait_step(self):
        if self.phase == Phase.ENTERING:
            self._gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))
        super()._wait_step()


class _TinyLimit(_Scripted):
    """A notional limit no order can meet by the time the wait runs out."""

    def _wait_step(self):
        if self.phase == Phase.ENTERING:
            self._limits = dataclasses.replace(self._limits, max_order_notional=D(1))
        super()._wait_step()


@pytest.mark.parametrize(
    ("strategy_cls", "why"),
    [(_StopAbove, "stop_through"), (_PausedAtWait, "paused")],
)
def test_a_fallback_check_that_fails_sends_nothing_and_goes_flat(strategy_cls, why, caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _go(strategy_cls, prices=RISE)

    assert _orders(engine, OrderType.MARKET) == []
    assert [f["why"] for f in _logs(caplog, "entry_fallback_skipped")] == [why]
    assert len(_decisions(rows)) == 1  # no new decision row
    assert strategy.phase == Phase.FLAT


def test_a_risk_refusal_at_the_fallback_sends_nothing_and_raises_the_risk_event(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _go(_TinyLimit, prices=RISE)

    assert _orders(engine, OrderType.MARKET) == []
    [skipped] = _logs(caplog, "entry_fallback_skipped")
    [event] = _risk_events(rows)
    assert event.kind == RiskEventKind.LIMIT_HIT
    assert event.details["reason"] == skipped["why"]
    assert event.decision_id == _decisions(rows)[0].id
    assert strategy.phase == Phase.FLAT


def test_a_stale_quote_at_the_wait_sends_nothing(caplog):
    data = _ticks(COIN_ID, RISE, 20, quotes_until=4.9)
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _go(coin_data=data, stale_quote_s=D(30), entry_wait_bars=2)

    assert _orders(engine, OrderType.MARKET) == []
    [why] = [f["why"] for f in _logs(caplog, "entry_fallback_skipped")]
    assert why == "stale_quote" or why.startswith("blocked:")
    assert strategy.phase == Phase.FLAT


class _FlattenAt5(_Scripted):
    """The kill switch right after bar 5's entry: its cancel of the limit sends no fallback."""

    def on_bar(self, bar):
        super().on_bar(bar)
        if bar.bar_type == self.bar_type and getattr(self.rule, "n", 0) == 5:
            self.flatten(HALT_ID)


def test_a_kill_switch_cancel_of_the_limit_sends_no_fallback(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _go(_FlattenAt5, prices=RISE)

    assert _entry_limit(engine).status == OrderStatus.CANCELED
    assert _orders(engine, OrderType.MARKET) == []
    assert "entry_fallback_market" not in _codes(caplog)
    assert "entry_fallback_skipped" not in _codes(caplog)
    assert strategy.phase == Phase.FLAT


class _RejectsFallback(_Scripted):
    """The venue refuses the fallback market."""

    def submit_order(self, order, *args, **kwargs):
        if order.order_type == OrderType.MARKET and not order.is_reduce_only:
            self._refused(order.client_order_id, "rejected: venue says no", post_only=False)
            return
        super().submit_order(order, *args, **kwargs)


def test_a_rejected_fallback_raises_the_order_rejected_event_and_goes_flat():
    engine, strategy, rows = _go(_RejectsFallback, prices=RISE)

    [event] = _risk_events(rows)
    assert event.kind == RiskEventKind.ORDER_REJECTED
    assert engine.cache.positions_open(instrument_id=COIN_ID) == []
    assert strategy.phase == Phase.FLAT


class _LosesFallback(_Scripted):
    """The venue cancels the fallback market before it fills."""

    def submit_order(self, order, *args, **kwargs):
        if order.order_type == OrderType.MARKET and not order.is_reduce_only:
            self._canceled(order.client_order_id, "canceled")
            return
        super().submit_order(order, *args, **kwargs)


def test_a_lost_fallback_market_warns_and_goes_flat(caplog):
    # covers: AC-4 (fallback failures: a cancel of the fallback market)
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _go(_LosesFallback, prices=RISE)

    [lost] = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "entry_fallback_lost"]
    assert lost.levelno == logging.WARNING
    assert engine.cache.positions_open(instrument_id=COIN_ID) == []
    assert _orders(engine, OrderType.STOP_MARKET) == []
    assert len(_decisions(rows)) == 1
    assert strategy.phase == Phase.FLAT


def test_a_filled_limit_needs_no_fallback(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, _ = _go(prices=FILL)

    assert _entry_limit(engine).status == OrderStatus.FILLED
    assert _orders(engine, OrderType.MARKET, reduce_only=False) == []
    assert "entry_fallback_market" not in _codes(caplog)


# The trailing stop (AC-5) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("best", "current", "moves", "level"),
    [
        (D(101), D(94), True, D("95.0")),  # entry 100, ATR 2: 1 above the stop, step 0.2 met
        (D("101.1"), D(95), False, D("95.1")),  # 0.1 is below the 0.2 step
        (D(103), D(95), True, D("97.0")),
        (D(99), D(95), False, D("93.0")),  # looser: never
    ],
)
def test_the_trail_level_and_its_step(best, current, moves, level):
    assert trail_level(Side.LONG, best, D(2), D(3), D("0.1")) == level
    assert trail_moves(Side.LONG, level, current, D("0.2")) is moves


def test_a_short_s_trail_rounds_up_and_only_moves_down():
    assert trail_level(Side.SHORT, D("99.95"), D(2), D(3), D("0.1")) == D("106.0")
    assert trail_moves(Side.SHORT, D(105), D(106), D("0.2"))
    assert not trail_moves(Side.SHORT, D(107), D(106), D("0.2"))


def test_a_rising_market_trails_the_stop_up_by_steps_at_least_five_seconds_apart(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _go(prices=FILL)

    [stop] = _orders(engine, OrderType.STOP_MARKET)
    moves = [e for e in _updates(stop) if e.trigger_price is not None]
    triggers = [e.trigger_price.as_decimal() for e in moves]
    assert len(triggers) >= 3
    assert triggers == sorted(set(triggers))  # only ever up
    stamps = [e.ts_event for e in moves]
    assert all(b - a >= 5 * NS_PER_S for a, b in itertools.pairwise(stamps))
    assert strategy._stop_px == stop.trigger_price.as_decimal() == triggers[-1]
    logged = _logs(caplog, "stop_trailed")
    assert [D(f["new"]) for f in logged] == triggers
    assert stop.status == OrderStatus.ACCEPTED


def test_with_the_trail_off_the_stop_never_moves(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, rows = _go(prices=FILL, trail_atr=None)

    [stop] = _orders(engine, OrderType.STOP_MARKET)
    enter = _decisions(rows)[0]
    assert stop.trigger_price.as_decimal() == D(str(enter.inputs["stop"]))
    assert "stop_trailed" not in _codes(caplog)


class _SwallowsModify(_Scripted):
    """A trail modify that never gets an answer."""

    def modify_order(self, order, quantity=None, price=None, trigger_price=None, *args, **kwargs):
        if trigger_price is not None:
            self.calls = getattr(self, "calls", 0) + 1
            return
        super().modify_order(order, quantity, price, trigger_price, *args, **kwargs)


def test_a_pending_modify_blocks_the_next():
    _, strategy, _ = _go(_SwallowsModify, prices=FILL)
    assert strategy.calls == 1


class _RefusesModify(_Scripted):
    """The venue refuses every trail modify."""

    def modify_order(self, order, quantity=None, price=None, trigger_price=None, *args, **kwargs):
        if trigger_price is None:
            super().modify_order(order, quantity, price, trigger_price, *args, **kwargs)
            return
        self.calls = getattr(self, "calls", 0) + 1
        _reject_modify(self, order)


def _reject_modify(strategy, order):
    now = strategy.clock.timestamp_ns()
    strategy.on_order_modify_rejected(
        OrderModifyRejected(
            strategy.trader_id,
            strategy.id,
            order.instrument_id,
            order.client_order_id,
            order.venue_order_id,
            order.account_id,
            "no",
            UUID4(),
            now,
            now,
        )
    )


def test_five_refusals_in_a_row_disable_the_trail_and_the_stop_stays(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _go(_RefusesModify, prices=FILL)

    assert strategy.calls == 5
    assert _codes(caplog).count("stop_trail_rejected") == 5
    assert _codes(caplog).count("stop_trail_disabled") == 1
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    assert stop.trigger_price.as_decimal() == D(str(_decisions(rows)[0].inputs["stop"]))
    assert stop.status == OrderStatus.ACCEPTED


class _RefusesEveryOther(_Scripted):
    """The venue refuses every odd trail modify and takes every even one."""

    def modify_order(self, order, quantity=None, price=None, trigger_price=None, *args, **kwargs):
        if trigger_price is not None:
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls % 2:
                _reject_modify(self, order)
                return
        super().modify_order(order, quantity, price, trigger_price, *args, **kwargs)


def test_a_taken_modify_resets_the_refusal_count(caplog):
    # covers: AC-5 (the trail is disabled only after trail_max_refusals refusals in a row)
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _go(_RefusesEveryOther, prices=FILL, trail_max_refusals=2)

    assert strategy.calls >= 3  # refused, taken, refused: two refusals, never two in a row
    assert _codes(caplog).count("stop_trail_rejected") >= 2
    assert "stop_trail_disabled" not in _codes(caplog)
    assert "stop_trailed" in _codes(caplog)
    [stop] = _orders(engine, OrderType.STOP_MARKET)
    assert strategy._stop_px == stop.trigger_price.as_decimal()


# The time exit (AC-6) -----------------------------------------------------------------------


def test_the_time_stop_rests_a_maker_exit_for_one_bar_then_goes_to_market():
    engine, strategy, rows = _go(prices=FILL_FLAT, minutes=25, time_stop_bars=3)

    exit_ = _decisions(rows)[-1]
    [limit] = _orders(engine, OrderType.LIMIT, reduce_only=True)
    [market] = _orders(engine, OrderType.MARKET, reduce_only=True)
    assert (exit_.intent, exit_.reason, exit_.inputs["exit_mode"]) == (Intent.EXIT, "time_stop", "maker")
    assert limit.status == OrderStatus.CANCELED
    filled = [e.ts_event for e in market.events if isinstance(e, OrderFilled)]
    assert exit_.decided_ns < filled[0] <= exit_.decided_ns + 2 * 60 * NS_PER_S  # one bar later
    assert engine.cache.positions_open(instrument_id=COIN_ID) == []
    assert strategy.phase == Phase.FLAT


# The restart (AC-9) -------------------------------------------------------------------------


def _min(minutes):
    return _ns(START + timedelta(minutes=minutes))


class _Restart(_Scripted):
    """Loses its trade state and its ATR at 7.5 minutes and is handed the open position at 7.6."""

    def on_start(self):
        super().on_start()
        self.clock.set_time_alert_ns("test_forget", _min(7.5), self._forget)
        self.clock.set_time_alert_ns("test_resume", _min(7.6), self._resume)

    def _forget(self, _event):
        self._reset()
        self._atr = Atr(self._cfg.atr_period)

    def _resume(self, _event):
        (position,) = self.cache.positions_open(instrument_id=COIN_ID)
        self.resume_position(position, resting_stop(self.cache, position))


def test_a_resumed_position_trails_only_once_the_atr_is_back_and_only_tighter():
    engine, strategy, _ = _go(_Restart, prices=FILL)

    [stop] = _orders(engine, OrderType.STOP_MARKET)
    moves = [e for e in _updates(stop) if e.trigger_price is not None]
    after = [e for e in moves if e.ts_event > _min(7.6)]
    # No move between the resume and the fresh 2 bar ATR's first value, at the third bar close after it (bar 11).
    assert all(e.ts_event >= _at(11) for e in after)
    assert after
    triggers = [e.trigger_price.as_decimal() for e in moves]
    assert triggers == sorted(set(triggers))
    assert strategy._stop_px == stop.trigger_price.as_decimal()


class _LateRestart(_Restart):
    """Loses its trade state at 6.5 minutes and is handed the open position only at 9.6, past a 3 bar time stop."""

    def on_start(self):
        SixVoteTrend.on_start(self)
        self.clock.set_time_alert_ns("test_forget", _min(6.5), self._forget)
        self.clock.set_time_alert_ns("test_resume", _min(9.6), self._resume)


def test_a_position_resumed_past_the_time_stop_exits_at_once_at_market():
    # covers: AC-9 (past time_stop_bars at resume: a market exit as time_stop with resumed true)
    engine, strategy, rows = _go(_LateRestart, prices=FILL_FLAT, time_stop_bars=3)

    exits = [d for d in _decisions(rows) if d.intent == Intent.EXIT]
    [exit_] = exits
    assert (exit_.reason, exit_.inputs["resumed"]) == ("time_stop", True)
    assert exit_.decided_ns == _min(9.6)
    [market] = _orders(engine, OrderType.MARKET, reduce_only=True)
    assert market.status == OrderStatus.FILLED
    assert engine.cache.positions_open(instrument_id=COIN_ID) == []
    assert strategy.phase == Phase.FLAT
