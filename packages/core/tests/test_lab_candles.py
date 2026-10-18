"""covers: spec 0014 AC-1, AC-2, AC-2a, AC-3, AC-4 (months, candle rows, funding periods, the merge)."""

from decimal import Decimal

import pytest

from trading_core.lab.candles import (
    FUNDING_CARRY_MAX_NS,
    FUNDING_HOURLY_FROM_NS,
    MINUTE_NS,
    FundingRow,
    Month,
    MonthManifest,
    RawCandle,
    build_candles,
    carried_rate,
    charge_time_ns,
    expected_charge_times,
    fetch_months,
    find_gaps,
    is_complete,
    merge_funding,
    missing_charges,
    quiet_prefix_problems,
    round_price,
    rows_by_month,
    snap_volume,
)
from trading_core.lab.errors import CandleInputError, OutdatedManifestError
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
SEP = Month(2026, 9)
NOW = Month(2026, 10).start_ns + 17 * 24 * HOUR_NS  # 2026-10-18
TICK, STEP = D(1), D("0.0001")


def _raw(minute, o="100", h="101", lo="99", c="100", v="1.5"):
    return RawCandle((SEP.start_ns + minute * MINUTE_NS) // 1_000_000, o, h, lo, c, v)


def test_month_bounds_and_parsing():
    assert str(Month.parse("2026-09")) == "2026-09"
    assert Month(2026, 12).next() == Month(2027, 1)
    assert SEP.end_ns - SEP.start_ns == 30 * 24 * HOUR_NS
    assert Month.of_ns(SEP.end_ns - 1) == SEP
    for bad in ("2026-9", "2026-13", "202609", "x"):
        with pytest.raises(CandleInputError):
            Month.parse(bad)


def test_fetch_months_are_inclusive_and_bounded():
    assert fetch_months("2026-08", "2026-10", NOW) == [Month(2026, 8), SEP, Month(2026, 10)]
    with pytest.raises(CandleInputError, match="before 2022-03"):
        fetch_months("2022-02", "2022-04", NOW)
    with pytest.raises(CandleInputError, match="after the current month"):
        fetch_months("2026-10", "2026-11", NOW)
    with pytest.raises(CandleInputError, match="is after"):
        fetch_months("2026-09", "2026-08", NOW)


def test_a_month_is_complete_once_it_ended_before_the_fetch():
    assert is_complete(SEP, NOW)
    assert not is_complete(Month(2026, 10), NOW)
    assert is_complete(SEP, SEP.end_ns)


def test_charge_time_is_stamp_plus_the_period():
    four_h_stamp = FUNDING_HOURLY_FROM_NS - 4 * HOUR_NS
    assert charge_time_ns(four_h_stamp) == FUNDING_HOURLY_FROM_NS
    assert charge_time_ns(FUNDING_HOURLY_FROM_NS) == FUNDING_HOURLY_FROM_NS + HOUR_NS
    # A stamp of 2022-04-30 20:00 is charged on 2022-05-01 00:00, so it files under May.
    stamp = Month(2022, 5).start_ns - 4 * HOUR_NS
    assert Month.of_ns(FundingRow.from_stamp(stamp, D("0.0001"), "export").charge_ns) == Month(2022, 5)


def test_expected_charge_times_switch_from_four_hourly_to_hourly():
    start = FUNDING_HOURLY_FROM_NS - 12 * HOUR_NS  # 2022-09-29 00:00
    times = expected_charge_times(start, FUNDING_HOURLY_FROM_NS + 3 * HOUR_NS)
    offsets = [(t - start) // HOUR_NS for t in times]
    assert offsets == [4, 8, 12, 13, 14]
    assert expected_charge_times(start, start + HOUR_NS) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.3565999999999999", D("0.3566")), ("1.5", D("1.5000")), ("0", D("0")), ("0.00000000001", D("0"))],
)
def test_noise_volumes_snap_to_the_step(text, expected):
    assert snap_volume(D(text), STEP) == expected


def test_a_volume_further_off_than_the_noise_is_bad():
    assert snap_volume(D("0.356601"), STEP) is None


def test_build_candles_rounds_flags_quiet_and_dedupes():
    raw = [
        _raw(0, v="0.3565999999999999"),
        _raw(1, o="100", h="100", lo="100", c="100", v="0"),
        _raw(1, o="100", h="100", lo="100", c="100", v="0"),  # identical page edge repeat
        _raw(2, o="100", h="102", lo="100", c="101", v="0.000000001"),  # rounds to 0, not flat
        RawCandle(SEP.end_ns // 1_000_000, "1", "1", "1", "1", "1"),  # next month: dropped
    ]
    build = build_candles(raw, SEP, TICK, STEP)
    assert build.bad == []
    assert [(r.ts_ns - SEP.start_ns) // MINUTE_NS for r in build.rows] == [1, 2, 3]
    assert [r.quiet for r in build.rows] == [False, True, False]
    assert build.rows[0].volume == D("0.3566")
    assert (build.volume_rounded_rows, build.zero_volume_trade_rows) == (2, 1)


def test_build_candles_lists_bad_rows():
    raw = [_raw(0, o="100.5"), _raw(1, v="0.12345"), _raw(2), _raw(2, c="99")]
    bad = build_candles(raw, SEP, TICK, STEP).bad
    assert [field for _, field, _ in bad] == ["open", "volume", "duplicate"]


@pytest.mark.parametrize(
    ("text", "name", "expected"),
    [
        ("25920.8322615", "high", D(25920)),
        ("28319.01027283563", "low", D(28320)),
        ("39522.99999999999", "high", D(39523)),
        ("42938.99999999999", "low", D(42939)),
        ("100.00000001", "open", D(100)),
        ("100.0000001", "open", None),
        ("100.5", "close", None),
        ("101", "high", D(101)),
    ],
)
def test_prices_snap_noise_and_round_extremes_toward_the_body(text, name, expected):
    assert round_price(D(text), TICK, name) == expected


def test_off_tick_extremes_are_stored_rounded_and_counted_once():
    raw = [
        _raw(0, h="101.7", lo="98.2"),  # two real roundings, one candle
        _raw(1, h="100.99999999999"),  # a noise snap only: counted, not listed
        _raw(2, h="99.99999999999", c="100"),  # high snaps to the close, not a bad row
        _raw(3),
    ]
    build = build_candles(raw, SEP, TICK, STEP)
    assert build.bad == []
    assert [(r.high, r.low) for r in build.rows[:3]] == [(D(101), D(99)), (D(101), D(99)), (D(100), D(99))]
    assert build.price_rounded_rows == 3
    assert [(r.field, r.raw, r.stored) for r in build.price_roundings] == [
        ("high", "101.7", D(101)),
        ("low", "98.2", D(99)),
    ]


def test_a_bad_row_is_never_counted_as_rounded():
    build = build_candles([_raw(0, o="100.5", h="101.7")], SEP, TICK, STEP)
    assert [field for _, field, _ in build.bad] == ["open"]
    assert (build.price_rounded_rows, build.price_roundings, build.rows) == (0, [], [])


def test_page_edge_copies_that_differ_only_in_noise_are_one_row():
    raw = [_raw(0, h="101"), _raw(0, h="100.99999999999"), _raw(1)]
    build = build_candles(raw, SEP, TICK, STEP)
    assert build.bad == []
    assert len(build.rows) == 2


def test_a_volume_0_candle_that_rounds_flat_is_quiet():
    raw = [_raw(0, c="100"), _raw(1, o="100", h="100.000000001", lo="99.99999999999", c="100", v="0")]
    build = build_candles(raw, SEP, TICK, STEP)
    assert build.bad == []
    assert build.rows[1].quiet
    assert build.price_rounded_rows == 1


def test_off_minute_times_and_inconsistent_extremes_are_bad_rows():
    off_minute = RawCandle(SEP.start_ns // 1_000_000 + 30_000, "100", "101", "99", "100", "1")
    raw = [off_minute, _raw(1, h="99"), _raw(2, lo="101"), _raw(3, o="98", h="99", lo="97", c="100")]
    bad = build_candles(raw, SEP, TICK, STEP).bad
    assert [field for _, field, _ in bad] == ["time", "high", "low", "high"]


def _quiet(minute, price):
    return _raw(minute, o=price, h=price, lo=price, c=price, v="0")


def test_a_quiet_minute_must_be_flat_at_the_previous_close():
    raw = [_raw(0, c="100"), _quiet(1, "100"), _quiet(3, "100"), _quiet(4, "101")]
    build = build_candles(raw, SEP, TICK, STEP)
    assert [(ts, field) for ts, field, _ in build.bad] == [(str(SEP.start_ns + 4 * MINUTE_NS), "quiet")]
    assert not build.quiet_unchecked


def test_the_first_quiet_rows_are_checked_against_the_previous_month():
    raw = [_quiet(0, "100"), _raw(1)]
    assert build_candles(raw, SEP, TICK, STEP, prev_close=D(100)).bad == []
    assert [f for _, f, _ in build_candles(raw, SEP, TICK, STEP, prev_close=D(99)).bad] == ["quiet"]
    unchecked = build_candles(raw, SEP, TICK, STEP)
    assert unchecked.bad == []
    assert unchecked.quiet_unchecked
    assert not build_candles([_raw(0), _quiet(1, "100")], SEP, TICK, STEP).quiet_unchecked


def test_the_quiet_prefix_check_stops_at_the_first_trade_minute():
    rows = build_candles(
        [_quiet(0, "100"), _quiet(1, "100"), _raw(2, lo="97", c="97"), _quiet(3, "97")], SEP, TICK, STEP
    ).rows
    assert quiet_prefix_problems(rows, D(100)) == []
    assert [ts for ts, _, _ in quiet_prefix_problems(rows, D(99))] == [
        str(SEP.start_ns + m * MINUTE_NS) for m in (0, 1)
    ]


def test_month_previous_wraps_the_year():
    assert Month(2026, 1).previous() == Month(2025, 12)
    assert Month(2026, 9).previous() == Month(2026, 8)


def test_find_gaps_counts_missing_minutes_and_runs_of_five():
    rows = build_candles([_raw(m) for m in (0, 1, 3, 9, 10)], SEP, TICK, STEP).rows
    missing, gaps = find_gaps(rows, SEP.start_ns, SEP.start_ns + 12 * MINUTE_NS)
    assert missing == 1 + 5 + 1
    assert [(g.start_ns - SEP.start_ns) // MINUTE_NS for g in gaps] == [4]
    assert (gaps[0].minutes, gaps[0].end_ns) == (5, SEP.start_ns + 9 * MINUTE_NS)


def test_merge_adds_keeps_and_flags_disagreements():
    t = SEP.start_ns
    stored = [FundingRow(t, D("0.0001"), HOUR_NS, "export")]
    new = [
        FundingRow(t, D("0.0001"), HOUR_NS, "api"),
        FundingRow(t + HOUR_NS, D("0.0002"), HOUR_NS, "api"),
    ]
    merged = merge_funding(stored, new)
    assert (merged.added, merged.present, merged.conflicts) == (1, 1, [])
    assert [r.source for r in merged.rows] == ["export", "api"]
    again = merge_funding(merged.rows, new)
    assert (again.added, again.present) == (0, 2)
    clash = merge_funding(stored, [FundingRow(t, D("0.0003"), HOUR_NS, "api")])
    assert clash.conflicts == [(str(t), "rate", "stored 0.0001, api 0.0003")]


def test_rows_by_month_and_missing_charges():
    t = SEP.end_ns - HOUR_NS
    rows = [FundingRow(t, D("0"), HOUR_NS, "api"), FundingRow(SEP.end_ns, D("0"), HOUR_NS, "api")]
    assert {str(m): len(r) for m, r in rows_by_month(rows).items()} == {"2026-09": 1, "2026-10": 1}
    assert missing_charges(rows[:1], t - 2 * HOUR_NS, t) == 2
    assert missing_charges([], None, None) == 0


def test_carried_rate_is_the_latest_row_inside_the_carry_limit():
    t = SEP.start_ns
    rows = [FundingRow(t, D("0.0001"), HOUR_NS, "api")]
    assert carried_rate(rows, t) == rows[0]
    assert carried_rate(rows, t + FUNDING_CARRY_MAX_NS) == rows[0]
    assert carried_rate(rows, t + FUNDING_CARRY_MAX_NS + 1) is None
    assert carried_rate(rows, t - 1) is None


def test_manifest_funding_fields_are_recomputed():
    t = SEP.start_ns
    first, last = t + MINUTE_NS, t + 2 * HOUR_NS
    manifest = MonthManifest("2026-09", 2, 0, 0, 0, 0, 0, [], first, last, 0, 0, 0, True, "2026-10-01T00:00:00Z")
    rows = [FundingRow(t + HOUR_NS, D("0"), HOUR_NS, "export"), FundingRow(t + 2 * HOUR_NS, D("0"), HOUR_NS, "api")]
    updated = manifest.with_funding(rows)
    assert (updated.funding_rows, updated.funding_sources, updated.missing_funding_charges) == (2, ["api", "export"], 0)
    assert updated.complete
    assert updated.fetched_at == manifest.fetched_at


def test_the_charge_at_the_month_end_belongs_to_the_next_month():
    hourly = [FundingRow(SEP.start_ns + h * HOUR_NS, D("0"), HOUR_NS, "api") for h in range(1, 720)]
    manifest = MonthManifest(
        "2026-09", 0, 0, 0, 0, 0, 0, [], SEP.start_ns + MINUTE_NS, SEP.end_ns, 0, 0, 0, True, "2026-10-01T00:00:00Z"
    )
    assert manifest.with_funding(hourly).missing_funding_charges == 0


def test_a_manifest_without_a_later_field_is_outdated_and_other_gaps_are_input_errors():
    manifest = MonthManifest("2026-09", 2, 0, 0, 0, 0, 0, [], 1, 2, 0, 0, 0, True, "2026-10-01T00:00:00Z")
    data = vars(manifest).copy()
    assert MonthManifest.from_json(data) == manifest
    with pytest.raises(OutdatedManifestError):
        MonthManifest.from_json({k: v for k, v in data.items() if k != "price_rounded_rows"})
    with pytest.raises(CandleInputError, match="missing") as caught:
        MonthManifest.from_json({k: v for k, v in data.items() if k != "bar_rows"})
    assert not isinstance(caught.value, OutdatedManifestError)
    with pytest.raises(CandleInputError, match="unknown"):
        MonthManifest.from_json({**data, "extra": 1})
