"""covers: spec 0017 AC-22 (``book_imbalance``: a long, a short, imbalance without matching flow, imbalance
that does not persist for ``persist_bars`` firings, an empty book side, a boundary trade landing in the
bucket of its ``ts_event``, and a quiet bucket still evaluated with flow 0)."""

from decimal import Decimal

import pytest

from trading_core.recording.enums import OrderSide
from trading_core.strategy.book_imbalance import BUCKET_NS, BookImbalanceState
from trading_core.strategy.scalp_indicators import DeltaWindow

D = Decimal
K0 = 116_000_000  # a bucket index (15 s buckets since the epoch)
MID = D(60_000)
HEAVY_BID = ([D(3)] * 5, [D(1)] * 5)  # imbalance 0.5
HEAVY_ASK = ([D(1)] * 5, [D(3)] * 5)  # imbalance -0.5
EVEN = ([D(2)] * 5, [D(2)] * 5)  # imbalance 0


def _state(**overrides):
    fields = {"imb_min": D("0.3"), "persist_bars": 2, "levels": 5, "stop_bps": D(9), "target_bps": D(6)} | overrides
    return BookImbalanceState(**fields)  # type: ignore[arg-type]


def _at(bucket, offset_ns=0):
    return bucket * BUCKET_NS + offset_ns


def _fire(state, bucket, book, mid=MID):
    return state.on_firing(bucket, book[0], book[1], mid)


def test_a_heavy_bid_with_buying_flow_two_firings_running_is_a_long():
    state = _state()
    state.on_trade(_at(K0, 1), D(1), "buy")
    assert _fire(state, K0, HEAVY_BID) is None  # one imbalance value so far
    state.on_trade(_at(K0 + 1, 1), D(2), "buy")

    signal = _fire(state, K0 + 1, HEAVY_BID)

    assert signal is not None
    assert signal.side == OrderSide.BUY
    assert signal.stop_px == MID * (1 - D(9) / 10_000)
    assert signal.target_px == MID * (1 + D(6) / 10_000)
    assert signal.inputs == {"imbalance": D("0.5"), "bid_depth": D(15), "ask_depth": D(5), "flow": D(2), "mid": MID}


def test_a_heavy_ask_with_selling_flow_is_a_short():
    state = _state()
    _fire(state, K0, HEAVY_ASK)
    state.on_trade(_at(K0 + 1, 1), D(1), "sell")

    signal = _fire(state, K0 + 1, HEAVY_ASK)

    assert signal is not None
    assert signal.side == OrderSide.SELL
    assert signal.stop_px == MID * (1 + D(9) / 10_000)
    assert signal.target_px == MID * (1 - D(6) / 10_000)


@pytest.mark.parametrize(("book", "aggressor"), [(HEAVY_BID, "sell"), (HEAVY_ASK, "buy"), (HEAVY_BID, "none")])
def test_imbalance_without_matching_flow_gives_no_signal(book, aggressor):
    state = _state()
    _fire(state, K0, book)
    state.on_trade(_at(K0 + 1, 1), D(1), aggressor)

    assert _fire(state, K0 + 1, book) is None


def test_imbalance_that_does_not_persist_gives_no_signal():
    state = _state()
    _fire(state, K0, EVEN)
    state.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(state, K0 + 1, HEAVY_BID) is None  # the firing before was not lopsided


@pytest.mark.parametrize("book", [([], [D(1)] * 5), ([D(1)] * 5, []), ([D(0)], [D(1)])])
def test_an_empty_book_side_gives_no_signal_and_restarts_the_persistence_count(book):
    state = _state()
    _fire(state, K0, HEAVY_BID)
    assert _fire(state, K0 + 1, book) is None
    state.on_trade(_at(K0 + 2, 1), D(1), "buy")

    assert _fire(state, K0 + 2, HEAVY_BID) is None  # only one value since the empty side


def test_no_mid_yet_gives_no_signal():
    state = _state()
    _fire(state, K0, HEAVY_BID)
    state.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(state, K0 + 1, HEAVY_BID, mid=None) is None


def test_only_the_top_levels_count():
    state = _state(levels=2)
    _fire(state, K0, ([D(1), D(1), D(100)], [D(1), D(1)]))  # a far level the rule ignores: imbalance 0
    state.on_trade(_at(K0 + 1, 1), D(1), "buy")

    assert _fire(state, K0 + 1, HEAVY_BID) is None


def test_a_boundary_trade_lands_in_the_bucket_of_its_ts_event():
    state = _state()
    _fire(state, K0, HEAVY_BID)
    state.on_trade(_at(K0 + 2), D(5), "sell")  # the first instant of bucket K0 + 2
    state.on_trade(_at(K0 + 2) - 1, D(1), "buy")  # the last instant of bucket K0 + 1

    signal = _fire(state, K0 + 1, HEAVY_BID)

    assert signal is not None
    assert signal.inputs["flow"] == D(1)


def test_a_quiet_bucket_is_still_evaluated_with_flow_0():
    state = _state()
    _fire(state, K0, HEAVY_BID)

    assert _fire(state, K0 + 1, HEAVY_BID) is None  # no trades: flow 0 is not above 0
    state.on_trade(_at(K0 + 2, 1), D(1), "buy")
    assert _fire(state, K0 + 2, HEAVY_BID) is not None  # the quiet firing still counted toward persistence


def test_a_trade_of_a_bucket_already_read_is_dropped_and_counted():
    state = _state()
    _fire(state, K0, HEAVY_BID)
    state.on_trade(_at(K0, 1), D(1), "buy")

    assert state.window.late_trades == 1


def test_a_delta_window_takes_its_own_bucket_size():
    window = DeltaWindow(1, BUCKET_NS)
    window.add(_at(K0, 1), D(2), "buy")
    window.add(_at(K0 + 1, 1), D(5), "sell")

    found = window.close(K0)

    assert found is not None
    assert found.delta == D(2)


@pytest.mark.parametrize(
    "overrides", [{"imb_min": D(0)}, {"imb_min": D(1)}, {"persist_bars": 0}, {"levels": 0}, {"stop_bps": D(0)}]
)
def test_bad_settings_are_refused(overrides):
    with pytest.raises(ValueError, match="imb_min"):
        _state(**overrides)
