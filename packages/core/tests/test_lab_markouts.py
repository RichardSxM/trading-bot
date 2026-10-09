"""covers: spec 0008 AC-10 (markouts: mid at or before fill time + h, signed by side, left out past the segment end)."""

from decimal import Decimal

import pytest

from trading_core.lab.markouts import MarkoutTracker, markout_bps

D = Decimal
S = 1_000_000_000


def test_a_buy_gains_when_the_mid_rises_and_a_sell_loses():
    assert markout_bps(True, D("100"), D("100.01")) == D("1")
    assert markout_bps(False, D("100"), D("100.01")) == D("-1")


def test_a_non_positive_fill_price_is_refused():
    with pytest.raises(ValueError, match="positive"):
        markout_bps(True, D("0"), D("1"))


def test_each_horizon_takes_the_last_mid_at_or_before_its_time():
    tracker = MarkoutTracker()
    tracker.on_mid(0, D("100"))
    tracker.on_fill("t1", buy=True, fill_px=D("100"), fill_ns=10 * S)
    assert tracker.waiting
    tracker.on_mid(10 * S + S // 2, D("100.01"))  # in force at +1 s
    tracker.on_mid(15 * S, D("100.02"))  # exactly at +5 s: counts
    tracker.on_mid(41 * S, D("99.99"))  # after +30 s: the +30 s horizon keeps 100.02

    (result,) = tracker.finish(50 * S)

    assert result.trade_id == "t1"
    assert result.markouts == {1: D("1"), 5: D("2"), 30: D("2")}
    assert not tracker.waiting


def test_a_horizon_after_the_segment_end_is_left_out():
    tracker = MarkoutTracker()
    tracker.on_mid(0, D("100"))
    tracker.on_fill("t1", buy=False, fill_px=D("100"), fill_ns=10 * S)

    (result,) = tracker.finish(12 * S)

    # +1 s is inside the data (the last mid holds); +5 s and +30 s are after its end.
    assert result.markouts == {1: D("0"), 5: None, 30: None}
