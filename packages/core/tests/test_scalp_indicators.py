"""covers: spec 0017 AC-4 (Sma, Bollinger, Rsi, Atr, Adx against hand worked values; None until warm)."""

from decimal import Decimal

import pytest

from trading_core.strategy.scalp_indicators import Adx, Atr, Bollinger, Rsi, Sma, true_range

D = Decimal


def _feed(indicator, values):
    return [indicator.update(D(v)) for v in values]


def test_sma_is_the_mean_of_the_last_n():
    assert _feed(Sma(3), [1, 2, 3, 4, 8]) == [None, None, D(2), D(3), D(5)]


def test_bollinger_uses_the_population_standard_deviation():
    # Mean 5, population std dev exactly 2.
    readings = _feed(Bollinger(8, D(2)), [2, 4, 4, 4, 5, 5, 7, 9])

    assert readings[:7] == [None] * 7
    bands = readings[7]
    assert (bands.lower, bands.middle, bands.upper) == (D(1), D(5), D(9))


def test_flat_closes_give_bands_on_the_middle():
    bands = _feed(Bollinger(3, D("2.5")), [100, 100, 100])[2]

    assert bands.lower == bands.middle == bands.upper == D(100)


def test_rsi_seeds_with_simple_means_then_smooths_wilder_style():
    # Changes +1, +1, -1: gain 2/3, loss 1/3, RS 2. Then +2: gain 10/9, loss 2/9, RS 5.
    readings = _feed(Rsi(3), [10, 11, 12, 11, 13])

    assert readings[:3] == [None, None, None]
    assert readings[3] is not None
    assert readings[3].quantize(D("0.000001")) == D("66.666667")
    assert readings[4].quantize(D("0.000001")) == D("83.333333")


def test_rsi_is_100_when_nothing_was_lost():
    assert _feed(Rsi(2), [1, 2, 3])[2] == D(100)


def test_true_range_takes_a_gap_from_the_previous_close():
    assert true_range(D(20), D(19), D(14)) == D(6)
    assert true_range(D(12), D(10), D(11)) == D(2)


def test_atr_needs_a_previous_close_then_smooths_wilder_style():
    atr = Atr(2)
    bars = [(10, 8, 9), (11, 9, 10), (12, 10, 11), (15, 11, 14)]

    readings = [atr.update(D(h), D(lo), D(c)) for h, lo, c in bars]

    # True ranges 2, 2 seed 2 on the third bar; then 4 gives (2 + 4) / 2.
    assert readings == [None, None, D(2), D(3)]


def test_adx_gives_its_first_value_on_bar_2n():
    adx = Adx(2)
    bars = [(10, 8, 9), (12, 9, 11), (13, 10, 12), (13, 9, 10), (14, 13, 13)]

    readings = [adx.update(D(h), D(lo), D(c)) for h, lo, c in bars]

    # Bar 3: TR 3, +DM 1.5, -DM 0 give DX 100. Bar 4: TR 3.5, +DM 0.75, -DM 0.5 give DX 20, so ADX (100 + 20) / 2.
    assert readings[:3] == [None, None, None]
    assert readings[3] is not None
    assert readings[3].quantize(D("0.000001")) == D(60)
    assert readings[4] is not None


def test_a_bar_with_no_range_gives_dx_0():
    adx = Adx(2)

    readings = [adx.update(D(100), D(100), D(100)) for _ in range(4)]

    assert readings == [None, None, None, D(0)]


@pytest.mark.parametrize(
    "make", [lambda: Sma(0), lambda: Rsi(0), lambda: Atr(0), lambda: Adx(0), lambda: Bollinger(20, D(0))]
)
def test_bad_periods_are_refused(make):
    with pytest.raises(ValueError, match="needs"):
        make()
