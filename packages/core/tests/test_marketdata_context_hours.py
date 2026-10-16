"""covers: spec 0013 AC-7 (a closed hour missing a context file) against spec 0009 AC-17's
expectation that every closed hour has its steady context files.

Pure: no clock, no I/O.
"""

from trading_core.marketdata.context_hours import MissingContextHour, missing_context_hours

H = 3_600_000_000_000
T0 = 1_792_033_200_000_000_000  # 2026-10-15T03:00Z
TABLES = ("mark", "open_interest")


def hours(*offsets):
    return {T0 + i * H for i in offsets}


def check(book, context, skip=None, checked_to=T0 - H):
    return missing_context_hours(book_hours=book, context_hours=context, skip=skip or {}, checked_to=checked_to)


def test_an_hour_is_checked_only_once_a_later_book_hour_is_finished():
    missing, checked = check(hours(0, 1), {"mark": hours(0), "open_interest": hours(0)})

    assert missing == []
    assert checked == T0  # hour 1 waits for hour 2


def test_a_missing_file_is_reported_per_table():
    missing, checked = check(hours(0, 1, 2), {"mark": hours(0, 1, 2), "open_interest": hours(0)})

    assert missing == [MissingContextHour(T0 + H, "open_interest")]
    assert checked == T0 + H


def test_hours_already_checked_are_not_reported_again():
    context = {"mark": hours(0, 2), "open_interest": hours(0, 2)}
    first, checked = check(hours(0, 1, 2, 3), context)
    again, checked_again = check(hours(0, 1, 2, 3), context, checked_to=checked)

    assert [m.hour_ns for m in first] == [T0 + H, T0 + H]
    assert again == []
    assert checked_again == checked


def test_excused_hours_are_skipped():
    context = {"mark": hours(0), "open_interest": hours(0)}

    missing, _ = check(hours(0, 1, 2), context, skip={"mark": hours(1), "open_interest": hours(1)})

    assert missing == []


def test_an_outage_excuses_only_the_tables_it_covers():
    context = {"mark": hours(0), "open_interest": hours(0)}

    missing, _ = check(hours(0, 1, 2), context, skip={"open_interest": hours(1)})

    assert missing == [MissingContextHour(T0 + H, "mark")]


def test_the_first_check_reports_nothing_and_marks_old_hours_checked():
    missing, checked = check(hours(0, 1, 2, 3), {"mark": set(), "open_interest": set()}, checked_to=None)

    assert missing == []
    assert checked == T0 + 2 * H


def test_no_book_hours_changes_nothing():
    assert check(set(), {"mark": set()}) == ([], T0 - H)


def test_the_label_is_the_utc_hour():
    assert MissingContextHour(T0, "mark").label == "2026-10-15T03:00Z"
