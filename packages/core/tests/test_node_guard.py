"""covers: spec 0011 AC-4 (open notional over every instrument), AC-6 (the instrument's maintenance rate),
AC-15 (an open position without a quote cannot be judged), AC-17 (unrealized P&L for the equity).

Drives the cache readers in ``trading_core.node.guard`` against a real NautilusTrader ``Cache`` holding
a BTC and an ETH perpetual position. Needs the ``node`` extra.
"""

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.currencies import BTC, ETH, USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide as NautilusSide
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from trading_core.node import guard
from trading_core.risk.check import InvalidRiskInput


def _perpetual(symbol, base, maint):
    return CryptoPerpetual(
        instrument_id=InstrumentId.from_str(f"{symbol}.KRAKEN"),
        raw_symbol=Symbol(symbol),
        base_currency=base,
        quote_currency=USD,
        settlement_currency=USD,
        is_inverse=False,
        price_precision=1,
        size_precision=4,
        price_increment=Price.from_str("0.1"),
        size_increment=Quantity.from_str("0.0001"),
        margin_init=Decimal("0.02"),
        margin_maint=maint,
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


BTC_PERP = _perpetual("PF_XBTUSD", BTC, Decimal("0.01"))
ETH_PERP = _perpetual("PF_ETHUSD", ETH, Decimal("0.02"))


def _cache():
    cache = Cache()
    cache.add_instrument(BTC_PERP)
    cache.add_instrument(ETH_PERP)
    return cache


def _quote(cache, instrument, bid, ask):
    cache.add_quote_tick(
        QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price.from_str(bid),
            ask_price=Price.from_str(ask),
            bid_size=Quantity.from_str("1.0000"),
            ask_size=Quantity.from_str("1.0000"),
            ts_event=0,
            ts_init=0,
        )
    )


def _open(cache, instrument, side, qty, px, position_id):
    order = TestExecStubs.market_order(instrument=instrument, order_side=side, quantity=Quantity.from_str(qty))
    fill = TestEventStubs.order_filled(
        order, instrument, position_id=PositionId(position_id), last_px=Price.from_str(px)
    )
    position = Position(instrument=instrument, fill=fill)
    cache.add_position(position, OmsType.NETTING)
    return position


def _two_positions():
    cache = _cache()
    _open(cache, BTC_PERP, NautilusSide.BUY, "0.0100", "80000.0", "P-BTC")
    _open(cache, ETH_PERP, NautilusSide.SELL, "0.1000", "3000.0", "P-ETH")
    _quote(cache, BTC_PERP, "80999.0", "81001.0")  # mid 81000
    _quote(cache, ETH_PERP, "2899.0", "2901.0")  # mid 2900
    return cache


def test_the_mid_is_halfway_between_bid_and_ask():
    cache = _cache()
    _quote(cache, BTC_PERP, "80999.0", "81002.0")

    assert guard.mid(cache, BTC_PERP.id) == Decimal("81000.5")


def test_the_mid_is_none_before_the_first_quote():
    assert guard.mid(_cache(), BTC_PERP.id) is None


def test_open_notional_sums_every_instrument_at_its_own_mid():
    # covers: AC-4. 0.01 x 81000 (long BTC) + 0.1 x 2900 (short ETH, counted by size).
    cache = _two_positions()

    assert guard.open_notional(cache) == Decimal("810") + Decimal("290")


def test_open_notional_leaves_out_the_excluded_position():
    cache = _two_positions()

    assert guard.open_notional(cache, exclude=PositionId("P-BTC")) == Decimal("290")


def test_open_notional_is_zero_when_flat():
    assert guard.open_notional(_cache()) == Decimal(0)


def test_an_open_position_without_a_quote_cannot_be_judged():
    # covers: AC-4, AC-15. ETH is open but has never been quoted.
    cache = _cache()
    _open(cache, BTC_PERP, NautilusSide.BUY, "0.0100", "80000.0", "P-BTC")
    _open(cache, ETH_PERP, NautilusSide.SELL, "0.1000", "3000.0", "P-ETH")
    _quote(cache, BTC_PERP, "80999.0", "81001.0")

    with pytest.raises(InvalidRiskInput, match="PF_ETHUSD"):
        guard.open_notional(cache)
    with pytest.raises(InvalidRiskInput, match="PF_ETHUSD"):
        guard.unrealized(cache)


def test_unrealized_counts_every_position_at_its_own_mid():
    # covers: AC-17. Long BTC up 1000 x 0.01 = 10; short ETH down 100 x 0.1 = 10.
    cache = _two_positions()

    assert guard.unrealized(cache) == Decimal(20)


def test_the_maintenance_rate_is_read_from_the_instrument():
    # covers: AC-6, the instrument side of max(instrument, setting).
    assert (guard.margin_maint(BTC_PERP), guard.margin_maint(ETH_PERP)) == (Decimal("0.01"), Decimal("0.02"))
