"""covers: spec 0024 AC-1 (the form's defaults), AC-2 (Amsterdam dates to UTC bounds, daylight saving, refusals),
AC-14 (one chunk per Amsterdam month)."""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from trading_core.journal.bounds import (
    JournalRangeError,
    amsterdam_today,
    default_dates,
    default_mode,
    month_chunks,
    parse_range,
)
from trading_core.recording.enums import RunMode

TODAY = date(2026, 10, 30)


def test_a_winter_range_starts_and_ends_at_amsterdam_midnight_in_utc():
    journal_range = parse_range("2026-01-01", "2026-01-31", "paper", TODAY)

    assert journal_range.start_utc == datetime(2025, 12, 31, 23, tzinfo=UTC)
    assert journal_range.end_utc == datetime(2026, 1, 31, 23, tzinfo=UTC)
    assert journal_range.mode is RunMode.PAPER


def test_the_last_sunday_of_october_lasts_25_hours():
    journal_range = parse_range("2026-10-25", "2026-10-25", "live", TODAY)

    assert journal_range.start_utc == datetime(2026, 10, 24, 22, tzinfo=UTC)  # CEST, UTC+2
    assert journal_range.end_utc == datetime(2026, 10, 25, 23, tzinfo=UTC)  # CET, UTC+1
    assert journal_range.end_utc - journal_range.start_utc == timedelta(hours=25)


def test_the_last_sunday_of_march_lasts_23_hours():
    journal_range = parse_range("2026-03-29", "2026-03-29", "paper", TODAY)

    assert journal_range.end_utc - journal_range.start_utc == timedelta(hours=23)


def test_a_fill_at_00_30_amsterdam_on_1_january_belongs_to_the_new_year():
    fill_at = datetime(2025, 12, 31, 23, 30, tzinfo=UTC)  # 00:30 on 1 January in Amsterdam
    new_year = parse_range("2026-01-01", "2026-01-01", "paper", TODAY)
    old_year = parse_range("2025-12-31", "2025-12-31", "paper", TODAY)

    assert new_year.start_utc <= fill_at < new_year.end_utc
    assert not old_year.start_utc <= fill_at < old_year.end_utc


@pytest.mark.parametrize(
    ("start", "end", "mode", "message"),
    [
        ("2026-13-01", "2026-10-01", "paper", "does not exist"),
        ("2026-02-30", "2026-10-01", "paper", "does not exist"),
        ("2026-1-1", "2026-10-01", "paper", "YYYY-MM-DD"),
        ("20260101", "2026-10-01", "paper", "YYYY-MM-DD"),
        (None, "2026-10-01", "paper", "start date"),
        ("2026-01-01", "", "paper", "end date"),
        ("2026-01-01", "2026-10-01", "both", "live or paper"),
        ("2026-01-01", "2026-10-01", None, "live or paper"),
        ("2026-01-01", "2026-10-01", "backtest", "live or paper"),
        ("2026-10-02", "2026-10-01", "paper", "after the end"),
        ("2026-10-01", "2026-10-30", "paper", "yesterday or earlier"),
        ("2026-10-01", "2026-11-05", "paper", "yesterday or earlier"),
    ],
)
def test_a_bad_request_is_refused_as_bad_range(start, end, mode, message):
    with pytest.raises(JournalRangeError, match=message) as raised:
        parse_range(start, end, mode, TODAY)

    assert raised.value.reason == "bad_range"


def test_yesterday_is_the_latest_end():
    assert parse_range("2026-10-29", "2026-10-29", "paper", TODAY).end == date(2026, 10, 29)


def test_today_is_the_amsterdam_date():
    assert amsterdam_today(datetime(2026, 10, 29, 23, 30, tzinfo=UTC)) == date(2026, 10, 30)
    with pytest.raises(ValueError, match="naive"):
        amsterdam_today(datetime(2026, 10, 29, 23, 30))  # noqa: DTZ001


def test_the_defaults_are_1_january_to_yesterday_in_yesterdays_year():
    assert default_dates(TODAY) == (date(2026, 1, 1), date(2026, 10, 29))
    assert default_dates(date(2027, 1, 1)) == (date(2026, 1, 1), date(2026, 12, 31))


def test_the_default_mode_follows_the_env():
    assert default_mode("live") is RunMode.LIVE
    assert default_mode("demo") is RunMode.PAPER


def test_chunks_cut_the_range_at_each_amsterdam_month_start():
    journal_range = parse_range("2026-01-15", "2026-03-10", "paper", TODAY)

    chunks = month_chunks(journal_range)

    assert chunks == [
        (journal_range.start_utc, datetime(2026, 1, 31, 23, tzinfo=UTC)),
        (datetime(2026, 1, 31, 23, tzinfo=UTC), datetime(2026, 2, 28, 23, tzinfo=UTC)),
        (datetime(2026, 2, 28, 23, tzinfo=UTC), journal_range.end_utc),
    ]


def test_a_year_is_twelve_chunks_that_tile_it_with_no_gap():
    journal_range = parse_range("2025-01-01", "2025-12-31", "paper", TODAY)

    chunks = month_chunks(journal_range)

    assert len(chunks) == 12
    assert chunks[0][0] == journal_range.start_utc
    assert chunks[-1][1] == journal_range.end_utc
    assert all(a[1] == b[0] for a, b in pairwise(chunks))


def test_a_one_day_range_is_one_chunk():
    journal_range = parse_range("2026-10-01", "2026-10-01", "paper", TODAY)

    assert month_chunks(journal_range) == [(journal_range.start_utc, journal_range.end_utc)]
