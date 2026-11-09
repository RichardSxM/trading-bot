"""covers: spec 0029 AC-1 (``SixVoteRule``: the six votes, the score crossing, the stop, readiness, values).

A steady climb (close +1 a bar, high and low 1 either side, volume 1) makes V1, V3, and V4 vote long while V5
and V6 stay 0; a jump bar (close +10, closing at its high, volume 10) adds the breakout and the volume spike.
The ATR is the base's Wilder ATR(14), fed as the node feeds it. Pure: no Nautilus.
"""

from decimal import Decimal

import pytest

from trading_core.strategy.bar_indicators import Atr
from trading_core.strategy.bar_rule import BarInput, Side
from trading_core.strategy.six_vote import READY_BARS, EwmEma, EwmRsi, SixVoteRule, rsi_vote

D = Decimal
HOUR_NS = 3_600_000_000_000
ONE, TEN = D(1), D(10)


class Feeder:
    """Feeds bars to a rule with the base's ATR(14)."""

    def __init__(self, btc=1):
        self.rule = SixVoteRule()
        self.rule.set_btc_trend(btc)
        self.atr = Atr(14)
        self.n = 0
        self.close = D(1000)

    def bar(self, close, high, low, volume=ONE):
        self.n += 1
        atr = self.atr.update(high, low, close)
        self.rule.update(BarInput(self.n * HOUR_NS, close, high, low, close, volume), atr)
        self.close = close
        return atr

    def climb(self, bars, step=ONE):
        for _ in range(bars):
            c = self.close + step
            self.bar(c, c + 1, c - 1)

    def jump(self, size=TEN, volume=TEN):
        """A bar closing ``size`` beyond the last close at its extreme, with ``volume``."""
        c = self.close + size
        high, low = (c, self.close) if size > 0 else (self.close, c)
        return self.bar(c, high, low, volume)


def _ready_climb(bars=250):
    f = Feeder()
    f.climb(bars)
    return f


def test_a_steady_climb_votes_trend_ema_and_rsi_long_with_no_breakout_or_volume():
    f = _ready_climb()
    values = f.rule.values()
    assert (values["v_trend"], values["v_btc"], values["v_ema"], values["v_rsi"]) == (1, 1, 1, 1)
    assert (values["v_breakout"], values["v_volume"], values["score"]) == (0, 0, 4)
    assert f.rule.entry() is None


def test_all_six_agreeing_gives_one_long_with_the_stop_three_atr_below_the_close():
    f = _ready_climb()
    atr = f.jump()
    signal = f.rule.entry()
    assert f.rule.values()["score"] == 6
    assert signal is not None
    assert (signal.side, signal.reason) == (Side.LONG, "six_vote")
    assert signal.structural_stop == f.close - 3 * atr


def test_the_same_score_on_the_next_bar_gives_no_second_signal():
    f = _ready_climb()
    f.jump()
    f.rule.consumed()
    f.jump()
    assert f.rule.values()["score"] == 6
    assert f.rule.entry() is None


def test_six_then_five_then_six_gives_a_second_signal():
    f = _ready_climb()
    f.jump()
    f.rule.consumed()
    f.climb(1)  # no breakout, no volume: 4
    assert f.rule.values()["score"] < 6
    f.jump()
    assert f.rule.entry() is not None


def test_entry_is_a_pure_read_and_consumed_clears_it():
    f = _ready_climb()
    f.jump()
    assert f.rule.entry() == f.rule.entry()
    f.rule.consumed()
    assert f.rule.entry() is None


def test_a_falling_market_with_btc_down_gives_a_short_three_atr_above():
    f = Feeder(btc=-1)
    f.climb(250, step=D(-1))
    atr = f.jump(size=D(-10))
    signal = f.rule.entry()
    assert f.rule.values()["score"] == -6
    assert signal is not None
    assert signal.side == Side.SHORT
    assert signal.structural_stop == f.close + 3 * atr


def test_btc_against_the_coin_blocks_the_signal():
    f = Feeder(btc=-1)
    f.climb(250)
    f.jump()
    assert (f.rule.values()["v_btc"], f.rule.values()["score"]) == (-1, 4)
    assert f.rule.entry() is None


def test_v2_is_zero_until_set():
    rule = SixVoteRule()
    f = Feeder()
    f.rule = rule
    f.climb(250)
    assert rule.values()["v_btc"] == 0


@pytest.mark.parametrize("bad", [2, -2])
def test_a_btc_trend_outside_minus_one_to_one_is_refused(bad):
    with pytest.raises(ValueError, match="BTC trend"):
        SixVoteRule().set_btc_trend(bad)


def test_ready_after_two_hundred_bars_with_an_atr():
    assert READY_BARS == 200
    f = Feeder()
    f.climb(199)
    assert not f.rule.ready()
    assert f.rule.values()["score"] == 0
    f.climb(1)
    assert f.rule.ready()


def test_not_ready_without_an_atr():
    rule = SixVoteRule()
    for n in range(1, 260):
        rule.update(BarInput(n * HOUR_NS, D(n), D(n + 1), D(n - 1), D(n), D(1)), None)
    assert not rule.ready()
    assert rule.entry() is None


def test_a_volume_spike_needs_three_times_the_mean():
    f = _ready_climb()
    f.jump(volume=D(3))  # exactly 3 x the mean of 1: not above
    assert f.rule.values()["v_volume"] == 0
    assert f.rule.values()["v_breakout"] == 1


def test_a_spike_closing_mid_bar_votes_zero_on_volume():
    f = _ready_climb()
    c = f.close + 5
    f.bar(c, c + 5, c - 5, D(10))  # close in the middle of a 10 wide bar
    assert f.rule.values()["v_volume"] == 0


def test_a_zero_range_bar_votes_zero_on_volume():
    f = _ready_climb()
    c = f.close + 10
    f.bar(c, c, c, D(10))
    assert f.rule.values()["v_volume"] == 0


@pytest.mark.parametrize(
    ("low_gap", "vote"),
    [(D(15), 1), (D("14.8"), 0), (D(5), -1), (D("5.2"), 0)],
)
def test_the_volume_vote_takes_the_top_and_bottom_quarters_inclusive(low_gap, vote):
    # covers: AC-1 (V6: (close - low) / range >= 0.75 votes +1, <= 0.25 votes -1)
    f = _ready_climb()
    c = f.close + 1
    f.bar(c, c - low_gap + 20, c - low_gap, D(10))  # a 20 wide bar, far above 1.5 x ATR
    assert f.rule.values()["v_volume"] == vote


def test_the_breakout_reads_the_previous_twenty_bars_only():
    f = _ready_climb()
    c = f.close + 1
    f.bar(c, c + 50, c - 1)  # a high far above: the next bars cannot break out for 20 bars
    for _ in range(20):
        f.bar(f.close + 1, f.close + 1, f.close)  # each bar closes at its high, above the last bar's high
        assert f.rule.values()["v_breakout"] == 0
    f.bar(f.close + 1, f.close + 1, f.close)
    assert f.rule.values()["v_breakout"] == 1


def test_v3_is_zero_when_the_close_is_on_the_other_side_of_ema9():
    f = _ready_climb()
    c = f.close - 6  # a dip under EMA9 while EMA9 stays above EMA21
    f.bar(c, c + 1, c - 1)
    values = f.rule.values()
    assert D(str(values["ema9"])) > D(str(values["ema21"]))
    assert values["v_ema"] == 0


@pytest.mark.parametrize(
    ("rsi", "vote"),
    [(D(55), 0), (D("55.0001"), 1), (D(45), 0), (D("44.9999"), -1), (D(50), 0), (D(100), 1), (D(0), -1)],
)
def test_the_rsi_vote_is_zero_at_exactly_55_and_45(rsi, vote):
    assert rsi_vote(rsi) == vote


def test_the_ema_is_ewm_adjust_false_seeded_with_the_first_close():
    ema = EwmEma(3)  # alpha 0.5
    assert [ema.update(D(x)) for x in (1, 2, 3)] == [D(1), D("1.5"), D("2.25")]


def test_the_rsi_is_ewm_of_moves_with_a_first_move_of_zero_and_100_while_down_is_zero():
    rsi = EwmRsi(14)
    assert rsi.update(D(1)) == 100
    assert rsi.update(D(2)) == 100
    third = rsi.update(D(1))
    up, down = D(1) / 14 * D(13) / 14, D(1) / 14
    assert third == 100 - 100 / (1 + up / down)


def test_values_hold_the_score_the_six_votes_and_the_indicators():
    f = _ready_climb()
    assert set(f.rule.values()) == {
        "score",
        "v_trend",
        "v_btc",
        "v_ema",
        "v_rsi",
        "v_breakout",
        "v_volume",
        "ema9",
        "ema21",
        "ema200",
        "rsi14",
    }


def test_the_rule_never_exits_and_on_flat_changes_nothing():
    f = _ready_climb()
    f.jump()
    before = f.rule.entry()
    f.rule.on_flat()
    assert f.rule.entry() == before
    assert f.rule.exit(None) is None
