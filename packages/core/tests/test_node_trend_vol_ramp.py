"""covers: spec 0034 AC-3 (the working plan, daily only growth, holds, flips, cuts, retries), AC-4 (the hourly
stop check, the stop close and its retry), AC-5 (the stop block and the daily clear), AC-6 (the restart rebuild
of stops from the seed closes), AC-7 (the plan snapshot fields).

Runs ``TrendVolRamp`` in a BacktestEngine with the two coin harness of ``test_node_trend_portfolio`` (BTC mid
80000, ETH mid 3000), with scripted targets; one quote and trade a minute keeps multi day runs fast. Needs the
``node`` extra.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderSide
from nautilus_trader.model.identifiers import TradeId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_bar_rule import INSTRUMENT
from test_node_six_vote_trend import COIN, COIN_ID
from test_node_trend_daily import MONDAY
from test_node_trend_portfolio import BTC, COINS, ETH, PRICES, _fields, _logs, _ns, _pause, _Planned, _Sent

from trading_core.node import trend_vol, trend_vol_ramp
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_vol import TrendVol
from trading_core.node.trend_vol_ramp import TrendVolRamp, TrendVolRampConfig
from trading_core.recording.enums import Intent
from trading_core.recording.enums import OrderSide as RecordedSide
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose

D = Decimal
NS_PER_S = 1_000_000_000
DAY_NS = 86_400 * NS_PER_S
DAY0 = date(2026, 10, 4)  # the plan day of Monday's 00:05 run
FULL = {BTC: 3000, ETH: 1500}


class _Ramp(_Planned, TrendVolRamp):
    """``TrendVolRamp`` with scripted targets: ``FULL`` unless ``by_day`` names that plan day's offset."""

    first: ClassVar[dict[str, int]] = FULL
    by_day: ClassVar[dict[int, dict[str, int]]] = {}

    def _make_plan(self, day):
        offset = (day - DAY0).days
        if offset in type(self).by_day:
            self.targets = dict(type(self).by_day[offset])
        _Planned._make_plan(self, day)
        self.full_plan = self.plan
        return True


def _ramp(name, **attrs):
    return type(name, (_Ramp,), attrs)


def _ticks(coin, minutes, price):
    data = []
    iid = INSTRUMENT.id if coin == BTC else COIN_ID
    q = Quantity.from_str("5.0000")
    for step in range(minutes):
        ts = _ns(MONDAY) + step * 60 * NS_PER_S
        px = price(coin, ts)
        data.append(TradeTick(iid, Price(px, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER,
                              TradeId(f"{iid.symbol}-{step}"), ts, ts))  # fmt: skip
        data.append(QuoteTick(iid, Price(px - 1, 0), Price(px + 1, 0), q, q, ts, ts))
    return data


def _steady(coin, ts):
    return PRICES[coin]


def _run(cls, minutes, price=_steady, seeds=None, stops=None, **config):
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
        engine.add_data(_ticks(coin, minutes, price))
    gate = PauseGate()
    gate.unpause()
    records: list[WriteItem] = []
    cls.t0_ns = _ns(MONDAY)
    cfg = TrendVolRampConfig(**_fields(order_id_tag=trend_vol_ramp.ORDER_ID_TAG), **config)
    strategy = cls(cfg, gate, EntryBlocks(), records.append, seeds, None, stops)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def _on_day(strategy, day, coin=None):
    # Orders sent during day ``day`` (0 is Monday) from its 00:05 run on.
    start = _ns(MONDAY) + day * DAY_NS
    return [s for s in strategy.sent if start <= s.ts < start + DAY_NS and (coin is None or s.coin == coin)]


def _held(engine, coin):
    return sum((p.signed_decimal_qty() for p in engine.cache.positions_open() if str(p.instrument_id) == coin), D(0))


def test_the_module_names_and_inherits_trend_vol():
    assert trend_vol_ramp.STRATEGY_NAME == "trend_vol_ramp"
    assert trend_vol_ramp.ORDER_ID_TAG == "TVR"
    assert issubclass(TrendVolRamp, TrendVol)
    assert TrendVolRamp.uses_stop is False


@pytest.mark.parametrize("bad", [{"ramp_steps": 0}, {"stop_vols": D(0)}])
def test_a_bad_ramp_setting_is_refused(bad):
    with pytest.raises(ValueError, match="must be"):
        TrendVolRampConfig(**_fields(order_id_tag="TVR"), **bad)


# The ramp (AC-3) ----------------------------------------------------------------------------


def test_a_flat_book_builds_a_third_per_daily_run_and_the_start_round_sends_nothing(caplog):
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _run(_Ramp, minutes=2 * 24 * 60 + 4)

    before_daily = [s for s in strategy.sent if s.ts < _ns(MONDAY) + 60 * NS_PER_S]
    assert before_daily == []  # the start round only held the flat coins
    assert {r.fields["instrument"] for r in _logs(caplog, "ramp_hold_grow")} == {BTC, ETH}
    assert [s.qty for s in strategy.sent if s.coin == BTC] == [D("0.0125")] * 3
    # Each third is sized on what is held, rounded down, so ETH ends one size step short of 0.5 (inside the band).
    assert [s.qty for s in strategy.sent if s.coin == ETH] == [D("0.1666")] * 3
    assert all(not s.reduce_only for s in strategy.sent)
    assert (_held(engine, BTC), _held(engine, ETH)) == (D("0.0375"), D("0.4998"))
    daily = [r for r in _logs(caplog, "ramp_target") if r.fields["reason"] == "daily"]
    assert [D(r.fields["working"]) for r in daily if r.fields["instrument"] == BTC] == [1000, 2000, 3000]
    assert all(r.fields["may_grow"] for r in daily)


def test_a_flip_closes_at_once_and_opens_a_third_on_the_new_side_the_same_day():
    cls = _ramp("Flip", by_day={1: {BTC: -3000, ETH: 1500}})

    engine, strategy, _ = _run(cls, minutes=24 * 60 + 4)

    flip = _on_day(strategy, 1, BTC)
    assert [(s.side, s.qty, s.reduce_only) for s in flip] == [
        (OrderSide.SELL, D("0.0125"), True),
        (OrderSide.SELL, D("0.0125"), False),
    ]
    assert _held(engine, BTC) == D("-0.0125")


def test_a_round_that_is_not_daily_cuts_at_once_and_holds_a_coin_below_its_target(caplog):
    # At 01:04 a weekly round (not daily): BTC's target falls under what it holds, ETH's rises above it.
    cls = _ramp("Cut", later=((60, {BTC: 500, ETH: 3000}),))
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _run(cls, minutes=70)

    after = [s for s in strategy.sent if s.ts >= _ns(MONDAY) + 60 * 60 * NS_PER_S]
    assert [(s.coin, s.side, s.reduce_only) for s in after] == [(BTC, OrderSide.SELL, True)]
    assert _held(engine, BTC) == D("0.0062")
    assert _held(engine, ETH) == D("0.1666")
    eth_holds = [r for r in _logs(caplog, "ramp_hold_grow") if r.fields["instrument"] == ETH]
    assert len(eth_holds) == 2  # the start round and the 01:04 round


class _DeniesEthOnce(_Ramp):
    denied = 0

    def submit_order(self, order, *args, **kwargs):
        if str(order.instrument_id) == ETH and not order.is_reduce_only and type(self).denied == 0:
            type(self).denied += 1
            self.sent.append(_Sent(self.clock.timestamp_ns(), ETH, order.side, order.quantity.as_decimal(), False,
                                   order))  # fmt: skip
            self.on_order_denied(SimpleNamespace(client_order_id=order.client_order_id, reason="test says no"))
            return
        super().submit_order(order, *args, **kwargs)


def test_a_grow_refused_then_retried_still_ends_at_one_third():
    cls = type("Retry", (_DeniesEthOnce,), {"denied": 0})

    engine, strategy, _ = _run(cls, minutes=10)

    assert [s.qty for s in strategy.sent if s.coin == ETH] == [D("0.1666"), D("0.1666")]
    assert _held(engine, ETH) == D("0.1666")


# The stop (AC-4, AC-5) ----------------------------------------------------------------------


def _crash_at(hour):
    # BTC drops 16.25 % at Monday ``hour``:30 (past 8 vols of the scripted 2 % vol); ETH stays put.
    def price(coin, ts):
        if coin == BTC and ts >= _ns(MONDAY.replace(hour=hour, minute=30)):
            return 67000
        return PRICES[coin]

    return price


def test_a_long_that_falls_8_vols_is_closed_kept_flat_while_the_signal_holds_and_cleared_by_a_flat_signal(caplog):
    cls = _ramp("Stop", by_day={2: {BTC: 0, ETH: 1500}, 3: FULL})
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls, minutes=3 * 24 * 60 + 4, price=_crash_at(3))

    btc = [s for s in strategy.sent if s.coin == BTC]
    assert [(s.side, s.reduce_only) for s in btc] == [
        (OrderSide.BUY, False),  # Monday's third
        (OrderSide.SELL, True),  # the 04:00 stop
        (OrderSide.BUY, False),  # Thursday's third, after the stop cleared on Wednesday
    ]
    assert btc[1].ts == _ns(MONDAY.replace(hour=4, minute=0))
    assert _on_day(strategy, 1, BTC) == []
    assert _on_day(strategy, 2, BTC) == []
    stop = [r for r in rows if r.reason == "stop_vol"]
    assert len(stop) == 1
    assert (stop[0].intent, stop[0].side, stop[0].instrument_id) == (Intent.EXIT, RecordedSide.SELL, BTC)
    assert D(stop[0].inputs["entry_vol"]) == D("0.02")
    hit = _logs(caplog, "ramp_stop_hit")
    assert [r.fields["instrument"] for r in hit] == [BTC]
    assert _logs(caplog, "ramp_entry_vol_fallback")
    cleared = _logs(caplog, "ramp_stop_cleared")
    assert [(r.fields["instrument"], r.fields["plan_day"]) for r in cleared] == [(BTC, "2026-10-06")]
    tuesday = [r for r in _logs(caplog, "ramp_target") if r.fields["instrument"] == BTC and D(r.fields["full"]) == 3000]
    assert any(D(r.fields["working"]) == 0 for r in tuesday)
    assert _held(engine, BTC) == D("0.0149")  # a third of 3000 at the new 67000
    assert not _logs(caplog, "ramp_stop_check_failed")


class _DeniesFirstStop(_Ramp):
    denied = 0

    def submit_order(self, order, *args, **kwargs):
        if str(order.instrument_id) == BTC and order.is_reduce_only and type(self).denied == 0:
            type(self).denied += 1
            self.sent.append(_Sent(self.clock.timestamp_ns(), BTC, order.side, order.quantity.as_decimal(), True,
                                   order))  # fmt: skip
            self.on_order_denied(SimpleNamespace(client_order_id=order.client_order_id, reason="test says no"))
            return
        super().submit_order(order, *args, **kwargs)


def test_a_refused_stop_close_is_sent_again_after_the_retry_wait(caplog):
    cls = type("StopRetry", (_DeniesFirstStop,), {"denied": 0})
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _run(cls, minutes=5 * 60, price=_crash_at(3))

    closes = [s for s in strategy.sent if s.coin == BTC and s.reduce_only]
    assert [s.ts - closes[0].ts for s in closes] == [0, 60 * NS_PER_S]
    assert _logs(caplog, "ramp_stop_close_refused")
    assert _held(engine, BTC) == 0


def _grow_btc_now(strategy):
    strategy._send_grow(BTC)
    strategy._step(INSTRUMENT.id, first=False)


def test_a_grow_or_step_after_the_stop_never_sends(caplog):
    cls = _ramp("Race", later=((5 * 60, _grow_btc_now),))  # 05:04, after the 04:00 stop

    _, strategy, _ = _run(cls, minutes=5 * 60 + 10, price=_crash_at(3))

    assert [s.reduce_only for s in strategy.sent if s.coin == BTC] == [False, True]
    assert BTC in strategy.stopped


def test_the_stop_is_checked_while_paused():
    cls = _ramp("Paused", later=((60, _pause),))  # paused at 01:04, the stop fires at 04:00

    _, strategy, _ = _run(cls, minutes=5 * 60, price=_crash_at(3))

    assert strategy._gate.paused
    assert [s.reduce_only for s in strategy.sent if s.coin == BTC] == [False, True]


# The restart (AC-6) -------------------------------------------------------------------------

LAST = date(2026, 10, 3)


def _rising(count=150, last=LAST):
    return [DailyClose(last - timedelta(days=count - 1 - i), D(100 + i)) for i in range(count)]


def _falls_from(day, count=150, last=LAST):
    # Rising until ``day``, then a steep fall: the 30 day signal turns short a few days after ``day``.
    closes, price = [], D(100)
    for i in range(count):
        d = last - timedelta(days=count - 1 - i)
        price = price + 1 if d <= day else price * D("0.9")
        closes.append(DailyClose(d, price))
    return closes


def _bare(seeds, stops):
    cfg = TrendVolRampConfig(**_fields(order_id_tag="TVR"))
    return TrendVolRamp(cfg, PauseGate(), EntryBlocks(), lambda item: None, seeds, None, stops)


def _at(day, hour):
    return _ns(datetime(day.year, day.month, day.day, hour, tzinfo=UTC))


def test_a_stop_with_an_unchanged_signal_since_is_restored(caplog):
    with caplog.at_level(logging.INFO):
        strategy = _bare({BTC: _rising(), ETH: _rising()}, {BTC: (_at(date(2026, 10, 1), 12), 1)})

    assert strategy.stopped == {BTC: (1, LAST)}
    restored = _logs(caplog, "ramp_stop_restored")
    assert [(r.fields["signals"], r.fields["partial"]) for r in restored] == [([1, 1, 1], False)]


def test_a_stop_whose_signal_changed_since_is_dropped(caplog):
    with caplog.at_level(logging.INFO):
        strategy = _bare({BTC: _falls_from(date(2026, 9, 25)), ETH: _rising()}, {BTC: (_at(date(2026, 9, 20), 12), 1)})

    assert strategy.stopped == {}
    assert _logs(caplog, "ramp_stop_dropped")


def test_a_stop_older_than_the_seeds_is_kept(caplog):
    with caplog.at_level(logging.INFO):
        strategy = _bare({BTC: _falls_from(date(2026, 9, 25)), ETH: _rising()}, {BTC: (_at(date(2026, 1, 1), 12), 1)})

    assert BTC in strategy.stopped
    assert [r.fields["partial"] for r in _logs(caplog, "ramp_stop_restored")] == [True]


def test_a_stop_without_seeds_is_kept():
    strategy = _bare({}, {BTC: (_at(date(2026, 10, 1), 12), -1)})

    assert strategy.stopped[BTC][0] == -1


@pytest.mark.parametrize(("hour", "signals"), [(0, 2), (12, 1)])
def test_a_stop_before_the_0005_run_counts_that_run_and_one_after_it_does_not(hour, signals, caplog):
    # On 2026-10-03 at 00:00 the 00:05 run (plan day 10-02) is still to come; at 12:00 it has passed.
    with caplog.at_level(logging.INFO):
        _bare({BTC: _rising(), ETH: _rising()}, {BTC: (_at(LAST, hour), 1)})

    assert len(_logs(caplog, "ramp_stop_restored")[0].fields["signals"]) == signals


def test_a_restored_stop_blocks_the_first_daily_run():
    seeds = {BTC: _rising(), ETH: _rising()}
    cls = _ramp("Restored")

    _, strategy, _ = _run(cls, minutes=10, seeds=seeds, stops={BTC: (_at(date(2026, 10, 2), 12), 1)})

    assert [s for s in strategy.sent if s.coin == BTC] == []
    assert [s.coin for s in strategy.sent] == [ETH]


# The real plan and the snapshot (AC-3, AC-7) --------------------------------------------------


def _seed(start, up, down, days=130):
    closes, price = [], D(start)
    for i in range(days):
        price *= 1 + D(up if i % 2 == 0 else down)
        closes.append(DailyClose(DAY0 - timedelta(days=days - 1 - i), price))
    return closes


def test_the_real_sized_plan_is_the_full_plan_and_each_working_target_is_a_third():
    seeds = {BTC: _seed(60000, "0.05", "-0.03"), ETH: _seed(2000, "0.06", "-0.04")}

    _, strategy, rows = _run(TrendVolRamp, minutes=4, seeds=seeds)

    full, working = strategy.full_plan, strategy.plan
    assert full is not None
    assert working is not None
    for coin in COINS:
        assert working.coins[coin].target_notional == full.coins[coin].target_notional / 3
        assert working.coins[coin].weight == pytest.approx(full.coins[coin].weight / 3)
    assert {r.reason for r in rows} == {"daily"}


def test_a_failed_plan_leaves_the_full_plan_as_it_was(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("too few closes")

    monkeypatch.setattr(trend_vol, "book_vol_plan", fail)
    seeds = {BTC: _seed(60000, "0.05", "-0.03"), ETH: _seed(2000, "0.06", "-0.04")}

    _, strategy, rows = _run(TrendVolRamp, minutes=4, seeds=seeds)

    assert strategy.full_plan is None
    assert rows == []


def test_the_snapshot_shows_the_full_target_how_much_is_held_and_the_stop():
    _, strategy, _ = _run(_Ramp, minutes=6 * 60, price=_crash_at(3))

    data = strategy.plan_snapshot()
    coins = {c["instrument_id"]: c for c in data["coins"]}
    assert (data["ramp_steps"], data["stop_vols"]) == (3, "8")
    assert coins[BTC]["target_notional"] == "3000"
    assert coins[BTC]["stopped_side"] == "long"
    assert coins[BTC]["held_pct"] == "0.00"
    assert coins[ETH]["target_notional"] == "1500"
    assert coins[ETH]["target_qty"] == "0.5000"
    assert coins[ETH]["held_pct"] == "33.32"
    assert coins[ETH]["stopped_side"] is None
    assert data["schedule"] == "daily"
    assert "book_gross" in data


def test_a_check_that_raises_fails_alone(caplog, monkeypatch):
    calls = []

    def boom(self, coin):
        calls.append(str(coin))
        if str(coin) == BTC:
            raise RuntimeError("boom")

    monkeypatch.setattr(TrendVolRamp, "_check_stop", boom)
    with caplog.at_level(logging.INFO):
        _run(_Ramp, minutes=60)

    assert calls == sorted([BTC, ETH])
    assert [r.fields["instrument"] for r in _logs(caplog, "ramp_stop_check_failed")] == [BTC]
