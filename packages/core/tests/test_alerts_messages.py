"""covers: spec 0013 AC-8 (the live fill push text: side, quantity, price, fee, at the instrument's precision).

Pure.
"""

import pytest

from trading_core.alerts import messages

ROW = {
    "trade_id": "T-1",
    "instrument_id": "PF_XBTUSD.KRAKEN",
    "side": "sell",
    "quantity": "0.0012",
    "price": "67412",
    "fee": "0.0214",
    "fee_currency": "USD",
    "base_currency": "BTC",
    "lot_size": "0.0001",
    "tick_size": "0.5",
}


def test_a_fill_takes_the_lot_and_tick_precision():
    assert messages.fill(ROW) == ("Fill PF_XBTUSD.KRAKEN", "Sold 0.0012 BTC at 67,412.0, fee 0.0214 USD")


@pytest.mark.parametrize(("fee", "shown"), [("0", "0.00"), ("0.1", "0.10"), ("1.23456", "1.23456")])
def test_a_fee_shows_at_least_cents_and_every_digit_it_has(fee, shown):
    assert messages.fill({**ROW, "fee": fee})[1].endswith(f"fee {shown} USD")


def test_a_whole_tick_shows_no_decimals():
    assert "at 3,100, fee" in messages.fill({**ROW, "price": "3100.00", "tick_size": "1"})[1]


@pytest.mark.parametrize("broken", [{"side": "flat"}, {"price": "NaN"}, {"quantity": "x"}, {"lot_size": None}])
def test_a_missing_or_bad_column_falls_back_to_the_trade_id(broken):
    assert messages.fill({**ROW, **broken})[1] == "fill T-1"
