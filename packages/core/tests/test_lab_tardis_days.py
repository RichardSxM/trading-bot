"""covers: spec 0008 AC-1 (free first of month days only) and AC-2 (the catalog cap)."""

from datetime import date
from decimal import Decimal

import pytest

from trading_core.lab.tardis_days import fits_under_cap, import_days
from trading_core.marketdata.hours import GB


def test_a_range_between_two_firsts_gives_every_first_in_it():
    assert import_days(date(2026, 8, 1), date(2026, 10, 1)) == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert import_days(date(2026, 9, 1), date(2026, 9, 1)) == [date(2026, 9, 1)]


def test_an_end_that_is_not_a_first_is_refused():
    with pytest.raises(ValueError, match="first of each month only: 2026-09-02"):
        import_days(date(2026, 9, 2), date(2026, 9, 2))
    with pytest.raises(ValueError, match="2026-09-15"):
        import_days(date(2026, 9, 1), date(2026, 9, 15))


def test_from_after_to_is_refused():
    with pytest.raises(ValueError, match="after"):
        import_days(date(2026, 10, 1), date(2026, 9, 1))


def test_the_cap_holds_the_next_day_too():
    assert fits_under_cap(37 * GB, Decimal(3), Decimal(40))
    assert not fits_under_cap(37 * GB + 1, Decimal(3), Decimal(40))
    assert not fits_under_cap(0, Decimal(3), Decimal(1))
