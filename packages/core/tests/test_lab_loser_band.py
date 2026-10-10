"""covers: spec 0008 AC-14 (the loser band math)."""

from decimal import Decimal

import pytest

from trading_core.lab.loser_band import BandTrade, loser_band

D = Decimal


def _trade(net: str, **overrides) -> BandTrade:
    values = {
        "qty": D("0.0125"),
        "entry_notional": D("1000"),
        "exit_notional": D("1000"),
        "entry_spread": D("1"),
        "exit_spread": D("1"),
        "net_pnl": D(net),
    }
    return BandTrade(**(values | overrides))


def test_expected_is_fees_plus_half_the_spread_on_each_side():
    # 0.0005 * 2000 = 1.00 in fees, 0.0125 * (1 + 1) / 2 = 0.0125 in spread.
    assert _trade("0").expected(D("0.0005")) == D("-1.0125")


def test_a_run_losing_what_fees_and_spread_predict_passes():
    trades = [_trade("-1.00"), _trade("-1.05"), _trade("-0.98"), _trade("-1.02")]

    band = loser_band(trades, D("0.0005"))

    assert band.trades_used == 4
    assert band.expected == D("-1.0125")
    assert band.mean_net == D("-1.0125")
    assert band.passed
    assert band.low is not None
    assert band.high is not None
    assert band.low <= band.mean_net <= band.high


def test_setting_the_taker_fee_to_zero_moves_the_band_by_exactly_the_fee_term():
    trades = [_trade("-1.00"), _trade("-1.05"), _trade("-0.98"), _trade("-1.02")]

    with_fee = loser_band(trades, D("0.0005"))
    no_fee = loser_band(trades, D(0))

    assert with_fee.expected is not None
    assert no_fee.expected is not None
    assert no_fee.expected - with_fee.expected == D("0.0005") * D("2000")
    assert no_fee.half_width == with_fee.half_width
    assert not no_fee.passed  # the run lost far more than spread alone explains


def test_the_band_is_at_least_half_a_basis_point_of_mean_notional():
    trades = [_trade("-1.0125"), _trade("-1.0125")]

    band = loser_band(trades, D("0.0005"))

    assert band.std_error == 0
    assert band.half_width == D("0.00005") * D("1000")


def test_funding_and_segment_end_trades_count_in_the_total_only():
    trades = [
        _trade("-1.00"),
        _trade("-1.02"),
        _trade("-50", crosses_funding=True),
        _trade("-60", segment_end=True),
    ]

    band = loser_band(trades, D("0.0005"))

    assert band.trades_used == 2
    assert band.total_net == D("-112.02")
    assert band.mean_net == D("-1.01")


def test_a_winning_run_fails_even_inside_the_band():
    trades = [_trade("1"), _trade("1")]

    assert not loser_band(trades, D(0)).passed


def test_fewer_than_two_trades_cannot_pass():
    band = loser_band([_trade("-1")], D("0.0005"))

    assert (band.trades_used, band.expected, band.passed) == (1, None, False)


def test_a_fee_outside_zero_to_one_is_refused():
    with pytest.raises(ValueError, match="taker fee"):
        loser_band([], D(1))
