"""covers: spec 0014 AC-13 (the Decimal EMA and MACD)."""

from decimal import Decimal

import pytest

from trading_core.strategy.indicators import Ema, Macd

D = Decimal


def test_the_ema_is_none_until_seeded_with_the_simple_mean():
    ema = Ema(3)
    assert [ema.update(D(x)) for x in (1, 2)] == [None, None]
    assert ema.update(D(6)) == D(3)  # (1 + 2 + 6) / 3


def test_each_later_step_moves_by_alpha_two_over_n_plus_one():
    ema = Ema(3)
    for x in (1, 2, 6):
        ema.update(D(x))
    # alpha = 2 / 4 = 0.5: 3 + 0.5 x (7 - 3) = 5
    assert ema.update(D(7)) == D(5)
    assert isinstance(ema.value, Decimal)


def test_the_ema_needs_n_of_at_least_one():
    with pytest.raises(ValueError, match="at least 1"):
        Ema(0)


def test_the_first_histogram_arrives_on_input_slow_plus_signal_minus_one():
    macd = Macd(3, 5, 4)
    readings = [macd.update(D(100 + i)) for i in range(12)]
    first = next(i for i, r in enumerate(readings) if r is not None)
    assert first + 1 == 5 + 4 - 1
    assert all(r is not None for r in readings[first:])


def test_the_histogram_is_the_line_minus_its_signal():
    macd, fast, slow, signal = Macd(2, 3, 2), Ema(2), Ema(3), Ema(2)
    closes = [D(x) for x in (10, 12, 11, 15, 14, 9, 13)]
    for close in closes:
        point = macd.update(close)
        f, s = fast.update(close), slow.update(close)
        sig = signal.update(f - s) if f is not None and s is not None else None
        if point is None:
            continue
        assert sig is not None
        assert f is not None
        assert s is not None
        assert (point.macd, point.signal, point.histogram) == (f - s, sig, f - s - sig)


def test_a_steady_rise_gives_a_positive_line():
    macd = Macd(3, 6, 3)
    point = None
    for i in range(20):
        point = macd.update(D(100 + 2 * i))
    assert point is not None
    assert point.macd > 0


def test_fast_must_be_below_slow():
    with pytest.raises(ValueError, match="fast below slow"):
        Macd(26, 12, 9)
