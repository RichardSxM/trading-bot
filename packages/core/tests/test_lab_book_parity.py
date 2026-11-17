"""covers: spec 0033 AC-10a, the parity compare: daily returns within 10 bps, the cumulative return within
1 percentage point, sides equal per coin per day except near the band edge, and the same days in both."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_core.lab.book_parity import BookDay, compare_books

D0 = date(2024, 1, 1)
BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
START = Decimal(10000)


def book(equities, sides=None):
    sides = sides or [{BTC: 1, ETH: -1}] * len(equities)
    return [
        BookDay(D0 + timedelta(days=i), Decimal(e), {c: Decimal(s) for c, s in side.items()})
        for i, (e, side) in enumerate(zip(equities, sides, strict=True))
    ]


def test_identical_books_pass():
    ref = book(["10100", "10050", "10200"])
    result = compare_books(ref, ref, START, Decimal("0.3"), {})
    assert result.passed
    assert result.worst_day_bps == 0
    assert result.cumulative_gap_pp == 0
    assert result.days == 3


def test_a_day_off_by_more_than_10_bps_fails():
    ref = book(["10000", "10000"])
    other = book(["10011", "10011"])  # day 1 return +11 bps, day 2 the same as ref
    result = compare_books(ref, other, START, Decimal(0), {})
    assert not result.passed
    assert result.worst_day == D0
    assert result.worst_day_bps == pytest.approx(Decimal(11), abs=Decimal("0.0001"))


def test_small_daily_gaps_that_add_up_past_1_pp_fail():
    # Nine bps a day for 12 days: every day passes, the cumulative gap does not.
    ref = book(["10000"] * 12)
    other = book([str(10000 * Decimal("1.0009") ** (i + 1)) for i in range(12)])
    result = compare_books(ref, other, START, Decimal(0), {})
    assert result.worst_day_bps < 10
    assert result.cumulative_gap_pp > 1
    assert not result.passed


def test_a_side_miss_fails_unless_near_the_band_edge():
    ref = book(["10000", "10000"])
    other = book(["10000", "10000"], [{BTC: 1, ETH: -1}, {BTC: 0, ETH: -1}])
    far = compare_books(ref, other, START, Decimal("0.3"), {D0 + timedelta(days=1): {BTC: Decimal("0.5")}})
    assert not far.passed
    assert [(m.coin, m.reference, m.other, m.excused) for m in far.side_misses] == [(BTC, 1, 0, False)]
    near = compare_books(ref, other, START, Decimal("0.3"), {D0 + timedelta(days=1): {BTC: Decimal("0.305")}})
    assert near.passed
    assert near.side_misses[0].excused
    assert near.unexcused == ()


def test_a_side_miss_on_a_coin_the_plan_did_not_judge_is_not_excused():
    ref = book(["10000"])
    other = book(["10000"], [{BTC: -1, ETH: -1}])
    result = compare_books(ref, other, START, Decimal("0.3"), {})
    assert not result.passed
    assert result.side_misses[0].gap is None


def test_different_days_fail():
    ref = book(["10000", "10000"])
    result = compare_books(ref, ref[:1], START, Decimal(0), {})
    assert not result.passed
    assert result.unmatched_days == (D0 + timedelta(days=1),)


def test_bad_inputs_raise():
    with pytest.raises(ValueError, match="start equity"):
        compare_books(book(["1"]), book(["1"]), Decimal(0), Decimal(0), {})
    with pytest.raises(ValueError, match="no day"):
        compare_books([], [], START, Decimal(0), {})
