"""covers: spec 0014 AC-21 (candle parity: enter decisions matched by side within 60 s, approved or refused)."""

from decimal import Decimal

from trading_core.lab.candle_parity import MATCH_NS, match_enters
from trading_core.lab.parity import Decided

T0 = 1_791_446_400_000_000_000  # 2026-10-08 08:00 UTC
MIN = 60_000_000_000


def _d(minute, side="buy", intent="enter", approved=True, offset=0):
    return Decided(T0 + minute * MIN + offset, intent, side, approved, None if approved else "position_open")


def test_enters_with_the_same_side_within_60_s_match_approved_or_refused():
    book = [_d(1), _d(5, "sell", approved=False), _d(9)]
    candles = [_d(1, offset=MATCH_NS), _d(5, "sell", approved=True, offset=-30 * 10**9), _d(9)]
    result = match_enters(book, candles)
    assert (result.matches, result.book, result.candles) == (3, 3, 3)
    assert result.ratio == 1
    assert result.passed


def test_a_side_mismatch_or_a_gap_over_60_s_is_unmatched_on_both_sides():
    result = match_enters([_d(1), _d(5)], [_d(1, "sell"), _d(5, offset=MATCH_NS + 1)])
    assert result.matches == 0
    assert result.unmatched_book == (_d(1), _d(5))
    assert result.unmatched_candles == (_d(1, "sell"), _d(5, offset=MATCH_NS + 1))
    assert result.ratio == 0


def test_only_enter_decisions_count():
    result = match_enters([_d(1), _d(2, "sell", intent="exit")], [_d(1), _d(3, "sell", intent="exit")])
    assert (result.matches, result.book, result.candles) == (1, 1, 1)


def test_each_decision_matches_once_earliest_first():
    result = match_enters([_d(1), _d(1, offset=10**9)], [_d(1, offset=5 * 10**8)])
    assert result.matches == 1
    assert result.unmatched_book == (_d(1, offset=10**9),)
    assert result.ratio == Decimal("0.5")


def test_the_ratio_divides_by_the_larger_side_and_passes_at_95_percent():
    book = [_d(m) for m in range(0, 200, 10)]  # 20
    assert match_enters(book, book[:19]).ratio == Decimal("0.95")
    assert match_enters(book, book[:19]).passed
    assert not match_enters(book, book[:18]).passed


def test_no_decision_on_either_side_has_no_ratio():
    result = match_enters([_d(1, intent="exit")], [])
    assert result.ratio is None
    assert not result.passed
