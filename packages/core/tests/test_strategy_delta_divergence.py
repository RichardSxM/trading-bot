"""covers: spec 0017 AC-4 (``DeltaWindow``) and AC-15 (``delta_divergence``: a bearish divergence, a bullish one,
a move with matching delta, ``NO_AGGRESSOR`` trades counting 0, a boundary trade landing in its ``ts_event``
minute whatever its arrival order, and a late trade counted in ``late_trades``)."""

from decimal import Decimal

from trading_core.recording.enums import OrderSide
from trading_core.strategy.delta_divergence import DeltaDivergenceState
from trading_core.strategy.scalp_indicators import MINUTE_NS, DeltaWindow

D = Decimal
M0 = 29_000_000  # a minute index (minutes since the epoch)


def _at(minute, offset_ns=0):
    return minute * MINUTE_NS + offset_ns


def _closed(window, minute):
    """``close(minute)`` of a window that is warm."""
    found = window.close(minute)
    assert found is not None
    return found


def test_the_window_sums_the_last_n_closed_minutes_and_counts_quiet_minutes_as_0():
    window = DeltaWindow(2)
    window.add(_at(M0, 5), D(1), "buy")
    assert window.close(M0) is None  # not warm: one minute closed
    window.add(_at(M0 + 1, 5), D("0.5"), "sell")

    first = _closed(window, M0 + 1)
    second = _closed(window, M0 + 2)  # minute M0 + 2 had no trades

    assert (first.delta, first.buy_size, first.sell_size) == (D("0.5"), D(1), D("0.5"))
    assert (second.delta, second.buy_size, second.sell_size) == (D("-0.5"), D(0), D("0.5"))


def test_no_aggressor_trades_count_0():
    window = DeltaWindow(1)
    window.add(_at(M0, 1), D(7), "none")

    assert _closed(window, M0).delta == 0


def test_a_boundary_trade_lands_in_its_ts_event_minute_whatever_its_arrival_order():
    early, late = DeltaWindow(1), DeltaWindow(1)
    trades = [(_at(M0 + 1), D(2), "buy"), (_at(M0 + 1) - 1, D(3), "sell")]  # 00:01:00.000 and 00:00:59.999...

    for args in trades:
        early.add(*args)
    for args in reversed(trades):
        late.add(*args)

    assert _closed(early, M0).delta == _closed(late, M0).delta == D(-3)
    assert _closed(early, M0 + 1).delta == _closed(late, M0 + 1).delta == D(2)


def test_a_trade_of_a_minute_already_closed_is_dropped_and_counted():
    window = DeltaWindow(1)
    window.close(M0)
    window.add(_at(M0, 10), D(5), "buy")

    assert window.late_trades == 1
    assert _closed(window, M0 + 1).delta == 0


def _state():
    return DeltaDivergenceState(lookback_bars=3, move_atr=D("1.5"), atr_n=2, stop_atr_buffer=D("0.25"))


def _bars(state, closes, trades_per_minute):
    """One bar per close with a one unit range; ``trades_per_minute(i)`` gives (size, aggressor) of minute i."""
    signals = []
    for i, close in enumerate(closes):
        size, aggressor = trades_per_minute(i)
        state.on_trade(_at(M0 + i, 30), D(size), aggressor)
        c = D(close)
        signals.append(state.on_bar(M0 + i, c + D("0.5"), c - D("0.5"), c))
    return signals


# Flat for 4 minutes (ATR settles at 1), then up 3 a minute: a move of 9 over 3 bars, above 1.5 ATRs.
UP = [100, 100, 100, 100, 103, 106, 109]
DOWN = [100, 100, 100, 100, 97, 94, 91]


def test_a_rise_on_selling_is_a_bearish_divergence_and_a_short():
    state = _state()
    signals = _bars(state, UP, lambda i: (1, "sell"))

    short = signals[-1]
    assert short.side == OrderSide.SELL
    assert short.inputs["move"] == D(9)
    assert short.target_px == D(109) - D("4.5")
    assert short.stop_px == D("109.5") + D("0.25") * short.inputs["atr"]
    assert set(short.inputs) == {"close", "move", "atr", "delta", "buy_size", "sell_size"}


def test_a_fall_on_buying_is_a_bullish_divergence_and_a_long():
    state = _state()
    long = _bars(state, DOWN, lambda i: (1, "buy"))[-1]

    assert long.side == OrderSide.BUY
    assert long.target_px == D(91) + D("4.5")
    assert long.stop_px == D("90.5") - D("0.25") * long.inputs["atr"]


def test_a_move_backed_by_its_delta_gives_no_signal():
    state = _state()

    assert _bars(state, UP, lambda i: (1, "buy"))[-1] is None
