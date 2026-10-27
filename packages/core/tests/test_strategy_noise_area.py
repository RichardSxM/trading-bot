"""covers: spec 0020 AC-3 (``DayVwap`` and ``NoiseBands`` on hand worked 30 minute bars)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_core.strategy.noise_area import DayVwap, NoiseBands, utc_date

D = Decimal
HALF_HOUR_NS = 1_800_000_000_000
DAY_NS = 48 * HALF_HOUR_NS
DAY1 = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp()) * 10**9


def at(day, slot):
    """The open time of ``slot`` on day ``day`` (0 is 2026-02-01)."""
    return DAY1 + day * DAY_NS + slot * HALF_HOUR_NS


def test_utc_date_is_the_calendar_date_of_the_timestamp():
    assert utc_date(at(0, 47)) == date(2026, 2, 1)
    assert utc_date(at(1, 0)) == date(2026, 2, 2)
    assert utc_date(at(1, 0) - 1) == date(2026, 2, 1)


def test_the_vwap_weights_the_typical_price_by_volume_and_resets_at_utc_midnight():
    vwap = DayVwap()
    # typical (12 + 9 + 9) / 3 = 10, then (24 + 18 + 18) / 3 = 20 with 3 times the volume
    assert vwap.update(at(0, 46), D(12), D(9), D(9), D(1)) == D(10)
    assert vwap.update(at(0, 47), D(24), D(18), D(18), D(3)) == D("17.5")  # (10 + 60) / 4
    assert vwap.day == date(2026, 2, 1)
    assert vwap.update(at(1, 0), D(30), D(30), D(30), D(2)) == D(30)  # the new day starts afresh
    assert vwap.day == date(2026, 2, 2)


def test_a_zero_volume_bar_gives_no_vwap_until_the_day_has_volume_and_then_leaves_it_unchanged():
    vwap = DayVwap()
    assert vwap.update(at(0, 0), D(10), D(10), D(10), D(0)) is None
    assert vwap.update(at(0, 1), D(20), D(20), D(20), D(2)) == D(20)
    assert vwap.update(at(0, 2), D(50), D(50), D(50), D(0)) == D(20)


def _bands(lookback=2):
    return NoiseBands(lookback, HALF_HOUR_NS)


def _day(bands, day, open_, closes, first_slot=0):
    """Feed ``closes`` from ``first_slot`` on; the day opens at ``open_``. Returns each bar's sigma."""
    sigmas = []
    for i, close in enumerate(closes):
        slot = first_slot + i
        sigmas.append(bands.update(at(day, slot), D(open_) if i == 0 else D(closes[i - 1]), D(close)))
    return sigmas


def test_sigma_is_none_until_the_slot_has_lookback_days_of_earlier_moves():
    bands = _bands(lookback=2)
    assert _day(bands, 0, 100, [101, 102]) == [None, None]  # moves 0.01 and 0.02
    assert _day(bands, 1, 200, [203, 202]) == [None, None]  # moves 0.015 and 0.01
    # day 2 reads days 0 and 1: slot 0 (0.01 + 0.015) / 2, slot 1 (0.02 + 0.01) / 2
    assert _day(bands, 2, 50, [50, 50]) == [D("0.0125"), D("0.015")]
    assert bands.day_open == D(50)
    assert (bands.day, bands.slot) == (date(2026, 2, 3), 1)


def test_the_history_keeps_only_the_last_lookback_days():
    bands = _bands(lookback=2)
    _day(bands, 0, 100, [101])  # 0.01
    _day(bands, 1, 100, [103])  # 0.03
    _day(bands, 2, 100, [105])  # 0.05, read 0.02
    assert _day(bands, 3, 100, [100]) == [D("0.04")]  # (0.03 + 0.05) / 2: day 0 dropped


def test_a_bars_own_move_never_enters_its_own_sigma():
    bands = _bands(lookback=2)
    for day in (0, 1):
        _day(bands, day, 100, [101, 101, 101, 101, 101, 101])  # slot 5's move 0.01 each day
    today = _day(bands, 2, 100, [100, 100, 100, 100, 100, 200])  # a huge move at slot 5 today
    assert today[5] == D("0.01")
    tomorrow = _day(bands, 3, 100, [100, 100, 100, 100, 100, 100])
    assert tomorrow[5] == D("0.505")  # (0.01 + 1) / 2


def test_the_2330_bar_is_slot_47_of_the_old_day_and_the_0000_bar_starts_a_new_day_open():
    bands = _bands(lookback=1)
    bands.update(at(0, 0), D(100), D(100))
    bands.update(at(0, 47), D(100), D(110))
    assert (bands.day, bands.slot, bands.day_open) == (date(2026, 2, 1), 47, D(100))
    assert bands.update(at(1, 0), D(120), D(126)) == D(0)  # day 0 slot 0 moved 0
    assert (bands.day, bands.slot, bands.day_open) == (date(2026, 2, 2), 0, D(120))
    assert bands.update(at(1, 47), D(126), D(132)) == D("0.1")  # day 0 slot 47: 110 / 100 - 1


def test_a_day_with_no_0000_bar_has_no_open_and_adds_no_history_and_the_next_day_still_works():
    bands = _bands(lookback=1)
    _day(bands, 0, 100, [102, 104])  # 0.02, 0.04
    assert _day(bands, 1, 500, [600], first_slot=1) == [D("0.04")]  # sigma is still read
    assert bands.day_open is None
    # day 1 added nothing, so day 2's slot 1 still reads day 0's move
    assert _day(bands, 2, 100, [101, 101]) == [D("0.02"), D("0.04")]


def test_a_slot_missing_on_one_earlier_day_reads_the_days_that_had_it():
    bands = _bands(lookback=2)
    _day(bands, 0, 100, [101, 102])  # slot 1: 0.02
    _day(bands, 1, 100, [101])  # no slot 1 bar
    _day(bands, 2, 100, [101, 104])  # slot 1: 0.04
    assert _day(bands, 3, 100, [100, 100])[1] == D("0.03")


def test_a_zero_volume_bar_flat_at_the_previous_close_adds_its_move_like_any_bar():
    bands = _bands(lookback=1)
    _day(bands, 0, 100, [105, 105])  # the second bar is flat at the previous close: move 0.05 too
    assert _day(bands, 1, 100, [100, 100]) == [D("0.05"), D("0.05")]


@pytest.mark.parametrize(("lookback", "interval"), [(0, HALF_HOUR_NS), (1, 0), (1, 7 * 60 * 10**9)])
def test_bad_settings_are_refused(lookback, interval):
    with pytest.raises(ValueError, match=r"lookback_days|interval_ns"):
        NoiseBands(lookback, interval)
