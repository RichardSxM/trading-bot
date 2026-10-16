"""covers: spec 0004 AC-17, the num, pnl, and ts filters."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dashboard.ui.format import local_hm, num, pnl, ts


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("1234.50"), "1,234.50"),
        (Decimal("1E+3"), "1,000"),
        (Decimal("0.0150"), "0.0150"),
        (Decimal("1234567.8912"), "1,234,567.8912"),
        (Decimal("-42.10"), "\N{MINUS SIGN}42.10"),
        (Decimal("-0.00"), "0.00"),
        (Decimal("0"), "0"),
    ],
)
def test_num_keeps_the_decimals_own_precision_with_comma_grouping(value, text):
    assert num(value) == text


@pytest.mark.parametrize("value", [1.5, 2, True, "1.5", None])
def test_num_refuses_anything_but_a_decimal(value):
    with pytest.raises(TypeError, match="expected a Decimal"):
        num(value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("sNaN")])
def test_num_refuses_nan_and_infinity(value):
    with pytest.raises(ValueError, match="cannot show"):
        num(value)


def test_pnl_marks_profit_with_plus_up_arrow_and_class():
    assert pnl(Decimal("312.40")) == (
        '<span class="pnl pnl--profit"><span class="pnl__arrow" aria-hidden="true">▲</span>+312.40</span>'
    )


def test_pnl_marks_loss_with_minus_down_arrow_and_class():
    assert pnl(Decimal("-1234.5")) == (
        '<span class="pnl pnl--loss"><span class="pnl__arrow" aria-hidden="true">▼</span>\N{MINUS SIGN}1,234.5</span>'
    )


@pytest.mark.parametrize("value", [Decimal("0.00"), Decimal("-0.00")])
def test_pnl_zero_is_neutral_with_no_sign_or_arrow(value):
    assert pnl(value) == '<span class="pnl pnl--zero">0.00</span>'


def test_pnl_refuses_floats_and_nan():
    with pytest.raises(TypeError):
        pnl(1.0)
    with pytest.raises(ValueError, match="cannot show"):
        pnl(Decimal("NaN"))


def test_ts_renders_utc_to_the_second_with_amsterdam_summer_time_in_the_title():
    moment = datetime(2026, 10, 2, 14, 3, 7, 999_999, tzinfo=UTC)
    assert ts(moment) == (
        '<time datetime="2026-10-02T14:03:07Z" title="16:03:07 CEST, Amsterdam">2026-10-02 14:03:07Z</time>'
    )


def test_ts_uses_cet_in_winter_and_converts_other_offsets_to_utc():
    moment = datetime(2026, 1, 15, 4, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert ts(moment) == (
        '<time datetime="2026-01-15T09:30:00Z" title="10:30:00 CET, Amsterdam">2026-01-15 09:30:00Z</time>'
    )


def test_ts_takes_nanoseconds_and_truncates_them():
    ns = int(datetime(2026, 10, 2, 14, 3, 7, tzinfo=UTC).timestamp()) * 1_000_000_000 + 999_999_999
    assert ">2026-10-02 14:03:07Z</time>" in ts(ns)


def test_ts_refuses_a_naive_datetime():
    with pytest.raises(ValueError, match="naive datetime"):
        ts(datetime(2026, 10, 2, 14, 3, 7))  # noqa: DTZ001


@pytest.mark.parametrize("value", [True, 1.5, "2026-10-02", None])
def test_ts_refuses_other_types(value):
    with pytest.raises(TypeError):
        ts(value)


# --- gaps found against spec 0004 (/test pass)


@pytest.mark.parametrize("value", [2, True, "1.5", None])
def test_pnl_refuses_anything_but_a_decimal(value):
    """covers: AC-17"""
    with pytest.raises(TypeError, match="expected a Decimal"):
        pnl(value)


@pytest.mark.parametrize("value", [Decimal("Infinity"), Decimal("-Infinity")])
def test_pnl_refuses_infinity(value):
    """covers: AC-17"""
    with pytest.raises(ValueError, match="cannot show"):
        pnl(value)


def test_the_smallest_loss_is_a_loss_not_zero():
    """covers: AC-17"""
    assert pnl(Decimal("-0.01")) == (
        '<span class="pnl pnl--loss"><span class="pnl__arrow" aria-hidden="true">▼</span>\N{MINUS SIGN}0.01</span>'
    )


def test_ts_from_nanoseconds_carries_the_same_datetime_and_title():
    """covers: AC-17"""
    moment = datetime(2026, 10, 2, 14, 3, 7, tzinfo=UTC)
    ns = int(moment.timestamp()) * 1_000_000_000 + 999_999_999
    assert ts(ns) == ts(moment)


def test_ts_takes_a_time_before_the_epoch():
    """covers: AC-17 (floor, not truncation toward zero)"""
    ns = -1  # one nanosecond before 1970-01-01T00:00:00Z
    assert ">1969-12-31 23:59:59Z</time>" in ts(ns)


def test_ts_names_the_type_it_refused():
    """covers: AC-17"""
    with pytest.raises(TypeError, match="got bool"):
        ts(False)
    with pytest.raises(TypeError, match="got float"):
        ts(1.5)


# --- the mute badge's local time (spec 0013 AC-16)


@pytest.mark.parametrize(
    ("moment", "local"),
    [
        (datetime(2026, 10, 16, 22, 30, tzinfo=UTC), "00:30"),  # summer time, UTC+2
        (datetime(2027, 1, 15, 22, 30, tzinfo=UTC), "23:30"),  # winter time, UTC+1
    ],
)
def test_local_hm_shows_amsterdam_time_with_utc_in_the_title(moment, local):
    """covers: spec 0013 AC-16"""
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert local_hm(moment) == f'<time datetime="{stamp}" title="22:30:00Z UTC">{local}</time>'
