"""covers: spec 0017 AC-5 (the pure refusal order, spread, reward floor, stop side, target and time stop),
AC-18 (the stop already through at the fill), and AC-13 (``bollinger_rsi``: a long, a short, non flat bands
refused, warmup)."""

from decimal import Decimal

import pytest

from trading_core.recording.enums import OrderSide
from trading_core.strategy.bollinger_rsi import BollingerRsiState
from trading_core.strategy.scalp import (
    MINUTE_NS,
    EntryGate,
    ScalpSignal,
    pre_refusal,
    round_stop,
    spread_bps,
    stop_through,
    target_hit,
    time_stop_due,
)

D = Decimal


def _gate(**changes) -> EntryGate:
    values = {
        "halted": False,
        "paused": False,
        "blocks": (),
        "bid": D("99.95"),
        "ask": D("100.05"),
        "max_spread_bps": D(20),
        "min_target_bps": D("10.5"),
    }
    return EntryGate(**{**values, **changes})


LONG = ScalpSignal(OrderSide.BUY, stop_px=D(99), target_px=D("100.2"))


def test_spread_is_in_bps_of_the_mid():
    assert spread_bps(D("99.95"), D("100.05")) == D(10)


def test_a_good_signal_passes_every_check():
    assert pre_refusal(LONG, _gate()) is None


@pytest.mark.parametrize(
    ("changes", "signal", "reason"),
    [
        ({"halted": True, "paused": True, "blocks": ("stale_feed",)}, LONG, "halted"),
        ({"paused": True, "blocks": ("stale_feed",)}, LONG, "paused"),
        ({"blocks": ("trade_cap", "regime"), "max_spread_bps": D(1)}, LONG, "blocked:regime"),
        ({"max_spread_bps": D("9.99")}, ScalpSignal(OrderSide.BUY, D(101), D("100.01")), "spread_cap"),
        ({}, ScalpSignal(OrderSide.BUY, D(101), D("100.1")), "target_too_close"),
        ({}, ScalpSignal(OrderSide.BUY, D(100), D("100.2")), "invalid_stop"),
        ({}, ScalpSignal(OrderSide.SELL, D("99.9"), D("99.8")), "invalid_stop"),
    ],
)
def test_refusals_come_in_their_fixed_order(changes, signal, reason):
    assert pre_refusal(signal, _gate(**changes)) == reason


def test_an_unset_cap_or_floor_checks_nothing():
    signal = ScalpSignal(OrderSide.BUY, D(99), D("100.01"))

    assert pre_refusal(signal, _gate(max_spread_bps=None, min_target_bps=None, ask=D(101))) is None


def test_the_stop_rounds_away_from_the_entry():
    assert round_stop(OrderSide.BUY, D("100.37"), D("0.5")) == D("100.0")
    assert round_stop(OrderSide.SELL, D("100.37"), D("0.5")) == D("100.5")
    assert round_stop(OrderSide.SELL, D("100.5"), D("0.5")) == D("100.5")


def test_the_target_is_reached_by_the_bid_for_a_long_and_the_ask_for_a_short():
    assert target_hit(True, D(101), D("101.5"), D(101))
    assert not target_hit(True, D("100.5"), D(101), D(101))
    assert target_hit(False, D("98.5"), D(99), D(99))
    assert not target_hit(False, D(99), D("99.5"), D(99))


def test_a_stop_is_through_when_the_quote_or_the_fill_price_is_at_or_past_it():
    # long(bid, ask, avg_px, stop): the bid or the fill at or below the stop.
    assert not stop_through(True, D(100), D(101), D("100.5"), D(99))
    assert stop_through(True, D(99), D(100), D("100.5"), D(99))
    assert stop_through(True, D(100), D(101), D(99), D(99))
    # A short: the ask or the fill at or above the stop.
    assert not stop_through(False, D(99), D(100), D("99.5"), D(101))
    assert stop_through(False, D(100), D(101), D("99.5"), D(101))
    assert stop_through(False, D(99), D(100), D(101), D(101))


def test_the_time_stop_is_due_at_its_minute():
    assert not time_stop_due(0, 15, 15 * MINUTE_NS - 1)
    assert time_stop_due(0, 15, 15 * MINUTE_NS)


def _state(**changes) -> BollingerRsiState:
    values = {
        "bb_n": 4,
        # One outlier in a window of 4 is at most 1.5 population deviations out, so k is small here.
        "bb_k": D("1.4"),
        "rsi_n": 2,
        "rsi_low": 30,
        "atr_n": 2,
        "flat_band_frac": D("0.6"),
        "stop_atr_buffer": D("0.25"),
    }
    return BollingerRsiState(**{**values, **changes})


def _feed(state, closes):
    """Bars with high = close + 1 and low = close - 1; returns every signal."""
    return [state.on_bar(D(c) + 1, D(c) - 1, D(c)) for c in closes]


# A quiet range (every close one deviation from the middle, never outside k = 1.4), then an excursion.
FLAT = [100, 101] * 5


def test_a_close_back_inside_after_a_drop_below_the_band_is_a_long():
    state = _state()
    signals = _feed(state, [*FLAT, 94, 99])

    assert signals[:-1] == [None] * 11
    long = signals[-1]
    assert long.side == OrderSide.BUY
    # Lowest low of the run (94 - 1) and bar t (99 - 1), minus 0.25 ATR.
    assert long.stop_px == D(93) - D("0.25") * long.inputs["atr"]
    assert long.target_px == long.inputs["middle"]
    assert set(long.inputs) == {"close", "lower", "middle", "upper", "rsi_prev", "atr", "band_drift"}


def test_the_mirror_is_a_short():
    state = _state()
    signals = _feed(state, [*FLAT, 107, 102])

    assert signals[:-1] == [None] * 11
    short = signals[-1]
    assert short.side == OrderSide.SELL
    assert short.stop_px == D(108) + D("0.25") * short.inputs["atr"]


def test_bands_that_drift_are_not_flat_and_refuse_the_signal():
    state = _state(flat_band_frac=D(0))

    assert _feed(state, [*FLAT, 94, 99])[-1] is None


def test_no_signal_before_twice_bb_n_bars():
    state = _state()
    # The excursion lands on bars 7 and 8: bands exist, but 2 * bb_n bars have not come before.
    signals = _feed(state, [100, 101, 100, 101, 100, 101, 94, 99])

    assert signals == [None] * 8
    assert not state.warm
    state.on_bar(D(101), D(99), D(100))
    assert state.warm  # the 9th bar has 8 before it


def test_rsi_low_must_be_under_50():
    with pytest.raises(ValueError, match="rsi_low"):
        _state(rsi_low=50)
