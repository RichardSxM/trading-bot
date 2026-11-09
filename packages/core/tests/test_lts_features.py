"""covers: spec 0031 AC-10 (flow, forced trades in either order), AC-11 (impact, efficiency, ranks), AC-12 (depth)."""

from decimal import Decimal

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.normalized import NormalizedTrade, TradeKind
from trading_core.strategy.lts_features import (
    WINDOW_1S_NS,
    WINDOW_2S_NS,
    WINDOW_250MS_NS,
    DepthTrack,
    EfficiencyRanks,
    FlowWindows,
    aligned_impact_bps,
    efficiency,
    replenishment_ratio,
)

BUY, SELL = AggressorSide.BUY, AggressorSide.SELL
MS = 1_000_000


def _trade(ts, side, notional, trade_id, kind=TradeKind.FILL):
    return NormalizedTrade("X", trade_id, ts, ts, Decimal(100), Decimal(1), Decimal(notional), side, kind)


def test_flow_by_hand():
    flow = FlowWindows()
    flow.add_trade(_trade(0, BUY, 100, "a"))
    flow.add_trade(_trade(100 * MS, SELL, 300, "b"))
    flow.add_trade(_trade(200 * MS, BUY, 200, "c"))

    one = flow.snapshot(WINDOW_1S_NS, 200 * MS)

    assert one.buy_notional == 300
    assert one.sell_notional == 300
    assert one.delta == 0
    assert one.total == 600
    assert one.imbalance == 0
    assert (one.buy_count, one.sell_count) == (2, 1)
    assert one.median_notional == 200
    assert one.largest_notional == 300
    assert one.arrivals_per_s == 3


def test_a_window_excludes_its_start_and_includes_its_end():
    flow = FlowWindows()
    flow.add_trade(_trade(0, BUY, 100, "a"))
    flow.add_trade(_trade(WINDOW_250MS_NS, BUY, 50, "b"))

    assert flow.side_notional(BUY, WINDOW_250MS_NS, WINDOW_250MS_NS) == 50
    assert flow.side_notional(BUY, WINDOW_2S_NS, WINDOW_250MS_NS) == 150


def test_an_empty_window_has_no_ratios():
    empty = FlowWindows().snapshot(WINDOW_1S_NS, 0)

    assert empty.imbalance is None
    assert empty.median_notional is None
    assert empty.largest_notional is None
    assert empty.arrivals_per_s == 0


def test_a_forced_trade_arriving_after_the_trade_is_taken_out():
    flow = FlowWindows()
    assert flow.add_trade(_trade(0, SELL, 500, "liq")) is True

    flow.add_forced("liq", SELL, Decimal(500), 10, TradeKind.LIQUIDATION)

    snap = flow.snapshot(WINDOW_1S_NS, 10)
    assert snap.sell_notional == 0
    assert snap.liquidation_sell == 500


def test_a_forced_trade_arriving_before_the_trade_is_left_out():
    flow = FlowWindows()
    flow.add_forced("liq", BUY, Decimal(500), 0, TradeKind.LIQUIDATION)

    assert flow.add_trade(_trade(10, BUY, 500, "liq")) is False

    snap = flow.snapshot(WINDOW_1S_NS, 10)
    assert snap.buy_notional == 0
    assert snap.liquidation_buy == 500


def test_a_forced_record_seen_twice_counts_once_and_a_block_is_not_a_liquidation():
    flow = FlowWindows()
    flow.add_forced("liq", SELL, Decimal(500), 0, TradeKind.LIQUIDATION)
    flow.add_forced("liq", SELL, Decimal(500), 1, TradeKind.LIQUIDATION)
    flow.add_forced("blk", SELL, Decimal(500), 2, TradeKind.BLOCK)

    assert flow.snapshot(WINDOW_1S_NS, 3).liquidation_sell == 500


def test_a_trade_of_a_known_forced_kind_is_not_flow():
    flow = FlowWindows()

    assert flow.add_trade(_trade(0, SELL, 500, "liq", TradeKind.LIQUIDATION)) is False

    snap = flow.snapshot(WINDOW_1S_NS, 0)
    assert snap.sell_notional == 0
    assert snap.liquidation_sell == 500


def test_old_prints_leave_the_window():
    flow = FlowWindows()
    flow.add_trade(_trade(0, BUY, 100, "a"))
    flow.add_trade(_trade(WINDOW_2S_NS, BUY, 7, "b"))

    assert flow.snapshot(WINDOW_2S_NS, WINDOW_2S_NS).buy_notional == 7


def test_impact_is_signed_by_the_pressure():
    assert aligned_impact_bps(1, Decimal(100), Decimal(101)) == 100
    assert aligned_impact_bps(-1, Decimal(100), Decimal(101)) == -100


def test_efficiency_by_hand():
    assert efficiency(Decimal(1000), Decimal("9.9")) == 100


def test_a_move_against_the_pressure_counts_as_strong_absorption():
    assert efficiency(Decimal(1000), Decimal(-50)) == 10_000
    assert efficiency(Decimal(1000), Decimal(0)) == 10_000


def test_rank_is_the_share_at_or_below():
    ranks = EfficiencyRanks()
    for value in (1, 2, 3, 4):
        ranks.add(BUY, 0, Decimal(value))

    assert ranks.rank(BUY, Decimal(3)) == 75
    assert ranks.rank(BUY, Decimal(0)) == 0
    assert ranks.rank(BUY, Decimal(9)) == 100
    assert ranks.count(BUY) == 4


def test_ranking_never_stores_the_value():
    ranks = EfficiencyRanks()
    ranks.add(BUY, 0, Decimal(1))

    ranks.rank(BUY, Decimal(5))

    assert ranks.count(BUY) == 1


def test_rank_is_zero_with_no_samples_and_sides_are_separate():
    ranks = EfficiencyRanks()
    ranks.add(BUY, 0, Decimal(1))

    assert ranks.rank(SELL, Decimal(5)) == 0


def _filled(side, samples, start=0):
    ranks = EfficiencyRanks()
    ranks.advance(start)
    for i in range(samples):
        ranks.add(side, start + i, Decimal(i))
    return ranks


def test_a_tiny_sample_stays_cold_even_after_an_hour():
    assert _filled(BUY, 499).warm(BUY, HOUR_NS) is False


def test_warm_needs_the_full_hour_too():
    ranks = _filled(BUY, 500)

    assert ranks.warm(BUY, HOUR_NS - 1) is False
    assert ranks.warm(BUY, HOUR_NS) is True


def test_warm_is_per_side():
    assert _filled(BUY, 500).warm(SELL, HOUR_NS) is False


def test_samples_older_than_an_hour_leave():
    ranks = EfficiencyRanks()
    ranks.advance(0)
    ranks.add(BUY, 0, Decimal(1))
    ranks.add(BUY, SECOND_NS, Decimal(2))

    ranks.advance(HOUR_NS)

    assert ranks.count(BUY) == 1
    assert ranks.rank(BUY, Decimal(1)) == 0


def test_replenishment_by_hand():
    assert replenishment_ratio(Decimal(10), Decimal(4), Decimal(9)) == Decimal("0.5")
    assert replenishment_ratio(Decimal(10), Decimal(4), Decimal(3)) == 0


def test_replenishment_with_no_depth_before_does_not_divide_by_zero():
    assert replenishment_ratio(Decimal(0), Decimal(0), Decimal(1)) == Decimal(100_000_000)


def test_depth_track_measures_recovery_from_the_minimum():
    track = DepthTrack(Decimal(10), 0)
    track.update(Decimal(4), 5)
    track.update(Decimal(6), 7)
    track.update(Decimal(11), 9)

    assert (track.before, track.minimum, track.after) == (Decimal(10), Decimal(4), Decimal(11))
    assert track.recovery_ns == 4
    assert track.ratio == Decimal("0.7")


def test_depth_track_never_replenished_is_none():
    track = DepthTrack(Decimal(10), 0)
    track.update(Decimal(4), 5)
    track.update(Decimal(9), 7)

    assert track.recovery_ns is None


def test_a_lower_minimum_restarts_the_wait():
    track = DepthTrack(Decimal(10), 0)
    track.update(Decimal(4), 1)
    track.update(Decimal(12), 2)
    track.update(Decimal(3), 3)

    assert track.recovery_ns is None
