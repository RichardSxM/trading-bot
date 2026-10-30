"""covers: spec 0023 AC-8 (the ``ReconcileExecutor``'s residual close: Kraken's quantity from a fresh fetch right
before each attempt, a reduce only market order on the closing side with no position id, nothing when Kraken is
flat; through the kill switch it runs for every untrusted instrument) and AC-9 (it never needs a stop).

The executor sits in a BacktestEngine for its cache and order factory; ``submit_order`` is replaced by a recorder,
so nothing reaches a venue. Needs the ``node`` extra.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_placeholder import INSTRUMENT
from test_node_reconcile import FakeExchange

from trading_core.node.reconcile_executor import ReconcileExecutor, ReconcileExecutorConfig
from trading_core.node.strategy import PauseGate
from trading_core.reconcile.views import ExchangeUnavailable, ExchangeView, PositionView
from trading_core.recording.enums import PositionSide

BTC = str(INSTRUMENT.id)
FLAT = ExchangeView(orders=(), positions=(), fetched_ns=1)


def _holding(side=PositionSide.LONG, qty="0.0020"):
    position = PositionView(instrument_id=BTC, side=side, quantity=Decimal(qty))
    return ExchangeView(orders=(), positions=(position,), fetched_ns=1)


def _executor(exchange):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(1000, USD)])
    engine.add_instrument(INSTRUMENT)
    executor = ReconcileExecutor(
        ReconcileExecutorConfig(instrument_id=BTC), PauseGate(), frozenset, exchange=exchange, residual_retry_s=0
    )
    engine.add_strategy(executor)
    sent = []
    # A recorder in place of the venue: nothing is ever submitted.
    executor.submit_order = lambda order, position_id=None, *args, **kwargs: sent.append(  # type: ignore[method-assign]
        (order, position_id)
    )
    return executor, sent


def _run_residual(executor, tags=None):
    async def run():
        executor.close_residual(BTC, tags)
        while executor.residual_running(BTC):
            await asyncio.sleep(0)

    asyncio.run(asyncio.wait_for(run(), 5))


def test_it_closes_kraken_s_quantity_reduce_only_without_a_position_id():
    exchange = FakeExchange(_holding(), FLAT)
    executor, sent = _executor(exchange)

    _run_residual(executor)

    ((order, position_id),) = sent
    assert (order.order_type, order.side, order.quantity.as_decimal()) == (
        OrderType.MARKET,
        NautilusOrderSide.SELL,
        Decimal("0.0020"),
    )
    assert order.is_reduce_only
    assert position_id is None  # never the wrong cached position
    assert "origin:reconciliation" in order.tags


def test_a_short_on_kraken_is_bought_back():
    executor, sent = _executor(FakeExchange(_holding(PositionSide.SHORT, "0.0010"), FLAT))

    _run_residual(executor)

    ((order, _),) = sent
    assert (order.side, order.quantity.as_decimal()) == (NautilusOrderSide.BUY, Decimal("0.0010"))


def test_nothing_is_sent_when_kraken_is_flat():
    executor, sent = _executor(FakeExchange(FLAT))

    _run_residual(executor)

    assert sent == []


def test_each_attempt_reads_kraken_again_and_a_fetch_error_only_waits():
    exchange = FakeExchange(ExchangeUnavailable("down"), _holding(qty="0.0020"), _holding(qty="0.0005"), FLAT)
    executor, sent = _executor(exchange)

    _run_residual(executor)

    assert [order.quantity.as_decimal() for order, _ in sent] == [Decimal("0.0020"), Decimal("0.0005")]
    assert exchange.fetches == 4


def test_a_second_call_while_it_runs_starts_no_second_close():
    executor, sent = _executor(FakeExchange(_holding(), FLAT))

    async def run():
        executor.close_residual(BTC)
        executor.close_residual(BTC)
        while executor.residual_running(BTC):
            await asyncio.sleep(0)

    asyncio.run(run())

    assert len(sent) == 1


def test_the_kill_switch_flatten_starts_it_for_every_untrusted_instrument():
    executor, sent = _executor(FakeExchange(_holding(), FLAT))
    halt_id = uuid.uuid4()

    async def run():
        executor.flatten_for(halt_id, frozenset({BTC}))
        assert executor.has_open_positions()  # the kill switch keeps waiting while it runs
        while executor.residual_running(BTC):
            await asyncio.sleep(0)

    asyncio.run(run())

    ((order, _),) = sent
    assert order.tags == ["origin:kill_switch", f"halt:{halt_id}"]
    assert not executor.has_open_positions()


def test_the_executor_needs_no_stop():
    """AC-9: its positions are always being closed."""
    assert ReconcileExecutor.uses_stop is False


def test_strategies_that_never_place_a_stop_say_so():
    """AC-9: ``BuyAndHold`` and ``RandomTaker`` have no stop to check."""
    from trading_core.node.buy_and_hold import BuyAndHold
    from trading_core.node.random_taker import RandomTaker
    from trading_core.node.strategy import KfsStrategy

    assert (KfsStrategy.uses_stop, BuyAndHold.uses_stop, RandomTaker.uses_stop) == (True, False, False)
