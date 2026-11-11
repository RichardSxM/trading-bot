"""covers: ``TrendVol``: the names and inheritance, the no trade band in a rebalance, the plan snapshot's
book fields, and the config checks.

Runs in a BacktestEngine with the two coin harness of ``test_node_trend_portfolio`` (scripted targets), so
the book volatility sizing itself is covered by ``test_strategy_trend_vol``. Needs the ``node`` extra.
"""

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_bar_rule import INSTRUMENT
from test_node_six_vote_trend import COIN
from test_node_trend_daily import MONDAY
from test_node_trend_portfolio import BTC, COINS, ETH, _fields, _logs, _ns, _Planned, _ticks

from trading_core.node import trend_vol
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_daily import TrendDaily
from trading_core.node.trend_vol import TrendVol, TrendVolConfig
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose

D = Decimal


class _VolPlanned(_Planned, TrendVol):
    """``TrendVol`` with scripted targets: day one opens BTC 2000 and ETH 1500, later days use ``by_day``."""

    by_day: ClassVar[dict[int, dict[str, int]]] = {}

    def _make_plan(self, day):
        offset = (day - date(2026, 10, 4)).days
        self.targets = dict(type(self).by_day.get(offset, {BTC: 2000, ETH: 1500}))
        return super()._make_plan(day)


def _run(cls, minutes, seeds=None, **config):
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
        engine.add_data(_ticks(coin, MONDAY, minutes))
    gate = PauseGate()
    gate.unpause()
    records: list[WriteItem] = []
    cls.t0_ns = _ns(MONDAY)
    cfg = TrendVolConfig(**_fields(order_id_tag=trend_vol.ORDER_ID_TAG), **config)
    strategy = cls(cfg, gate, EntryBlocks(), records.append, seeds, _ns(MONDAY))
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def test_the_module_names_and_inherits_the_daily_book():
    assert trend_vol.STRATEGY_NAME == "trend_vol"
    assert trend_vol.ORDER_ID_TAG == "TV"
    assert issubclass(TrendVol, TrendDaily)
    assert TrendVol.uses_stop is False


def test_a_change_inside_the_band_is_left_alone_and_one_outside_it_trades(caplog):
    # Day two: BTC's target moves 2000 -> 2400 (20 %, inside the 30 % band), ETH's 1500 -> 3000 (outside).
    cls = type("P", (_VolPlanned,), {"by_day": {1: {BTC: 2400, ETH: 3000}}})
    with caplog.at_level(logging.INFO):
        strategy, _ = _run(cls, minutes=24 * 60 + 3)

    day_two = [s for s in strategy.sent if s.ts >= _ns(MONDAY) + 24 * 3600 * 10**9]
    assert [s.coin for s in day_two] == [ETH]
    skipped = [r for r in _logs(caplog, "rebalance_skip_band")]
    assert [r.fields["instrument"] for r in skipped] == [BTC]


def test_with_the_band_off_every_change_trades():
    cls = type("P", (_VolPlanned,), {"by_day": {1: {BTC: 2400, ETH: 3000}}})

    strategy, _ = _run(cls, minutes=24 * 60 + 3, rebalance_band=D(0))

    day_two = [s for s in strategy.sent if s.ts >= _ns(MONDAY) + 24 * 3600 * 10**9]
    assert sorted(s.coin for s in day_two) == sorted([BTC, ETH])


def test_a_flip_and_a_close_always_trade():
    cls = type("P", (_VolPlanned,), {"by_day": {1: {BTC: -2000, ETH: 0}}})

    strategy, _ = _run(cls, minutes=24 * 60 + 3)

    day_two = {s.coin for s in strategy.sent if s.ts >= _ns(MONDAY) + 24 * 3600 * 10**9}
    assert day_two == {BTC, ETH}


def test_the_plan_snapshot_carries_the_book_settings():
    strategy, _ = _run(_VolPlanned, minutes=3)

    data = strategy.plan_snapshot()

    assert data["schedule"] == "daily"
    assert (data["book_vol_target"], data["rebalance_band"]) == ("0.5", "0.3")
    assert "unit_vol" in data
    assert "book_gross" in data


@pytest.mark.parametrize("bad", [{"book_vol_target": D(0)}, {"rebalance_band": D(1)}])
def test_a_bad_book_setting_is_refused(bad):
    with pytest.raises(ValueError, match="must be"):
        TrendVolConfig(**_fields(order_id_tag=trend_vol.ORDER_ID_TAG), **bad)


def _seed(start, up, down, days=130):
    # ``days`` closes ending Sunday 2026-10-04, the day the Monday 00:05 run plans from.
    closes, price = [], D(start)
    for i in range(days):
        price *= 1 + D(up if i % 2 == 0 else down)
        closes.append(DailyClose(date(2026, 10, 4) - timedelta(days=days - 1 - i), price))
    return closes


def test_the_real_plan_is_sized_by_book_volatility_not_the_gross_cap(caplog):
    # Both coins swing about 4 % a day and trend up, so a 50 % annual target allows well under the 5x cap.
    seeds = {BTC: _seed(60000, "0.05", "-0.03"), ETH: _seed(2000, "0.06", "-0.04")}
    with caplog.at_level(logging.INFO):
        strategy, rows = _run(TrendVol, minutes=3, seeds=seeds)

    plan = strategy.plan
    assert plan is not None
    gross = sum(abs(c.weight) for c in plan.coins.values())
    assert strategy.book_gross is not None
    assert gross == pytest.approx(strategy.book_gross)
    assert D("0.3") < gross < D(2)
    assert all(c.signal == 1 for c in plan.coins.values())
    sized = _logs(caplog, "book_vol_sized")[-1]
    assert sized.fields["gross"] == str(strategy.book_gross)
    assert {r.reason for r in rows} == {"daily"}
    assert strategy.plan_snapshot()["book_gross"] == str(strategy.book_gross)
