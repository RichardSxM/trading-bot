"""covers: spec 0032 AC-1 (the module's names, inheritance), AC-2 (one ``daily`` rebalance per date, never a
``weekly`` one, a start does not count), AC-2a (the plan snapshot's daily schedule), AC-2b (``close_missing``),
AC-3 (every change trades).

Runs ``TrendDaily`` in a BacktestEngine with the two coin harness of ``test_node_trend_portfolio``: a scripted
subclass replaces the rule with fixed target notionals. Needs the ``node`` extra.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_bar_rule import INSTRUMENT
from test_node_six_vote_trend import COIN
from test_node_trend_portfolio import BTC, COINS, ETH, MINUTE_NS, _fields, _logs, _ns, _Planned, _ticks

from trading_core.node import trend_daily
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_daily import DAILY, TrendDaily, TrendDailyConfig
from trading_core.node.trend_portfolio import START, WEEKLY, TrendPortfolio
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks

D = Decimal
MONDAY = datetime(2026, 10, 5, 0, 4, tzinfo=UTC)


class _DailyPlanned(_Planned, TrendDaily):
    """``TrendDaily`` with the scripted plan of ``test_node_trend_portfolio``."""


class _Growing(_DailyPlanned):
    """BTC's target grows by 1000 USD each plan day, so every daily rebalance has a change to trade."""

    def _make_plan(self, day):
        self.targets = {BTC: 2000 + 1000 * (day - date(2026, 10, 4)).days}
        return super()._make_plan(day)


class _Twice(_DailyPlanned):
    """The daily timer fires twice on the same date."""

    def _on_daily(self, event):
        super()._on_daily(event)
        super()._on_daily(event)


def _plan(name, base=_DailyPlanned, **attrs):
    return type(name, (base,), attrs)


def _run(cls, minutes, start=MONDAY, last_start_ns=None, every_s=60):
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
        engine.add_data(_ticks(coin, start, minutes, every_s=every_s))
    gate = PauseGate()
    gate.unpause()
    records: list[WriteItem] = []
    cls.t0_ns = _ns(start)
    config = TrendDailyConfig(**_fields(order_id_tag=trend_daily.ORDER_ID_TAG))
    strategy = cls(config, gate, EntryBlocks(), records.append, None, last_start_ns)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def test_the_module_names_and_inherits_the_portfolio():
    assert trend_daily.STRATEGY_NAME == "trend_daily"
    assert trend_daily.ORDER_ID_TAG == "TD"
    assert issubclass(TrendDaily, TrendPortfolio)
    assert TrendDaily.uses_stop is False


def test_three_days_rebalance_once_each_with_reason_daily_and_trade_every_change():
    # Monday, Tuesday, Wednesday 00:05: every weekday rebalances, and none of them is ``weekly``.
    _, strategy, rows = _run(_Growing, minutes=2 * 24 * 60 + 3, last_start_ns=_ns(MONDAY))

    assert [r.reason for r in rows] == [DAILY, DAILY, DAILY]
    assert WEEKLY not in {r.reason for r in rows}
    days = [datetime.fromtimestamp(s.ts / 1e9, tz=UTC).date() for s in strategy.sent]
    assert days == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    assert all(s.coin == BTC and s.side == OrderSide.BUY for s in strategy.sent)
    # 2000, then 3000, then 4000 USD at 80000: each day's order is the change since the day before.
    assert [s.qty for s in strategy.sent] == [D("0.025"), D("0.0125"), D("0.0125")]
    assert strategy.last_reason == DAILY


def test_a_second_fire_on_the_same_date_sends_nothing():
    _, strategy, rows = _run(_plan("P", base=_Twice, first={BTC: 2000}), minutes=3, last_start_ns=_ns(MONDAY))

    [sent] = strategy.sent
    assert sent.ts >= _ns(MONDAY) + MINUTE_NS
    assert [r.reason for r in rows] == [DAILY]


def test_a_start_before_the_run_does_not_count_as_the_daily_rebalance():
    # No cooldown: the start rebalance buys at once, then 00:05 still runs its daily rebalance, which plans
    # the same day and so finds nothing left to trade (no decision row, but it did run).
    start = datetime(2026, 10, 5, 0, 3, tzinfo=UTC)
    _, strategy, rows = _run(_Growing, minutes=4, start=start)

    assert [r.reason for r in rows] == [START]
    assert len(strategy.sent) == 1
    assert strategy.last_reason == DAILY
    assert strategy.last_rebalance_ns >= _ns(start) + 2 * MINUTE_NS


def test_the_plan_snapshot_reports_the_daily_schedule_and_the_missing_closes(caplog):
    cls = _plan("P", first={BTC: 2000, ETH: 1500}, stale=frozenset({BTC}))
    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(cls, minutes=3, last_start_ns=_ns(MONDAY))

    data = strategy.plan_snapshot()
    assert data["schedule"] == "daily"
    assert data["run_at_utc"] == "00:05"
    assert "rebalance_weekday" not in data
    assert data["close_missing"] == [BTC]
    assert data["last_reason"] == DAILY
    assert [s.coin for s in strategy.sent] == [ETH]
    assert _logs(caplog, "close_missing")


def test_close_missing_is_empty_when_every_close_is_in():
    _, strategy, _ = _run(_plan("P", first={BTC: 2000}), minutes=3, last_start_ns=_ns(MONDAY))

    assert strategy.plan_snapshot()["close_missing"] == []
    assert strategy.plan_day == date(2026, 10, 4)


def test_the_day_after_is_planned_from_the_day_that_just_ended():
    _, strategy, _ = _run(_Growing, minutes=24 * 60 + 3, last_start_ns=_ns(MONDAY))

    assert strategy.plan_day == date(2026, 10, 5)
    assert strategy.plan_day == (MONDAY + timedelta(days=1)).date() - timedelta(days=1)
