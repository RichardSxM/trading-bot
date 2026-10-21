"""covers: spec 0017 AC-4 (``SessionVwap``) and AC-14 (``vwap_retest``: a long retest, a short retest, a touch that
closes through VWAP, the anchor reset at 00:00 UTC, and the warmup after an anchor)."""

from decimal import Decimal

from trading_core.recording.enums import OrderSide
from trading_core.strategy.scalp_indicators import SessionVwap
from trading_core.strategy.vwap_retest import DAY_NS, VwapRetestState

D = Decimal
MINUTE = 60_000_000_000
# 2025-09-01 00:00 UTC.
DAY0 = 1_756_684_800_000_000_000


def test_session_vwap_weights_the_typical_price_by_volume_and_resets_at_its_anchor():
    vwap = SessionVwap()
    assert vwap.update(D(12), D(9), D(9), D(0)) is None  # no volume yet

    assert vwap.update(D(12), D(9), D(9), D(1)) == D(10)
    assert vwap.update(D(22), D(19), D(19), D(3)) == D("17.5")  # typical 10 and 20: (10 * 1 + 20 * 3) / 4
    vwap.anchor(5)
    assert (vwap.value, vwap.bars, vwap.anchor_ns) == (None, 0, 5)


def _state(min_bars=25):
    return VwapRetestState(atr_n=3, vwap_min_bars=min_bars, stop_atr=D(1), target_r=D(2))


def _trend(state, start_ns, n, step):
    """``n`` bars moving ``step`` a minute from 100, volume 1, a one unit range around each close."""
    signals = []
    for i in range(n):
        close = D(100) + step * i
        signals.append(state.on_bar(start_ns + i * MINUTE, close + D("0.5"), close - D("0.5"), close, D(1)))
    return signals


def _pullback(state, at_ns, *, below, above):
    """A bar with no volume (the VWAP stays put): its range reaches ``below`` and ``above`` the VWAP."""
    vwap = state.vwap.value
    return vwap, state.on_bar(at_ns, vwap + above, vwap - below, vwap + above / 2 if above else vwap - below / 2, D(0))


def test_a_pullback_that_touches_vwap_and_closes_above_it_in_an_uptrend_is_a_long():
    state = _state()
    assert _trend(state, DAY0, 30, D(1)) == [None] * 30

    vwap, signal = _pullback(state, DAY0 + 30 * MINUTE, below=D(1), above=D(4))

    assert signal.side == OrderSide.BUY
    atr = signal.inputs["atr"]
    assert signal.stop_px == vwap - atr
    close = vwap + 2
    assert signal.target_px == close + 2 * (close - signal.stop_px)
    assert signal.inputs["low"] == vwap - 1
    assert signal.inputs["bars_since_anchor"] == 31


def test_the_mirror_in_a_downtrend_is_a_short():
    state = _state()
    _trend(state, DAY0, 30, D(-1))
    vwap = state.vwap.value

    signal = state.on_bar(DAY0 + 30 * MINUTE, vwap + 1, vwap - 4, vwap - 2, D(0))

    assert signal.side == OrderSide.SELL
    assert signal.stop_px == vwap + signal.inputs["atr"]
    assert signal.inputs["high"] == vwap + 1


def test_a_touch_that_closes_through_vwap_gives_no_signal():
    state = _state()
    _trend(state, DAY0, 30, D(1))
    vwap = state.vwap.value

    assert state.on_bar(DAY0 + 30 * MINUTE, vwap + 1, vwap - 4, vwap - 2, D(0)) is None


def test_the_vwap_anchors_again_at_00_00_utc_and_waits_vwap_min_bars():
    state = _state()
    # 23:30 to 00:29: the session starts again at midnight.
    late = DAY0 + DAY_NS - 30 * MINUTE
    _trend(state, late, 60, D(1))
    assert state.vwap.anchor_ns == DAY0 + DAY_NS
    assert state.vwap.bars == 30

    # A retest 30 bars into the new session: past vwap_min_bars (25), so it signals.
    _, signal = _pullback(state, DAY0 + DAY_NS + 30 * MINUTE, below=D(1), above=D(4))
    assert signal is not None


def test_no_signal_before_vwap_min_bars_after_an_anchor():
    state = _state(min_bars=40)
    late = DAY0 + DAY_NS - 30 * MINUTE
    _trend(state, late, 60, D(1))  # EMAs warm, but only 30 bars under the new anchor

    _, signal = _pullback(state, DAY0 + DAY_NS + 30 * MINUTE, below=D(1), above=D(4))

    assert signal is None
    assert state.vwap.bars == 31
