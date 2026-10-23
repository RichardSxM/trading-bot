"""covers: spec 0017 AC-2 (the audit's per second samples, moves, order flow, funding, reachable, nearest rank)."""

import json
from datetime import date
from decimal import Decimal

import pytest

from trading_core.lab.audit import (
    MINUTE_NS,
    SECOND_NS,
    DayAudit,
    Rate,
    Samples,
    SegmentAudit,
    Span,
    audit_json,
    mean_abs_funding,
    nearest_rank,
    reachable,
    scaled,
)
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
# 2022-04-01 00:00 UTC.
T0 = 1_648_771_200_000_000_000


def test_nearest_rank_takes_the_ceiling_rank():
    values = [15, 20, 35, 40, 50]

    assert nearest_rank(values, 50) == 35
    assert nearest_rank(values, 30) == 20  # ceil(1.5) = 2
    assert nearest_rank(values, 99) == 50
    assert nearest_rank([7], 50) == 7
    assert nearest_rank([], 50) is None


def test_each_whole_second_takes_the_last_quote_at_or_before_it():
    audit = SegmentAudit(T0, T0 + MINUTE_NS - 1)
    audit.on_quote(T0 + SECOND_NS // 2, D(100), D(101), D(1), D(2))  # before second 1, after second 0
    audit.on_quote(T0 + 3 * SECOND_NS, D(100), D("100.5"), D(3), D(4))  # exactly at second 3
    audit.close()

    spreads = list(audit.samples.spread_bps)
    # Second 0 has no quote yet; seconds 1 and 2 use the first; 3 to 59 use the second.
    assert len(spreads) == 59
    assert spreads[:2] == [scaled(D(1) / D("100.5") * 10_000)] * 2
    assert spreads[2] == scaled(D("0.5") / D("100.25") * 10_000)
    assert list(audit.samples.depth_btc[:3]) == [scaled(D(3)), scaled(D(3)), scaled(D(7))]


def test_a_crossed_quote_is_not_sampled_and_the_previous_one_stays():
    audit = SegmentAudit(T0, T0 + MINUTE_NS - 1)
    audit.on_quote(T0, D(100), D(101), D(1), D(1))
    audit.on_quote(T0 + 10 * SECOND_NS, D(101), D(101), D(1), D(1))
    audit.close()

    assert set(audit.samples.spread_bps) == {scaled(D(1) / D("100.5") * 10_000)}
    assert len(audit.samples.spread_bps) == 60


def test_moves_pair_two_seconds_of_one_segment_at_each_horizon():
    audit = SegmentAudit(T0, T0 + 2 * MINUTE_NS - 1)
    audit.on_quote(T0, D(99), D(101), D(1), D(1))  # mid 100
    audit.on_quote(T0 + 60 * SECOND_NS, D("100.1"), D("102.1"), D(1), D(1))  # mid 101.1 from second 60
    audit.close()

    moves = list(audit.samples.moves_bps[1])
    # 120 seconds give 60 one minute pairs: second t against t + 60, all crossing to the new mid.
    assert len(moves) == 60
    assert set(moves) == {scaled(D(110))}  # 1.1 / 100 * 10,000
    assert list(audit.samples.moves_bps[5]) == []  # the segment is shorter than 5 minutes


def test_trades_fold_into_minutes_by_ts_event_and_count_quiet_minutes():
    audit = SegmentAudit(T0, T0 + 3 * MINUTE_NS - 1)
    audit.on_trade(T0 + MINUTE_NS - 1, D("0.5"), "buy")  # the last nanosecond of minute 0
    audit.on_trade(T0 + MINUTE_NS, D("0.2"), "sell")  # the first of minute 1
    audit.on_trade(T0 + MINUTE_NS + 5, D("0.3"), "none")
    audit.on_trade(T0 + 3 * MINUTE_NS, D(1), "buy")  # after the segment
    audit.close()

    assert list(audit.samples.trades_per_min) == [1, 2, 0]
    assert list(audit.samples.abs_delta_btc) == [scaled(D("0.5")), scaled(D("0.2")), 0]
    assert (audit.samples.buy_size, audit.samples.traded_size) == (scaled(D("0.5")), scaled(D(1)))
    assert audit.late_trades == 1


def test_samples_are_also_kept_per_utc_hour():
    audit = SegmentAudit(T0 + 23 * HOUR_NS, T0 + 24 * HOUR_NS - 1)
    audit.on_quote(T0 + 23 * HOUR_NS, D(100), D(101), D(1), D(1))
    audit.close()

    assert list(audit.by_hour) == [23]
    assert len(audit.by_hour[23].spread_bps) == 3600
    assert len(audit.by_hour[23].moves_bps[5]) == 3600 - 300


def test_a_segment_must_cover_whole_minutes():
    with pytest.raises(ValueError, match="whole minutes"):
        SegmentAudit(T0 + 1, T0 + MINUTE_NS)


def test_funding_is_time_weighted_from_the_rate_in_force():
    span = Span(T0, T0 + 2 * HOUR_NS - 1)
    rates = [Rate(T0 - HOUR_NS, D("0.0001")), Rate(T0 + HOUR_NS // 2, D("-0.0003"))]

    # Half an hour at 0.0001, then an hour and a half at |-0.0003|.
    assert mean_abs_funding([span], rates) == (D("0.0001") * 1 + D("0.0003") * 3) / 4


def test_a_span_with_no_rate_before_it_counts_from_its_first_rate():
    span = Span(T0, T0 + HOUR_NS - 1)

    assert mean_abs_funding([span], [Rate(T0 + HOUR_NS // 2, D("0.0002"))]) == D("0.0002")
    assert mean_abs_funding([span], [Rate(T0 + HOUR_NS, D("0.0002"))]) is None
    assert mean_abs_funding([span], []) is None


def _with_moves(median_bps: str) -> Samples:
    samples = Samples()
    samples.moves_bps[5].extend([scaled(D(median_bps))] * 3)
    return samples


def test_reachable_compares_the_pooled_5_minute_move_with_each_entry_cost():
    # maker entry 7 bps (hurdle 10.5), taker entry 10 bps (hurdle 15).
    assert reachable(_with_moves("10.5"), D("1.5")) == {"maker_entry": True, "taker_entry": False}
    assert reachable(_with_moves("15"), D("1.5")) == {"maker_entry": True, "taker_entry": True}
    assert reachable(_with_moves("10.4999"), D("1.5")) == {"maker_entry": False, "taker_entry": False}
    assert reachable(Samples(), D("1.5")) == {"maker_entry": None, "taker_entry": None}


def test_audit_json_pools_days_and_is_stable():
    def day(on: date, spread_ask: str) -> DayAudit:
        audit = SegmentAudit(T0, T0 + 6 * MINUTE_NS - 1)
        audit.on_quote(T0, D(100), D(spread_ask), D(1), D(1))
        audit.on_trade(T0, D(1), "buy")
        audit.close()
        return DayAudit(on, audit.samples, audit.by_hour, 1, {"missing": 23}, D("0.0001"), 0)

    days = [day(date(2022, 4, 1), "100.5"), day(date(2022, 5, 1), "101")]

    # Read back as plain JSON, as a reader of audit.json sees it.
    first = json.loads(json.dumps(audit_json(days, D("1.5"), "a")))
    again = json.loads(json.dumps(audit_json(days, D("1.5"), "b")))

    assert {k: v for k, v in first.items() if k != "generated_at"} == {
        k: v for k, v in again.items() if k != "generated_at"
    }
    assert first["days"] == ["2022-04-01", "2022-05-01"]
    pooled = first["pooled"]
    assert pooled["seconds_sampled"] == 720
    assert pooled["spread_bps"]["p50"] == str(D(scaled(D("0.5") / D("100.25") * 10_000)) * D("0.0001"))
    assert pooled["trades_per_minute_p50"] == 0
    assert pooled["aggressive_buy_share"] == "1.0000"
    assert first["reachable"] == {"maker_entry": False, "taker_entry": False}
    assert first["per_day"]["2022-04-01"]["dropped_hours"] == {"missing": 23}
    assert list(first["per_hour"]) == ["00"]
