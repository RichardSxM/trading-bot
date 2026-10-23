"""covers: spec 0018 AC-6 (SMA, Wilder RSI, Wilder ATR, Bollinger bands: hand worked values and warmup counts)."""

from decimal import Decimal

import pytest

from trading_core.strategy.bar_indicators import Atr, Bollinger, Rsi, Sma

D = Decimal


def test_the_sma_is_none_until_n_inputs_then_the_mean_of_the_last_n():
    sma = Sma(3)
    assert [sma.update(D(x)) for x in (1, 2, 3, 4)] == [None, None, D(2), D(3)]


def test_the_rsi_seeds_with_the_mean_of_the_first_n_changes_and_is_ready_from_input_n_plus_one():
    rsi = Rsi(2)
    assert [rsi.update(D(x)) for x in (10, 12)] == [None, None]
    # Changes +2, -1: average gain 1, average loss 0.5, RS 2, RSI 100 - 100 / 3.
    assert rsi.update(D(11)) == D(100) - D(100) / D(3)
    # +2: gain (1 x 1 + 2) / 2 = 1.5, loss (0.5 x 1 + 0) / 2 = 0.25, RS 6.
    assert rsi.update(D(13)) == D(100) - D(100) / D(7)


def test_the_rsi_is_100_with_no_losses_and_50_when_nothing_moves():
    rising, flat = Rsi(3), Rsi(3)
    for x in (1, 2, 3, 4, 5):
        rising.update(D(x))
        flat.update(D(7))
    assert rising.value == D(100)
    assert flat.value == D(50)


def test_the_atr_uses_the_true_range_seeds_with_its_mean_and_is_ready_from_bar_n_plus_one():
    atr = Atr(2)
    assert atr.update(D(10), D(8), D(9)) is None  # the first bar has no true range
    assert atr.update(D(12), D(9), D(11)) is None  # TR max(3, |12 - 9|, |9 - 9|) = 3
    assert atr.update(D(11), D(10), D(10)) == D(2)  # TR max(1, 0, |10 - 11|) = 1; (3 + 1) / 2
    # A gap up: TR max(2, |14 - 10|, |12 - 10|) = 4; (2 x 1 + 4) / 2.
    assert atr.update(D(14), D(12), D(13)) == D(3)


def test_bollinger_bands_use_the_population_standard_deviation():
    bands = Bollinger(4, D(2))
    assert [bands.update(D(x)) for x in (1, 2, 3)] == [None, None, None]
    point = bands.update(D(4))
    assert point is not None
    # Mean 2.5, variance (2.25 + 0.25 + 0.25 + 2.25) / 4 = 1.25.
    width = 2 * D("1.25").sqrt()
    assert (point.middle, point.upper, point.lower) == (D("2.5"), D("2.5") + width, D("2.5") - width)
    assert isinstance(point.upper, Decimal)


def test_bollinger_on_a_20_close_window():
    bands = Bollinger(20, D(2))
    closes = [D(100 + (i % 5)) for i in range(20)]  # 100..104 four times: mean 102, variance 2
    for c in closes:
        point = bands.update(c)
    assert point is not None
    assert point.middle == D(102)
    assert point.upper == D(102) + 2 * D(2).sqrt()


@pytest.mark.parametrize("make", [lambda: Sma(0), lambda: Rsi(0), lambda: Atr(0), lambda: Bollinger(0, D(2))])
def test_each_indicator_needs_n_of_at_least_one(make):
    with pytest.raises(ValueError, match="at least 1"):
        make()


def test_bollinger_needs_k_above_zero():
    with pytest.raises(ValueError, match="above 0"):
        Bollinger(20, D(0))
