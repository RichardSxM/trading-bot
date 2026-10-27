"""covers: spec 0021 AC-2 (``imbalance_fade``: the parent long faded to a short and the parent short to a long,
each with its stop and target mirrored around the same mid, the parent's inputs passed through, and every
case where the parent gives no signal still giving none, and a parent signal with no Decimal mid refused)."""

from decimal import Decimal

import pytest
from test_strategy_book_imbalance import EVEN, HEAVY_ASK, HEAVY_BID, K0, MID, _at, _fire

from trading_core.recording.enums import OrderSide
from trading_core.strategy.book_imbalance import BookImbalanceState
from trading_core.strategy.imbalance_fade import ImbalanceFadeState
from trading_core.strategy.scalp import ScalpSignal

D = Decimal
SETTINGS = {"imb_min": D("0.3"), "persist_bars": 2, "levels": 5, "stop_bps": D(9), "target_bps": D(6)}


def _pair():
    return BookImbalanceState(**SETTINGS), ImbalanceFadeState(**SETTINGS)  # type: ignore[arg-type]


def _feed(states, book, aggressor):
    """Two firings of ``book``, with one ``aggressor`` trade in the second bucket; each state's last signal."""
    signals = []
    for state in states:
        _fire(state, K0, book)
        state.on_trade(_at(K0 + 1, 1), D(2), aggressor)
        signals.append(_fire(state, K0 + 1, book))
    return signals


def test_the_parent_long_is_faded_to_a_short_with_the_stop_above_and_the_target_below():
    parent, fade = _feed(_pair(), HEAVY_BID, "buy")

    assert parent is not None
    assert parent.side == OrderSide.BUY
    assert fade is not None
    assert fade.side == OrderSide.SELL
    assert fade.stop_px == MID * (1 + D(9) / 10_000)
    assert fade.target_px == MID * (1 - D(6) / 10_000)


def test_the_parent_short_is_faded_to_a_long_with_the_stop_below_and_the_target_above():
    parent, fade = _feed(_pair(), HEAVY_ASK, "sell")

    assert parent is not None
    assert parent.side == OrderSide.SELL
    assert fade is not None
    assert fade.side == OrderSide.BUY
    assert fade.stop_px == MID * (1 - D(9) / 10_000)
    assert fade.target_px == MID * (1 + D(6) / 10_000)


def test_the_inputs_are_the_parents_unchanged():
    parent, fade = _feed(_pair(), HEAVY_BID, "buy")

    assert parent is not None
    assert fade is not None
    assert fade.inputs == parent.inputs
    assert fade.inputs == {"imbalance": D("0.5"), "bid_depth": D(15), "ask_depth": D(5), "flow": D(2), "mid": MID}


def test_the_stop_and_target_use_the_configured_bps():
    fade = ImbalanceFadeState(**(SETTINGS | {"stop_bps": D(12), "target_bps": D(9)}))  # type: ignore[arg-type]
    [signal] = _feed([fade], HEAVY_ASK, "sell")

    assert signal is not None
    assert (signal.stop_px, signal.target_px) == (MID * (1 - D(12) / 10_000), MID * (1 + D(9) / 10_000))


@pytest.mark.parametrize(("book", "aggressor"), [(HEAVY_BID, "sell"), (HEAVY_ASK, "buy"), (HEAVY_BID, "none")])
def test_imbalance_without_matching_flow_gives_no_signal(book, aggressor):
    assert _feed(_pair(), book, aggressor) == [None, None]


def test_imbalance_that_does_not_persist_gives_no_signal():
    _, fade = _pair()
    _fire(fade, K0, EVEN)
    fade.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(fade, K0 + 1, HEAVY_BID) is None


@pytest.mark.parametrize("book", [([], [D(1)] * 5), ([D(1)] * 5, [])])
def test_an_empty_book_side_gives_no_signal(book):
    _, fade = _pair()
    _fire(fade, K0, HEAVY_BID)
    fade.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(fade, K0 + 1, book) is None


def test_no_mid_gives_no_signal():
    _, fade = _pair()
    _fire(fade, K0, HEAVY_BID)
    fade.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(fade, K0 + 1, HEAVY_BID, mid=None) is None


def test_bad_settings_are_refused_as_the_parent_refuses_them():
    with pytest.raises(ValueError, match="imb_min"):
        ImbalanceFadeState(**(SETTINGS | {"imb_min": D(1)}))  # type: ignore[arg-type]


def test_a_parent_signal_without_a_decimal_mid_is_refused(monkeypatch):
    bad = ScalpSignal(OrderSide.BUY, MID, MID, {"mid": str(MID)})
    monkeypatch.setattr(BookImbalanceState, "on_firing", lambda *args: bad)

    with pytest.raises(TypeError, match="mid must be a Decimal"):
        _fire(_pair()[1], K0, HEAVY_BID)
