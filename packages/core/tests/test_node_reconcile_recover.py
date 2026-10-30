"""covers: spec 0023 AC-11 (``ExecEngineReports.recover`` lands Kraken's missed fills on our own cached order
through Nautilus 1.231.0's own ``LiveExecutionEngine.reconcile_execution_report``, keeps Kraken's price, time and
trade id, ends the order ``FILLED`` or ``CANCELED`` with its filled quantity, and a repeat feeds nothing twice).

A real ``LiveExecutionEngine`` over a fresh cache and message bus, with no clients: the reports go through the
same path the live trader's check uses. Needs the ``node`` extra.
"""

import asyncio
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import TradeId, VenueOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from test_node_placeholder import INSTRUMENT

from trading_core.node.reconcile import ExecEngineReports, ReconcileActionError, order_view
from trading_core.reconcile.recover import recover
from trading_core.reconcile.views import FillView
from trading_core.recording.enums import OrderSide

BTC = str(INSTRUMENT.id)


@pytest.fixture
def setup():
    loop = asyncio.new_event_loop()
    clock = TestClock()
    msgbus = MessageBus(TestIdStubs.trader_id(), clock)
    cache = Cache()
    cache.add_instrument(INSTRUMENT)
    engine = LiveExecutionEngine(loop, msgbus, cache, clock, LiveExecEngineConfig())
    yield engine, cache
    loop.close()


def _order(cache, *, venue="V-1", qty="0.0010"):
    order = TestExecStubs.limit_order(
        instrument=INSTRUMENT, order_side=NautilusOrderSide.SELL, quantity=Quantity.from_str(qty)
    )
    order.apply(TestEventStubs.order_submitted(order))
    if venue is not None:
        order.apply(TestEventStubs.order_accepted(order, venue_order_id=VenueOrderId(venue)))
    cache.add_order(order)
    return order


def _fill(trade, *, venue="V-1", client=None, qty="0.0010", ts=1_000):
    return FillView(
        trade_id=trade,
        instrument_id=BTC,
        venue_order_id=venue,
        client_order_id=client,
        side=OrderSide.SELL,
        quantity=Decimal(qty),
        price=Decimal("60123"),
        ts_event=ts,
        commission=Decimal("0.03"),
        liquidity_side="taker",
    )


def _recovery(order, fills):
    (recovery,) = recover([order_view(order)], fills, since_ns=0)
    return recovery


def test_a_missed_fill_lands_on_our_order_with_kraken_s_price_time_and_trade_id(setup):
    engine, cache = setup
    order = _order(cache)
    sink = ExecEngineReports(engine, cache, now_ns=lambda: 2_000)

    sink.recover(_recovery(order, [_fill("T-1")]))

    landed = cache.order(order.client_order_id)
    assert landed.status is OrderStatus.FILLED
    assert landed.trade_ids == [TradeId("T-1")]
    assert landed.avg_px == pytest.approx(60123)
    (filled,) = [e for e in landed.events if isinstance(e, OrderFilled)]
    assert (filled.ts_event, filled.trade_id) == (1_000, TradeId("T-1"))  # Kraken's venue time


def test_an_order_without_a_venue_id_is_accepted_first_then_filled(setup):
    """Nautilus finds a fill's order through its venue order id."""
    engine, cache = setup
    order = _order(cache, venue=None)
    sink = ExecEngineReports(engine, cache)

    sink.recover(_recovery(order, [_fill("T-1", venue="V-9", client=str(order.client_order_id))]))

    landed = cache.order(order.client_order_id)
    assert (landed.status, landed.venue_order_id) == (OrderStatus.FILLED, VenueOrderId("V-9"))


def test_a_partial_fill_ends_the_order_canceled_with_its_filled_quantity(setup):
    engine, cache = setup
    order = _order(cache)

    ExecEngineReports(engine, cache).recover(_recovery(order, [_fill("T-1", qty="0.0004")]))

    landed = cache.order(order.client_order_id)
    assert landed.status is OrderStatus.CANCELED
    assert landed.filled_qty == Quantity.from_str("0.0004")


def test_two_fills_land_in_venue_time_order(setup):
    engine, cache = setup
    order = _order(cache)

    ExecEngineReports(engine, cache).recover(
        _recovery(order, [_fill("T-2", qty="0.0006", ts=2_000), _fill("T-1", qty="0.0004", ts=1_000)])
    )

    landed = cache.order(order.client_order_id)
    assert landed.trade_ids == [TradeId("T-1"), TradeId("T-2")]
    assert landed.status is OrderStatus.FILLED


def test_a_repeat_run_finds_nothing_left_to_recover(setup):
    engine, cache = setup
    order = _order(cache, qty="0.0010")
    fills = [_fill("T-1", qty="0.0004")]
    ExecEngineReports(engine, cache).recover(_recovery(order, fills))

    assert recover([order_view(cache.order(order.client_order_id))], fills, since_ns=0) == ()


def test_fills_on_the_other_side_are_refused(setup):
    engine, cache = setup
    order = _order(cache)
    fill = _fill("T-1")
    wrong = FillView(**{**fill.__dict__, "side": OrderSide.BUY})

    with pytest.raises(ReconcileActionError):
        ExecEngineReports(engine, cache).recover(_recovery(order, [wrong]))
