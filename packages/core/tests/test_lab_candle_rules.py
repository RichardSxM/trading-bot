"""covers: spec 0033 AC-9a (the tick history and dropped spans of ``fetch-candles``)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_core.lab.candles import (
    MINUTE_NS,
    OFF_TICK_BODY_MAX,
    DropSpan,
    FetchRules,
    FetchRulesError,
    Month,
    RawCandle,
    TickPeriod,
    build_candles,
    find_gaps,
    parse_fetch_rules,
    tick_at,
)
from trading_core.marketdata.hours import SECOND_NS

D = Decimal
AUG = Month(2026, 8)
NEW_TICK, OLD_TICK, STEP = D("0.00001"), D("0.000001"), D(1)
CHANGE_NS = int(datetime(2026, 8, 20, 7, tzinfo=UTC).timestamp()) * SECOND_NS
HISTORY = (TickPeriod(OLD_TICK, CHANGE_NS),)


def _at(open_ns, o, h, lo, c, v="1000"):
    return RawCandle(open_ns // 1_000_000, o, h, lo, c, v)


def _straddle():
    """Twelve candles on the old tick just before the change, twelve on the new tick from it."""
    old = [_at(CHANGE_NS - (12 - i) * MINUTE_NS, "0.123451", "0.123459", "0.123441", "0.123452") for i in range(12)]
    new = [_at(CHANGE_NS + i * MINUTE_NS, "0.12345", "0.12347", "0.12344", "0.12346") for i in range(12)]
    return old + new


def test_tick_at_uses_the_history_before_until_and_the_pin_from_it():
    assert tick_at(CHANGE_NS - MINUTE_NS, NEW_TICK, HISTORY) == OLD_TICK
    assert tick_at(CHANGE_NS, NEW_TICK, HISTORY) == NEW_TICK
    assert tick_at(CHANGE_NS - MINUTE_NS, NEW_TICK, ()) == NEW_TICK
    two = (TickPeriod(D("0.0000001"), CHANGE_NS - 60 * MINUTE_NS), *HISTORY)
    assert tick_at(CHANGE_NS - 61 * MINUTE_NS, NEW_TICK, two) == D("0.0000001")
    assert tick_at(CHANGE_NS - 60 * MINUTE_NS, NEW_TICK, two) == OLD_TICK


def test_a_month_straddling_the_change_builds_each_candle_on_its_own_tick():
    build = build_candles(_straddle(), AUG, NEW_TICK, STEP, ticks=HISTORY)
    assert build.bad == []
    assert len(build.rows) == 24
    assert build.rows[0].close == D("0.123452")
    assert build.rows[-1].close == D("0.12346")
    assert build.price_rounded_rows == 0
    assert build.history_ticks == {OLD_TICK}


def test_without_the_history_the_same_month_fails_the_off_tick_pre_pass():
    build = build_candles(_straddle(), AUG, NEW_TICK, STEP)
    assert len([b for b in build.bad if b[1] in ("open", "close")]) > OFF_TICK_BODY_MAX
    assert build.history_ticks == set()


def test_a_candle_off_the_old_tick_is_still_rounded_as_ac_2a_says():
    raw = _straddle()
    raw[3] = _at(CHANGE_NS - 9 * MINUTE_NS, "0.123451", "0.123459", "0.123441", "0.1234524")
    build = build_candles(raw, AUG, NEW_TICK, STEP, ticks=HISTORY)
    assert build.bad == []
    assert build.rows[3].close == D("0.123452")
    assert build.price_rounded_rows == 1


def test_a_dropped_span_becomes_one_gap_and_the_next_candle_is_checked_against_the_last_before_it():
    start = AUG.start_ns
    raw = [_at(start + m * MINUTE_NS, "100.00", "100.10", "99.90", "100.05") for m in range(10)]
    raw += [
        _at(start + m * MINUTE_NS, "92.56186715334", "92.56186715334", "92.56186715334", "92.56186715334", "0")
        for m in range(10, 29)
    ]
    raw.append(_at(start + 29 * MINUTE_NS, "100.05", "100.05", "100.05", "100.05", "0"))
    span = DropSpan(start + 10 * MINUTE_NS, start + 29 * MINUTE_NS, "glitch")
    tick = D("0.01")

    refused = build_candles(raw, AUG, tick, D("0.01"))
    assert refused.bad

    build = build_candles(raw, AUG, tick, D("0.01"), drop=(span,))
    assert build.bad == []
    assert build.dropped_rows == 19
    assert len(build.rows) == 11
    assert build.rows[-1].quiet
    missing, gaps = find_gaps(build.rows, start, start + 30 * MINUTE_NS)
    assert missing == 19
    assert [g.minutes for g in gaps] == [19]


def test_the_quiet_minute_after_a_span_must_be_flat_at_the_close_before_it():
    start = AUG.start_ns
    raw = [_at(start, "100.00", "100.10", "99.90", "100.05")]
    raw.append(_at(start + 6 * MINUTE_NS, "92.56", "92.56", "92.56", "92.56", "0"))
    span = DropSpan(start + MINUTE_NS, start + 6 * MINUTE_NS, "glitch")
    build = build_candles(raw, AUG, D("0.01"), D("0.01"), drop=(span,))
    assert [b[1] for b in build.bad] == ["quiet"]


def test_no_rules_build_the_same_rows_as_before():
    raw = [_at(AUG.start_ns + m * MINUTE_NS, "0.12345", "0.12347", "0.12344", "0.12346") for m in range(30)]
    assert build_candles(raw, AUG, NEW_TICK, STEP) == build_candles(raw, AUG, NEW_TICK, STEP, ticks=(), drop=())


def test_parse_reads_the_pin_keys():
    rules = parse_fetch_rules(
        [{"price_increment": "0.000001", "until": "2026-08-20T07:00:00Z"}],
        [{"from": "2023-08-17T21:44:00Z", "until": "2023-08-17T22:03:00Z", "why": "glitch"}],
    )
    assert rules.ticks == HISTORY
    span = rules.drop[0]
    assert span.until_ns - span.from_ns == 19 * MINUTE_NS
    assert parse_fetch_rules(None, None) == FetchRules()


@pytest.mark.parametrize(
    ("history", "spans", "match"),
    [
        ([{"price_increment": "0", "until": "2026-08-20T07:00:00Z"}], None, "above 0"),
        ([{"until": "2026-08-20T07:00:00Z"}], None, "price_increment"),
        (
            [
                {"price_increment": "0.000001", "until": "2026-08-20T07:00:00Z"},
                {"price_increment": "0.0000001", "until": "2026-08-01T00:00:00Z"},
            ],
            None,
            "time order",
        ),
        ([{"price_increment": "0.000001", "until": "2026-08-20T07:00:30Z"}], None, "whole minute"),
        ([{"price_increment": "0.000001", "until": "2026-08-20T07:00:00+00:00"}], None, "ending in Z"),
        (None, [{"from": "2023-08-17T22:03:00Z", "until": "2023-08-17T22:03:00Z", "why": "x"}], "after from"),
        (None, [{"from": "2023-08-17T21:44:00Z", "until": "2023-08-17T22:03:00Z"}], "why"),
        ("not a list", None, "list of objects"),
    ],
)
def test_parse_refuses_a_malformed_entry(history, spans, match):
    with pytest.raises(FetchRulesError, match=match):
        parse_fetch_rules(history, spans)
