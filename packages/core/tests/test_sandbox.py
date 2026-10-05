"""covers: spec 0006 AC-2 and AC-10, the sandbox config and the fees it sets on every instrument.

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image.
"""

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.test_kit.providers import TestInstrumentProvider

from trading_core.node.sandbox import FeeSandboxConfig, sandbox_exec_client, with_fees
from trading_core.settings import PaperLoopSettings


def test_with_fees_changes_only_the_fees():
    instrument = TestInstrumentProvider.btcusdt_perp_binance()

    patched = with_fees(instrument, Decimal("0.0002"), Decimal("0.0005"))

    assert (patched.maker_fee, patched.taker_fee) == (Decimal("0.0002"), Decimal("0.0005"))
    assert patched.id == instrument.id
    assert patched.price_increment == instrument.price_increment
    assert patched.size_increment == instrument.size_increment
    assert (patched.ts_event, patched.ts_init) == (instrument.ts_event, instrument.ts_init)


def test_sandbox_config_is_a_usd_margin_netting_account_filling_on_quotes():
    settings = PaperLoopSettings(trading_env="demo")

    config = sandbox_exec_client(settings)

    assert isinstance(config, FeeSandboxConfig)
    assert config.venue == "KRAKEN"
    assert config.starting_balances == ["1000 USD"]
    assert (config.oms_type, config.account_type, config.base_currency) == ("NETTING", "MARGIN", "USD")
    assert config.default_leverage == Decimal(2)
    assert (config.bar_execution, config.trade_execution) == (False, False)
    assert (config.maker_fee, config.taker_fee) == (Decimal("0.0002"), Decimal("0.0005"))
    # A stop whose trigger is already in the market is rejected, so AC-7 handles it.
    assert config.reject_stop_orders is True
    assert config.reject_every_stop is False


def test_the_stop_reject_drill_setting_reaches_the_sandbox_config():
    settings = PaperLoopSettings(trading_env="demo", sandbox_reject_stop_orders=True)

    config = sandbox_exec_client(settings)

    assert (config.reject_every_stop, config.reject_stop_orders) == (True, True)


@pytest.fixture
def make_client():
    import asyncio

    loops = []

    def make(reject_every_stop):
        loops.append(asyncio.new_event_loop())
        return _client(loops[-1], reject_every_stop)

    yield make
    for loop in loops:
        loop.close()


def _client(loop, reject_every_stop):
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.portfolio.portfolio import Portfolio
    from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

    from trading_core.node.sandbox import FeeSandboxExecutionClient

    clock = LiveClock()
    msgbus = MessageBus(TestIdStubs.trader_id(), clock)
    cache = Cache()
    events = []
    msgbus.register("ExecEngine.process", events.append)
    config = sandbox_exec_client(PaperLoopSettings(trading_env="demo", sandbox_reject_stop_orders=reject_every_stop))
    client = FeeSandboxExecutionClient(
        loop=loop,
        portfolio=Portfolio(msgbus, cache, clock),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=config,
    )
    sent = []
    client._client = type("Exchange", (), {"submit_order": staticmethod(sent.append)})()
    return client, events, sent


def _submit(order):
    from nautilus_trader.execution.messages import SubmitOrder
    from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

    return SubmitOrder(
        trader_id=TestIdStubs.trader_id(),
        strategy_id=TestIdStubs.strategy_id(),
        order=order,
        command_id=TestIdStubs.uuid(),
        ts_init=0,
    )


def _orders():
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

    factory = OrderFactory(TestIdStubs.trader_id(), TestIdStubs.strategy_id(), TestClock())
    instrument_id = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
    market = factory.market(instrument_id, OrderSide.BUY, Quantity.from_str("0.0011"))
    stop = factory.stop_market(
        instrument_id, OrderSide.SELL, Quantity.from_str("0.0011"), Price.from_str("84000"), reduce_only=True
    )
    return market, stop


def test_the_stop_reject_drill_rejects_a_stop_and_passes_other_orders_on(make_client):
    from nautilus_trader.model.events import OrderRejected, OrderSubmitted

    from trading_core.node.sandbox import DRILL_REJECT_REASON

    client, events, sent = make_client(reject_every_stop=True)
    market, stop = _orders()

    client.submit_order(_submit(market))
    client.submit_order(_submit(stop))

    assert [command.order for command in sent] == [market]  # the stop never reaches the exchange
    assert [type(event) for event in events] == [OrderSubmitted, OrderRejected]
    assert {event.client_order_id for event in events} == {stop.client_order_id}
    assert events[1].reason == DRILL_REJECT_REASON


def test_without_the_drill_a_stop_goes_to_the_exchange(make_client):
    client, events, sent = make_client(reject_every_stop=False)
    _, stop = _orders()

    client.submit_order(_submit(stop))

    assert [command.order for command in sent] == [stop]
    assert events == []
