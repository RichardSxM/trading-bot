"""covers: spec 0014 AC-19 (the look ahead check: where the cuts fall, and how a cut run must match)."""

import pytest

from trading_core.lab.candles import MINUTE_NS
from trading_core.lab.lookahead import DecidedKey, cut_times, first_mismatch
from trading_core.marketdata.hours import HOUR_NS

T0 = 1_788_220_800_000_000_000  # 2026-09-01 00:00 UTC
DAY = 24 * HOUR_NS


def _key(hour, intent="enter", side="buy", reason="macd_entry", verdict="approved"):
    return DecidedKey(T0 + hour * HOUR_NS, intent, side, reason, verdict)


def test_the_cuts_are_whole_minutes_off_the_hour_in_the_middle_80_percent_and_the_same_for_a_seed():
    cuts = cut_times(7, T0, T0 + 30 * DAY, 3)
    assert cuts == cut_times(7, T0, T0 + 30 * DAY, 3)
    assert cuts != cut_times(8, T0, T0 + 30 * DAY, 3)
    assert cuts == sorted(set(cuts))
    assert len(cuts) == 3
    assert all(c % MINUTE_NS == 0 and c % HOUR_NS != 0 for c in cuts)
    assert all(T0 + 3 * DAY <= c <= T0 + 27 * DAY for c in cuts)


def test_every_middle_hour_and_every_minute_offset_can_be_drawn():
    drawn = {c for seed in range(4000) for c in cut_times(seed, T0, T0 + DAY, 1)}
    # 24 h: 2.4 h left out at each end (02:24 to 21:36), so hours 03:00 to 20:00, each plus 1 to 59 minutes.
    assert {c // HOUR_NS * HOUR_NS for c in drawn} == {T0 + h * HOUR_NS for h in range(3, 21)}
    assert {c % HOUR_NS // MINUTE_NS for c in drawn} == set(range(1, 60))


def test_a_range_too_short_for_the_cuts_is_refused():
    with pytest.raises(ValueError, match="fewer than"):
        cut_times(7, T0, T0 + 2 * HOUR_NS, 3)


# covers: spec 0019 AC-2 (cut times between two bar closes for any bar interval)
def test_the_hour_interval_draws_what_the_hour_only_cuts_drew():
    # Pinned from the hour only cut_times before spec 0019, so existing look ahead runs are unchanged.
    assert cut_times(7, T0, T0 + 30 * DAY, 3) == [1789034640000000000, 1789674120000000000, 1789934700000000000]
    assert cut_times(42, T0, T0 + 300 * DAY, 5, HOUR_NS) == [
        1791550080000000000,
        1794096540000000000,
        1798037040000000000,
        1798924020000000000,
        1809670500000000000,
    ]


def test_fifteen_minute_cuts_fall_1_to_14_minutes_past_a_quarter_hour():
    quarter = 15 * MINUTE_NS
    drawn = {c for seed in range(2000) for c in cut_times(seed, T0, T0 + DAY, 3, quarter)}
    assert all(c % MINUTE_NS == 0 and c % quarter != 0 for c in drawn)
    assert {c % quarter // MINUTE_NS for c in drawn} == set(range(1, 15))
    assert all(T0 + DAY // 10 <= c <= T0 + DAY - DAY // 10 for c in drawn)


@pytest.mark.parametrize("interval", [MINUTE_NS, 0, 90 * 10**9])
def test_an_interval_with_no_minute_strictly_between_closes_is_refused(interval):
    with pytest.raises(ValueError, match="whole minutes"):
        cut_times(7, T0, T0 + 30 * DAY, 3, interval)


def test_equal_decisions_before_the_cut_match_whatever_follows_it():
    full = [_key(1), _key(5, "exit", "sell", "macd_exit"), _key(9)]
    cut = [_key(1), _key(5, "exit", "sell", "macd_exit"), _key(6, reason="range_end")]
    assert first_mismatch(full, cut, T0 + 6 * HOUR_NS) is None


def test_a_decision_at_the_cut_itself_is_not_compared():
    assert first_mismatch([_key(1), _key(6)], [_key(1)], T0 + 6 * HOUR_NS) is None


def test_a_decision_the_cut_run_lacks_is_the_mismatch():
    found = first_mismatch([_key(1), _key(3)], [_key(1)], T0 + 6 * HOUR_NS)
    assert found is not None
    assert (found.full, found.cut) == (_key(3), None)


def test_an_extra_decision_in_the_cut_run_is_the_mismatch():
    found = first_mismatch([_key(1)], [_key(1), _key(2, side="sell")], T0 + 6 * HOUR_NS)
    assert found is not None
    assert (found.full, found.cut) == (None, _key(2, side="sell"))


@pytest.mark.parametrize(
    "changed",
    [
        _key(2, side="sell"),
        _key(2, verdict="rejected"),
        _key(2, reason="other"),
        _key(2, intent="exit"),
        _key(2, side=None),
    ],
)
def test_any_changed_field_is_a_mismatch(changed):
    found = first_mismatch([_key(1), _key(2)], [_key(1), changed], T0 + 6 * HOUR_NS)
    assert found is not None
    assert found.cut_ns == T0 + 6 * HOUR_NS


def test_a_moved_decision_is_a_mismatch():
    moved = DecidedKey(T0 + 2 * HOUR_NS + 1, "enter", "buy", "macd_entry", "approved")
    found = first_mismatch([_key(2)], [moved], T0 + 6 * HOUR_NS)
    assert found is not None
