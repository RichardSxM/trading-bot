"""The quote gap counter of quote_gaps.py (spec 0026 AC-19)."""

from quote_gaps import NS_PER_S, count_gaps, report

S = NS_PER_S
LIMIT = 10 * S


def test_a_gap_over_the_limit_fails():
    # First quote of any coin at 0 s, quotes at 0, 4, 15, 20 s, a 30 s run: gaps 0, 4, 11, 5, end 10.
    gaps = count_gaps([0, 4 * S, 15 * S, 20 * S], 0, 30 * S, LIMIT)
    assert gaps.quotes == 4
    assert gaps.longest_gap_ns == 11 * S
    assert gaps.gaps_over_limit == 1
    assert not gaps.passed


def test_a_gap_of_exactly_the_limit_passes():
    gaps = count_gaps([0, 10 * S, 20 * S], 0, 30 * S, LIMIT)
    assert gaps.longest_gap_ns == 10 * S
    assert gaps.gaps_over_limit == 0
    assert gaps.passed


def test_a_late_first_quote_fails_on_its_start_gap():
    gaps = count_gaps([12 * S, 20 * S, 28 * S], 0, 30 * S, LIMIT)
    assert gaps.gaps_over_limit == 1
    assert not gaps.passed


def test_an_end_gap_over_the_limit_fails():
    gaps = count_gaps([0, 5 * S], 0, 30 * S, LIMIT)
    assert gaps.longest_gap_ns == 25 * S
    assert not gaps.passed


def test_no_quotes_is_one_window_long_gap():
    gaps = count_gaps([], 0, 30 * S, LIMIT)
    assert gaps.quotes == 0
    assert gaps.longest_gap_ns == 30 * S
    assert gaps.gaps_over_limit == 1
    assert not gaps.passed


def test_the_report_starts_every_coin_at_the_first_quote_of_any_coin():
    t0 = 1_793_000_000 * S
    quotes = {"PF_XBTUSD.KRAKEN": [t0 + 5 * S, t0 + 12 * S], "PF_ETHUSD.KRAKEN": [t0 + 14 * S]}
    result = report(quotes, {"PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN"}, t0, t0 + 20 * S, 20)
    coins = result["coins"]
    assert isinstance(coins, dict)
    # The window starts at 5 s (BTC's first quote), not at the node start: BTC gaps 0, 7, 8.
    assert coins["PF_XBTUSD.KRAKEN"]["pass"] is True
    assert coins["PF_XBTUSD.KRAKEN"]["longest_gap_s"] == "8.000"
    # ETH starts 9 s after BTC and ends 6 s before the end: it passes.
    assert coins["PF_ETHUSD.KRAKEN"]["pass"] is True
    # SOL never quoted: one 15 s gap, a fail, and not loaded.
    assert coins["PF_SOLUSD.KRAKEN"] == {
        "loaded": False,
        "quotes": 0,
        "longest_gap_s": "15.000",
        "gaps_over_limit": 1,
        "pass": False,
    }
    assert result["passing"] == ["PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN"]
