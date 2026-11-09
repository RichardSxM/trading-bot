"""covers: spec 0031 AC-13 (the sweep signal and its mirror) and AC-14 (a cut stream gives the same earlier signals)."""

import random
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.normalized import NormalizedTrade, TradeKind
from trading_core.strategy.lts_sweep import (
    DetectorState,
    EndReason,
    LtsParams,
    LtsSide,
    RollingExtremes,
    SweepDetector,
)

BUY, SELL = AggressorSide.BUY, AggressorSide.SELL
MS = 1_000_000
BASE = Decimal(100_000)
PARAMS = LtsParams(
    tick=Decimal(1),
    lookback_s=60,
    sweep_buffer_ticks=1,
    reclaim_buffer_ticks=1,
    invalidation_buffer_ticks=5,
    reclaim_timeout_ms=3000,
    efficiency_pct_min=90,
    horizon_s=15,
)
# Warm up data: one trade per second and side, its notional cycling 1000 to 100000.
WARM_S = 3700
W = WARM_S * SECOND_NS
_ids = iter(range(10**9))


def trade(ts, price, side, notional=1, kind=TradeKind.FILL):
    return NormalizedTrade("X", f"t{next(_ids)}", ts, ts, Decimal(price), Decimal(1), Decimal(notional), side, kind)


def warm_events():
    events: list[Any] = []
    for second in range(WARM_S):
        ts = second * SECOND_NS
        notional = 1000 * (second % 100 + 1)
        events.append(("mid", ts, BASE))
        events.append(("trade", trade(ts + 10 * MS, BASE, SELL, notional)))
        events.append(("trade", trade(ts + 20 * MS, BASE, BUY, notional)))
    return events


def play(detector, events):
    signals = []
    for event in events:
        if event[0] == "mid":
            signal = detector.on_mid(event[1], Decimal(event[2]))
            if signal is not None:
                signals.append(signal)
        else:
            detector.on_trade(event[1])
    return signals


@pytest.fixture
def warmed():
    detector = SweepDetector(PARAMS)
    play(detector, warm_events())
    return detector


def at(ms):
    return W + ms * MS


BIG = 500_000


def test_a_failed_breakdown_gives_one_long(warmed):
    signals = play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(200), BASE - 1),
            ("mid", at(300), BASE + 1),
        ],
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.side is LtsSide.LONG
    assert signal.ts_init_ns == at(300)
    assert signal.mid == BASE + 1
    assert signal.level == BASE
    assert signal.extreme == BASE - 1
    assert signal.started_ns == at(100)
    assert signal.latched_ns == at(100)
    assert signal.efficiency_rank == 100
    assert warmed.state is DetectorState.FLAT


def test_a_warming_detector_ignores_a_sweep():
    detector = SweepDetector(PARAMS)
    events = warm_events()[:600]
    play(detector, events)

    detector.on_trade(trade(300 * SECOND_NS, BASE - 1, SELL, BIG))

    assert detector.state is DetectorState.WARMING
    assert detector.candidate is None


def test_a_level_needs_a_trade_past_the_buffer(warmed):
    warmed.on_trade(trade(at(100), BASE, SELL, BIG))

    assert warmed.candidate is None


def test_no_absorption_times_out(warmed):
    play(
        warmed,
        [
            ("trade", trade(at(2000), BASE - 1, SELL, 1)),
            ("mid", at(5100), BASE + 1),
        ],
    )

    assert warmed.ended[EndReason.NO_ABSORPTION] == 1
    assert warmed.candidate is None


def test_a_genuine_breakdown_invalidates(warmed):
    signals = play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("trade", trade(at(200), BASE - 6, SELL, 1)),
            ("mid", at(300), BASE + 1),
        ],
    )

    assert signals == []
    assert warmed.ended[EndReason.INVALIDATED] == 1


def test_a_trade_short_of_the_invalidation_keeps_it_alive(warmed):
    play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("trade", trade(at(200), BASE - 5, SELL, 1)),
        ],
    )

    assert warmed.candidate is not None
    assert warmed.state is DetectorState.ABSORBED


def test_a_late_reclaim_times_out(warmed):
    signals = play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(3101), BASE + 1),
        ],
    )

    assert signals == []
    assert warmed.ended[EndReason.NO_RECLAIM] == 1


def test_a_reclaim_exactly_at_the_timeout_still_counts(warmed):
    signals = play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(3100), BASE + 1),
        ],
    )

    assert len(signals) == 1


def test_a_level_stays_frozen_while_price_runs(warmed):
    # No absorption yet, so the extreme tracks; a far bigger seller then latches.
    signals = play(
        warmed,
        [
            ("trade", trade(at(2000), BASE - 1, SELL, 1)),
            ("trade", trade(at(2100), BASE - 8, SELL, 1)),
            ("trade", trade(at(2200), BASE - 4, SELL, BIG)),
            ("mid", at(2300), BASE - 4),
            ("mid", at(2400), BASE + 1),
        ],
    )

    assert len(signals) == 1
    assert signals[0].level == BASE
    assert signals[0].extreme == BASE - 8


def test_a_mid_already_above_the_level_needs_a_fresh_cross(warmed):
    warmed.on_mid(at(50), BASE + 3)
    signals = play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(200), BASE + 4),
        ],
    )
    assert signals == []

    signals = play(warmed, [("mid", at(300), BASE), ("mid", at(400), BASE + 1)])

    assert len(signals) == 1


def test_an_opposite_sweep_is_ignored(warmed):
    play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("trade", trade(at(200), BASE + 2, BUY, BIG)),
        ],
    )

    assert warmed.candidate is not None
    assert warmed.candidate.side is LtsSide.LONG
    assert warmed.candidate.level == BASE


def test_a_candidate_view_is_stable_until_it_ends(warmed):
    warmed.on_trade(trade(at(100), BASE - 1, SELL, BIG))
    first = warmed.candidate

    warmed.on_trade(trade(at(200), BASE, BUY, 1))

    assert warmed.candidate is first


def test_new_sweeps_are_ignored_for_the_horizon(warmed):
    play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(200), BASE - 1),
            ("mid", at(300), BASE + 1),
            ("trade", trade(at(400), BASE - 3, SELL, BIG)),
        ],
    )

    assert warmed.ended[EndReason.COOLDOWN] == 1
    assert warmed.candidate is None


def test_a_sweep_after_the_horizon_starts_again(warmed):
    play(
        warmed,
        [
            ("trade", trade(at(100), BASE - 1, SELL, BIG)),
            ("mid", at(200), BASE - 1),
            ("mid", at(300), BASE + 1),
            ("trade", trade(at(15_300), BASE - 3, SELL, BIG)),
        ],
    )

    assert warmed.ended[EndReason.COOLDOWN] == 0
    assert warmed.candidate is not None


def test_segment_end_is_counted(warmed):
    warmed.on_trade(trade(at(100), BASE - 1, SELL, BIG))

    warmed.finish()

    assert warmed.ended[EndReason.SEGMENT_END] == 1
    assert warmed.candidate is None


def test_a_print_known_to_be_forced_never_starts_a_candidate(warmed):
    warmed.on_trade(trade(at(100), BASE - 1, SELL, BIG, TradeKind.LIQUIDATION))

    assert warmed.candidate is None


def test_a_forced_record_removes_its_trade_from_the_flow(warmed):
    known = trade(at(2000), BASE, SELL, BIG)
    warmed.on_forced(known.trade_id, SELL, known.notional, at(1990), TradeKind.LIQUIDATION)
    warmed.on_trade(known)

    # With the big seller gone, the next sweep has no pressure and cannot latch.
    warmed.on_trade(trade(at(2100), BASE - 1, SELL, 1))

    assert warmed.state is DetectorState.SWEPT


def _mirror_price(price):
    return 2 * BASE - Decimal(price)


def _mirror(events):
    swap = {BUY: SELL, SELL: BUY}
    out: list[Any] = []
    for event in events:
        if event[0] == "mid":
            out.append(("mid", event[1], _mirror_price(event[2])))
        else:
            original = event[1]
            mirrored = replace(original, price=_mirror_price(original.price), aggressor=swap[original.aggressor])
            out.append(("trade", mirrored))
    return out


def _failed_breakdown(start_ms):
    return [
        ("trade", trade(at(start_ms + 100), BASE - 1, SELL, BIG)),
        ("mid", at(start_ms + 200), BASE - 1),
        ("mid", at(start_ms + 300), BASE + 1),
    ]


def test_a_short_is_the_exact_mirror_of_a_long():
    events = warm_events() + _failed_breakdown(0)

    longs = play(SweepDetector(PARAMS), events)
    shorts = play(SweepDetector(PARAMS), _mirror(events))

    assert len(longs) == len(shorts) == 1
    long, short = longs[0], shorts[0]
    assert long.side is LtsSide.LONG
    assert short.side is LtsSide.SHORT
    assert short.ts_init_ns == long.ts_init_ns
    assert short.started_ns == long.started_ns
    assert short.latched_ns == long.latched_ns
    assert short.mid == _mirror_price(long.mid)
    assert short.level == _mirror_price(long.level)
    assert short.extreme == _mirror_price(long.extreme)
    assert (short.sell_notional_1s, short.buy_notional_1s) == (long.buy_notional_1s, long.sell_notional_1s)
    assert short.efficiency_rank == long.efficiency_rank


def test_mirrored_failures_end_the_same_way():
    events = [
        *warm_events(),
        ("trade", trade(at(100), BASE - 1, SELL, BIG)),
        ("trade", trade(at(200), BASE - 6, SELL, 1)),
    ]
    detector = SweepDetector(PARAMS)
    play(detector, _mirror(events))

    assert detector.ended[EndReason.INVALIDATED] == 1


def _long_stream():
    """Warm up, then a failed breakdown every 70 s with noise between, some of them failing."""
    rng = random.Random(31)
    events = warm_events()
    for round_ in range(12):
        start = round_ * 70_000
        for ms in range(0, 69_000, 250):
            ts = at(start + 5000 + ms)
            px = BASE + rng.randint(0, 2) - 1
            if rng.random() < 0.5:
                events.append(("mid", ts, BASE + Decimal(rng.randint(0, 1))))
            else:
                events.append(("trade", trade(ts, max(px, BASE), rng.choice((BUY, SELL)), rng.randint(1, 200))))
        sweep = at(start + 75_000)
        events.append(("trade", trade(sweep, BASE - 1, SELL, BIG)))
        events.append(("mid", sweep + 100 * MS, BASE - 1))
        events.append(("mid", sweep + 300 * MS, BASE + 1 if round_ % 3 else BASE - 1))
    return sorted(events, key=lambda e: e[1] if e[0] == "mid" else e[1].ts_init_ns)


def _ts(event):
    return event[1] if event[0] == "mid" else event[1].ts_init_ns


def test_a_cut_stream_gives_exactly_the_earlier_signals():
    events = _long_stream()
    full = play(SweepDetector(PARAMS), events)
    assert len(full) >= 3

    for cut in (s.ts_init_ns for s in full):
        for shift in (-1, 0, 1):
            cut_at = cut + shift
            truncated = play(SweepDetector(PARAMS), [e for e in events if _ts(e) <= cut_at])
            assert truncated == [s for s in full if s.ts_init_ns <= cut_at]


def test_rolling_extremes_exclude_the_window_start():
    rolling = RollingExtremes(10)
    rolling.add(0, Decimal(5))
    rolling.add(5, Decimal(7))

    assert rolling.low(9) == 5
    assert rolling.low(10) == 7
    assert rolling.high(10) == 7
    assert rolling.low(100) is None


def test_rolling_needs_a_whole_lookback():
    rolling = RollingExtremes(10)
    assert rolling.full(100) is False
    rolling.add(0, Decimal(5))

    assert rolling.full(9) is False
    assert rolling.full(10) is True


def test_a_warm_up_hour_is_60_minutes():
    assert WARM_S * SECOND_NS > HOUR_NS
