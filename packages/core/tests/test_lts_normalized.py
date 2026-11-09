"""covers: spec 0031 AC-3 (taker sides, trade kinds, an unknown type, one count per id, the TradeTick converter)."""

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide as NautilusAggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.lts_errors import MarketDataIntegrityError
from trading_core.marketdata.normalized import (
    NormalizedTrade,
    TradeDeduper,
    TradeKind,
    parse_taker_side,
    parse_trade_kind,
)
from trading_core.node.lts_feed import normalized_trade

IID = "PF_XBTUSD.KRAKEN"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("fill", TradeKind.FILL),
        ("liquidation", TradeKind.LIQUIDATION),
        ("partialLiquidation", TradeKind.LIQUIDATION),
        ("termination", TradeKind.TERMINATION),
        ("block", TradeKind.BLOCK),
    ],
)
def test_kinds(raw, kind):
    assert parse_trade_kind(raw) is kind


def test_an_unknown_type_is_a_data_fault():
    with pytest.raises(MarketDataIntegrityError):
        parse_trade_kind("adl")


def test_kraken_side_is_the_taker():
    assert parse_taker_side("buy") is AggressorSide.BUY
    assert parse_taker_side("sell") is AggressorSide.SELL
    with pytest.raises(MarketDataIntegrityError):
        parse_taker_side("BUY")


def _tick(aggressor=NautilusAggressorSide.BUYER):
    return TradeTick(
        InstrumentId.from_str(IID),
        Price.from_str("100000.5"),
        Quantity.from_str("0.0200"),
        aggressor,
        TradeId("uid-1"),
        111,
        222,
    )


def test_converter_keeps_decimal_price_size_and_both_times():
    trade = normalized_trade(_tick(), Decimal(1))

    assert trade == NormalizedTrade(
        instrument_id=IID,
        trade_id="uid-1",
        ts_event_ns=111,
        ts_init_ns=222,
        price=Decimal("100000.5"),
        size=Decimal("0.0200"),
        notional=Decimal("2000.01"),
        aggressor=AggressorSide.BUY,
        kind=TradeKind.FILL,
    )


def test_converter_multiplies_by_contract_size():
    assert normalized_trade(_tick(), Decimal("0.5")).notional == Decimal("1000.005")


def test_converter_refuses_a_tick_with_no_aggressor():
    with pytest.raises(MarketDataIntegrityError):
        normalized_trade(_tick(NautilusAggressorSide.NO_AGGRESSOR), Decimal(1))


def test_a_second_trade_with_the_same_id_counts_once():
    trade = normalized_trade(_tick(), Decimal(1))
    deduper = TradeDeduper()

    assert deduper.first_seen(trade) is True
    assert deduper.first_seen(trade) is False
    assert deduper.first_seen(replace(trade, trade_id="uid-2")) is True
