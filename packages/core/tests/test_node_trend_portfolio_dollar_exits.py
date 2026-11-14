"""covers: the trend portfolio's dollar exits per coin and the re-entry that follows (the owner's demo choice).

Runs ``TrendPortfolio`` in a BacktestEngine with the two coins of ``test_node_trend_portfolio``, where BTC jumps
at a set minute. Needs the ``node`` extra.
"""

import asyncio
from datetime import timedelta
from decimal import ROUND_FLOOR
from decimal import Decimal as D

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
from test_node_trend_portfolio import (
    BTC,
    COINS,
    ETH,
    MINUTE_NS,
    PRICES,
    START,
    _fields,
    _ns,
    _pause,
    _plan,
    _Planned,
    _position,
)

from trading_core.node.strategy import PauseGate
from trading_core.node.trend_portfolio import TrendPortfolioConfig
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks

DOLLARS = {"coin_take_profit_usd": D(3), "coin_stop_loss_usd": D(2)}
JUMP_MINUTE = 2


def _ticks(coin, jump, minutes):
    data, at = [], START
    for step in range(minutes * 6):
        px = PRICES[coin] + (jump if coin == BTC and step / 6 >= JUMP_MINUTE else 0)
        ts, iid, q = _ns(at), INSTRUMENT.id if coin == BTC else COIN_ID, Quantity.from_str("5.0000")
        data.append(TradeTick(iid, Price(px, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER,
                              TradeId(f"{iid.symbol}-{step}"), ts, ts))  # fmt: skip
        data.append(QuoteTick(iid, Price(px - 1, 0), Price(px + 1, 0), q, q, ts, ts))
        at += timedelta(seconds=10)
    return data


def _run(cls, jump, minutes=5, gate=None, **overrides):
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
        engine.add_data(_ticks(coin, jump, minutes))
    if gate is None:
        gate = PauseGate()
        gate.unpause()
    records: list[WriteItem] = []
    cls.t0_ns = _ns(START)
    strategy = cls(TrendPortfolioConfig(**_fields(**overrides)), gate, EntryBlocks(), records.append, None, None)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def _btc(strategy):
    return [(s.side, s.qty, s.reduce_only) for s in strategy.sent if s.coin == BTC]


def _btc_rows(rows):
    return [r.reason for r in rows if r.instrument_id == BTC]


def _sent_at(strategy, index):
    return [s.ts for s in strategy.sent if s.coin == BTC][index] - _ns(START)


@pytest.mark.parametrize(
    ("target", "jump", "reason", "first", "closing"),
    [
        (8000, 40, "take_profit", OrderSide.BUY, OrderSide.SELL),
        (8000, -40, "stop_loss", OrderSide.BUY, OrderSide.SELL),
        (-8000, -40, "take_profit", OrderSide.SELL, OrderSide.BUY),
        (-8000, 40, "stop_loss", OrderSide.SELL, OrderSide.BUY),
    ],
)
def test_a_coin_closes_at_its_dollar_exit_and_goes_back_to_its_target_after_the_wait(
    target, jump, reason, first, closing
):
    engine, strategy, rows = _run(_plan("P", first={BTC: target, ETH: 0}), jump, **DOLLARS)
    again = (D(abs(target)) / (PRICES[BTC] + jump)).quantize(D("0.0001"), rounding=ROUND_FLOOR)  # sized at the new mid

    assert _btc(strategy) == [
        (first, D("0.1000"), False),
        (closing, D("0.1000"), True),
        (first, again, False),
    ]
    assert _btc_rows(rows) == ["start", reason, "reenter"]
    assert _sent_at(strategy, 1) == JUMP_MINUTE * MINUTE_NS
    assert _sent_at(strategy, 2) >= (JUMP_MINUTE + 1) * MINUTE_NS
    assert _position(engine, BTC) == (again if target > 0 else -again)


def test_a_coin_inside_both_exits_is_left_alone():
    _, strategy, _ = _run(_plan("P", first={BTC: 8000, ETH: 0}), 5, **DOLLARS)

    assert _btc(strategy) == [(OrderSide.BUY, D("0.1000"), False)]


def test_with_no_dollar_exits_set_a_big_move_sends_nothing():
    _, strategy, _ = _run(_plan("P", first={BTC: 8000, ETH: 0}), 400)

    assert _btc(strategy) == [(OrderSide.BUY, D("0.1000"), False)]


def test_a_coin_with_a_zero_target_is_not_opened_again():
    class Clears(_Planned):
        first = {BTC: 8000, ETH: 0}  # noqa: RUF012

        def _on_reenter(self, key, event):
            self.targets = {}
            self._make_plan(self.plan_day)
            super()._on_reenter(key, event)

    engine, strategy, _ = _run(Clears, 40, **DOLLARS)

    assert len(_btc(strategy)) == 2
    assert _position(engine, BTC) == 0


def test_a_pause_holds_the_re_entry():
    class Pauses(_Planned):
        first = {BTC: 8000, ETH: 0}  # noqa: RUF012

        def _on_reenter(self, key, event):
            _pause(self)
            super()._on_reenter(key, event)

    engine, strategy, rows = _run(Pauses, 40, **DOLLARS)

    assert len(_btc(strategy)) == 2
    assert _position(engine, BTC) == 0
    assert [r.risk_reason for r in rows if r.instrument_id == BTC][-1].startswith("held:")


@pytest.mark.parametrize(
    "overrides",
    [
        {"coin_take_profit_usd": D(3)},
        {"coin_stop_loss_usd": D(2)},
        {"coin_take_profit_usd": D(0), "coin_stop_loss_usd": D(2)},
        {**DOLLARS, "coin_reenter_wait_s": 0},
    ],
)
def test_the_config_refuses_bad_dollar_exits(overrides):
    with pytest.raises(ValueError, match="coin"):
        TrendPortfolioConfig(**_fields(**overrides))
