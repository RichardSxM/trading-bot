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
    events: list[object] = []
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
    sent: list[object] = []
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


@pytest.fixture
def make_exchange():
    """A sandbox client on its real simulated exchange; events are collected, never applied.

    That is how a live node behaves: its execution engine queues events, so the order in the cache
    still reads ``SUBMITTED`` while the exchange fills it.
    """
    import asyncio

    loops = []

    def make():
        from nautilus_trader.cache.cache import Cache
        from nautilus_trader.common.component import LiveClock, MessageBus
        from nautilus_trader.portfolio.portfolio import Portfolio
        from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

        from trading_core.node.sandbox import FeeSandboxExecutionClient

        loops.append(asyncio.new_event_loop())
        clock = LiveClock()
        msgbus = MessageBus(TestIdStubs.trader_id(), clock)
        cache = Cache()
        cache.add_instrument(_instrument())
        events: list[object] = []
        msgbus.register("ExecEngine.process", events.append)
        client = FeeSandboxExecutionClient(
            loop=loops[-1],
            portfolio=Portfolio(msgbus, cache, clock),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=sandbox_exec_client(PaperLoopSettings(trading_env="demo")),
        )
        client.connect()
        return client, cache, events

    yield make
    for loop in loops:
        loop.close()


def _instrument():
    from nautilus_trader.model.currencies import BTC, USD
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import CryptoPerpetual
    from nautilus_trader.model.objects import Price, Quantity

    return CryptoPerpetual(
        instrument_id=InstrumentId.from_str("PF_XBTUSD.KRAKEN"),
        raw_symbol=Symbol("PF_XBTUSD"),
        base_currency=BTC,
        quote_currency=USD,
        settlement_currency=USD,
        is_inverse=False,
        price_precision=0,
        size_precision=4,
        price_increment=Price.from_str("1"),
        size_increment=Quantity.from_str("0.0001"),
        margin_init=Decimal("0.02"),
        margin_maint=Decimal("0.01"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        ts_event=0,
        ts_init=0,
    )


def _thin_quote(bid_size="0.0002"):
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity

    return QuoteTick(
        instrument_id=InstrumentId.from_str("PF_XBTUSD.KRAKEN"),
        bid_price=Price.from_str("86089"),
        ask_price=Price.from_str("86090"),
        bid_size=Quantity(Decimal(bid_size), 4),
        ask_size=Quantity.from_str("0.0002"),
        ts_event=1,
        ts_init=1,
    )


def test_a_market_order_larger_than_the_top_of_book_fills_in_full(make_exchange):
    """The 2026-10-05 11:00 UTC demo entry: 0.0002 of 0.0011 filled, the rest silently dropped."""
    from nautilus_trader.model.events import OrderFilled

    client, cache, events = make_exchange()
    client.on_data(_thin_quote())
    market, _ = _orders()
    cache.add_order(market)

    client.submit_order(_submit(market))

    fills = [event for event in events if isinstance(event, OrderFilled)]
    assert sum(fill.last_qty.as_decimal() for fill in fills) == Decimal("0.0011")
    assert {fill.last_px.as_decimal() for fill in fills} == {Decimal(86090)}  # a buy, all at the best ask


def test_deep_quote_raises_thin_sizes_and_keeps_prices_and_times():
    from nautilus_trader.model.objects import Quantity

    from trading_core.node.sandbox import DEEP_BOOK_SIZE, deep_quote

    thin = _thin_quote()
    deep = deep_quote(thin)

    assert deep.bid_size == deep.ask_size == Quantity(DEEP_BOOK_SIZE, 4)
    assert (deep.bid_price, deep.ask_price, deep.ts_event, deep.ts_init) == (
        thin.bid_price,
        thin.ask_price,
        thin.ts_event,
        thin.ts_init,
    )
    assert deep_quote(_thin_quote(bid_size="5000")).bid_size == Quantity.from_str("5000.0000")  # deeper is kept
