"""covers: the trend portfolio's daily book take profit and stop, the flat day, and the next day's re-entry.

Runs ``TrendPortfolio`` in a BacktestEngine with the two coins of ``test_node_trend_portfolio``, where BTC jumps
at a set minute. Needs the ``node`` extra.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from types import SimpleNamespace

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
    NS_PER_S,
    PRICES,
    START,
    _fields,
    _logs,
    _ns,
    _plan,
    _Planned,
    _position,
    _Sent,
)

from trading_core.node.strategy import PauseGate
from trading_core.node.trend_portfolio import DAILY, WEEKLY, TrendPortfolioConfig, day_run_ns
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.book_exit import BOOK_STOP_LOSS, BOOK_TAKE_PROFIT

# 0.4 % of an 8,000 USD book is 32 USD: a 400 USD move on 0.1 BTC is 40 USD, a 100 USD move is 10.
LIMITS = {"book_take_profit_pct": D("0.004"), "book_stop_loss_pct": D("0.004")}
JUMP_MINUTE = 2
TUESDAY = datetime(2026, 10, 6, 0, 1, tzinfo=UTC)  # the daily run at 00:05 is 4 minutes in, and is not weekly


def _ticks(coin, jump, minutes, start):
    data, at = [], start
    for step in range(minutes * 6):
        px = PRICES[coin] + (jump if coin == BTC and step / 6 >= JUMP_MINUTE else 0)
        ts, iid, q = _ns(at), INSTRUMENT.id if coin == BTC else COIN_ID, Quantity.from_str("5.0000")
        data.append(TradeTick(iid, Price(px, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER,
                              TradeId(f"{iid.symbol}-{step}"), ts, ts))  # fmt: skip
        data.append(QuoteTick(iid, Price(px - 1, 0), Price(px + 1, 0), q, q, ts, ts))
        at += timedelta(seconds=10)
    return data


def _run(cls, jump, minutes=5, start=START, last_book_exit_ns=None, **overrides):
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
        engine.add_data(_ticks(coin, jump, minutes, start))
    gate = PauseGate()
    gate.unpause()
    records: list[WriteItem] = []
    cls.t0_ns = _ns(start)
    config = TrendPortfolioConfig(**_fields(**overrides))
    strategy = cls(config, gate, EntryBlocks(), records.append, None, None, last_book_exit_ns)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records if isinstance(item.row, DecisionRow)]


def _orders(strategy, coin=BTC):
    return [(s.side, s.qty, s.reduce_only) for s in strategy.sent if s.coin == coin]


def _reasons(rows, coin=BTC):
    return [r.reason for r in rows if r.instrument_id == coin]


@pytest.mark.parametrize(
    ("target", "jump", "reason", "first", "closing"),
    [
        (8000, 400, BOOK_TAKE_PROFIT, OrderSide.BUY, OrderSide.SELL),
        (8000, -400, BOOK_STOP_LOSS, OrderSide.BUY, OrderSide.SELL),
        (-8000, -400, BOOK_TAKE_PROFIT, OrderSide.SELL, OrderSide.BUY),
        (-8000, 400, BOOK_STOP_LOSS, OrderSide.SELL, OrderSide.BUY),
    ],
)
def test_the_book_closes_at_its_daily_limit_and_stays_flat(target, jump, reason, first, closing):
    engine, strategy, rows = _run(_plan("P", first={BTC: target, ETH: 0}), jump, **LIMITS)

    assert _orders(strategy) == [(first, D("0.1000"), False), (closing, D("0.1000"), True)]
    assert _reasons(rows) == ["start", reason]
    # The book is checked once a second on whichever coin quotes first, so at the jump or one 10 s tick later.
    assert 0 <= [s.ts for s in strategy.sent][1] - _ns(START) - JUMP_MINUTE * MINUTE_NS <= 10 * NS_PER_S
    assert _position(engine, BTC) == 0
    assert strategy.day_flat
    assert strategy.plan_snapshot()["day_flat_reason"] == reason


def test_every_coin_closes_not_only_the_one_that_moved():
    engine, strategy, rows = _run(_plan("P", first={BTC: 8000, ETH: 3000}), 600, **LIMITS)

    assert _orders(strategy, ETH)[-1] == (OrderSide.SELL, D("1.000"), True)
    assert _reasons(rows, ETH) == ["start", BOOK_TAKE_PROFIT]
    assert (_position(engine, BTC), _position(engine, ETH)) == (0, 0)


def test_a_move_inside_both_limits_sends_nothing():
    _, strategy, _ = _run(_plan("P", first={BTC: 8000, ETH: 0}), 100, **LIMITS)

    assert _orders(strategy) == [(OrderSide.BUY, D("0.1000"), False)]
    assert not strategy.day_flat


def test_with_no_limits_set_a_big_move_sends_nothing():
    _, strategy, _ = _run(_plan("P", first={BTC: 8000, ETH: 0}), 4000)

    assert _orders(strategy) == [(OrderSide.BUY, D("0.1000"), False)]


def test_only_the_take_profit_set_leaves_a_loss_alone():
    _, strategy, _ = _run(_plan("P", first={BTC: 8000, ETH: 0}), -400, book_take_profit_pct=D("0.004"))

    assert _orders(strategy) == [(OrderSide.BUY, D("0.1000"), False)]


def test_a_rebalance_on_a_flat_day_is_held(caplog):
    cls = _plan("P", first={BTC: 8000, ETH: 0}, later=((3, {BTC: 8000, ETH: 3000}),))
    with caplog.at_level(logging.INFO):
        engine, strategy, _ = _run(cls, 400, **LIMITS)

    assert len(strategy.sent) == 2  # the start buy and the book's close
    assert (_position(engine, BTC), _position(engine, ETH)) == (0, 0)
    [held] = _logs(caplog, "day_flat")
    assert held.fields["reason"] == WEEKLY


def test_the_next_daily_run_ends_the_flat_day_and_goes_back_to_the_targets():
    engine, strategy, rows = _run(_plan("P", first={BTC: 8000, ETH: 0}), 400, minutes=6, start=TUESDAY, **LIMITS)
    again = (D(8000) / (PRICES[BTC] + 400)).quantize(D("0.0001"), rounding=ROUND_FLOOR)  # sized at the new mid

    assert _orders(strategy) == [
        (OrderSide.BUY, D("0.1000"), False),
        (OrderSide.SELL, D("0.1000"), True),
        (OrderSide.BUY, again, False),
    ]
    assert _reasons(rows) == ["start", BOOK_TAKE_PROFIT, DAILY]
    assert [s.ts for s in strategy.sent][2] >= _ns(TUESDAY) + 4 * MINUTE_NS  # 00:05
    assert _position(engine, BTC) == again
    assert not strategy.day_flat


def test_a_start_on_the_day_of_a_book_exit_stays_flat(caplog):
    with caplog.at_level(logging.INFO):
        _, strategy, rows = _run(
            _plan("P", first={BTC: 8000, ETH: 0}), 0, last_book_exit_ns=_ns(START) - 60 * NS_PER_S, **LIMITS
        )

    assert strategy.sent == []
    assert rows == []
    assert strategy.day_flat
    assert len(_logs(caplog, "book_flat_resumed")) == 1


def test_a_start_after_an_earlier_days_book_exit_trades_as_normal():
    _, strategy, _ = _run(
        _plan("P", first={BTC: 8000, ETH: 0}), 0, last_book_exit_ns=_ns(START - timedelta(days=1)), **LIMITS
    )

    assert _orders(strategy) == [(OrderSide.BUY, D("0.1000"), False)]
    assert not strategy.day_flat


class _RefusesCloses(_Planned):
    """The first ``refuse`` reduce only orders are denied inside ``submit_order``."""

    refuse = 1

    def submit_order(self, order, *args, **kwargs):
        if not order.is_reduce_only or type(self).refuse <= 0:
            super().submit_order(order, *args, **kwargs)
            return
        type(self).refuse -= 1
        coin = str(order.instrument_id)
        self.sent.append(_Sent(self.clock.timestamp_ns(), coin, order.side, order.quantity.as_decimal(), True, order))
        self.on_order_denied(SimpleNamespace(client_order_id=order.client_order_id, reason="test says no"))


def test_a_refused_close_is_sent_again_after_the_retry_wait(caplog):
    cls = _plan("P", base=_RefusesCloses, first={BTC: 8000, ETH: 0}, refuse=1)
    with caplog.at_level(logging.INFO):
        engine, strategy, rows = _run(cls, 400, minutes=6, **LIMITS)

    closes = [s.ts for s in strategy.sent if s.reduce_only]
    assert len(closes) == 2
    assert 60 * NS_PER_S <= closes[1] - closes[0] <= 62 * NS_PER_S
    assert _position(engine, BTC) == 0
    assert _reasons(rows) == ["start", BOOK_TAKE_PROFIT]  # the retry reuses the decision
    assert len(_logs(caplog, "book_exit_refused")) == 1


def test_the_day_starts_at_the_newest_run_time():
    noon = _ns(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))

    assert day_run_ns(noon, 0, 5) == _ns(datetime(2026, 10, 6, 0, 5, tzinfo=UTC))
    assert day_run_ns(_ns(datetime(2026, 10, 6, 0, 4, tzinfo=UTC)), 0, 5) == _ns(
        datetime(2026, 10, 5, 0, 5, tzinfo=UTC)
    )


@pytest.mark.parametrize("overrides", [{"book_take_profit_pct": D(0)}, {"book_stop_loss_pct": D("-0.01")}])
def test_the_config_refuses_a_limit_at_or_below_zero(overrides):
    with pytest.raises(ValueError, match="book exit limit"):
        TrendPortfolioConfig(**_fields(**overrides))
