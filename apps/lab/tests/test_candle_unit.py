"""covers: spec 0014 AC-8, AC-10, AC-11, AC-12, AC-13, AC-14, AC-15 (one candle unit: buy_and_hold, macd_baseline,
the liquidation guard)."""

from dataclasses import replace
from decimal import Decimal

from lab.actors import LIQUIDATED, RANGE_END
from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.strategies import MarketContextValues, bar_spec_problem, registry_entry
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import Intent, OrderSide, RiskVerdict, StrategyKind
from trading_core.risk.check import RiskLimits

D = Decimal
SEP = Month(2026, 9)
T0 = SEP.start_ns
HOURS = 3
RISK = RiskLimits(
    max_order_notional=D(20000),
    max_leverage=D(2),
    max_total_exposure=D(20000),
    risk_per_trade=D("0.005"),
    maintenance_margin_rate=D("0.01"),
    min_liq_distance_pct=D(10),
    liq_stop_multiple=D(3),
    stop_slippage_bps=D(5),
)


def _minute(m, quiet=False):
    o = D(60000 + m)
    if quiet:
        return CandleRow(T0 + (m + 1) * MINUTE_NS, o, o, o, o, D(0), True)
    return CandleRow(T0 + (m + 1) * MINUTE_NS, o, o + 5, o - 5, o + 1, D(1), False)


def _plan(tmp_path, rows, **overrides):
    tree = candle_tree(tmp_path, "PF_XBTUSD.KRAKEN")
    tree.write_bars(SEP, rows)
    tree.write_funding(SEP, [FundingRow(T0 + h * HOUR_NS, D("0.0001"), HOUR_NS, "api") for h in range(HOURS + 1)])
    plan = CandleUnitPlan(
        variant="base",
        candles_root=tree.root,
        instrument_id="PF_XBTUSD.KRAKEN",
        start_ns=T0,
        end_ns=T0 + HOURS * HOUR_NS,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name="buy_and_hold",
        strategy_config={"notional_usd": "10000"},
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    return replace(plan, **overrides)


def test_buy_and_hold_buys_at_the_first_hour_close_and_closes_at_the_range_end(tmp_path):
    rows = [_minute(m) for m in range(HOURS * 60)]
    result = run_candle_unit(_plan(tmp_path, rows))

    decisions = [d for _, d in result.decisions]
    assert [(d.intent, d.reason, d.risk_verdict) for d in decisions] == [
        (Intent.ENTER, "buy_and_hold_entry", RiskVerdict.APPROVED),
        (Intent.EXIT, RANGE_END, RiskVerdict.APPROVED),
    ]
    assert decisions[0].decided_ns == T0 + HOUR_NS  # the first hour bar's close
    entry, exit_ = (f for _, f in result.fills)
    # The quote current at the hour close is minute 59's close point (60060) with 2 bps around it.
    assert (entry.side, entry.price) == (OrderSide.BUY, D(60073))  # ceil(60060 x 1.0002)
    # The range end close fills against the final quote: the last close, 60180, at the bid.
    assert (exit_.side, exit_.price) == (OrderSide.SELL, D(60167))  # floor(60180 x 0.9998)
    assert entry.quantity == exit_.quantity == D("0.1665")  # 10000 / 60060 floored to the step
    [(_, position)] = result.positions
    assert position.quantity == 0

    # Funding is sampled at 01:00 and 02:00 (strictly inside the range); the position is open at 02:00.
    samples = {s.boundary_ns: s for s in result.funding_samples}
    assert sorted(samples) == [T0 + HOUR_NS, T0 + 2 * HOUR_NS]
    assert samples[T0 + 2 * HOUR_NS].signed_qty == D("0.1665")
    assert samples[T0 + 2 * HOUR_NS].mid == D(60120)  # minute 119's close point, the last quote before 02:00
    assert result.zero_volume_bars == 0
    assert result.first_trade_ns == T0


def test_a_quiet_hour_builds_a_zero_volume_bar_that_is_counted(tmp_path):
    rows = [_minute(m) for m in range(60)] + [_minute(m, quiet=True) for m in range(60, 120)]
    rows += [_minute(m) for m in range(120, HOURS * 60)]
    result = run_candle_unit(_plan(tmp_path, rows, strategy_config={"notional_usd": "10000", "max_entry_attempts": 1}))
    assert result.zero_volume_bars == 1


def test_entries_are_blocked_only_inside_a_real_gap(tmp_path):
    """covers: spec 0014 AC-13 (the stale quote block at stale_quote_s 300 and a 60 s check)."""
    # Minutes 52 to 59 are missing: at the 01:00 close the newest quote is 8 minutes old.
    rows = [_minute(m) for m in range(HOURS * 60) if not 52 <= m < 60]
    result = run_candle_unit(_plan(tmp_path, rows))
    decisions = [d for _, d in result.decisions]
    assert [(d.decided_ns, d.risk_verdict, d.risk_reason) for d in decisions[:2]] == [
        (T0 + HOUR_NS, RiskVerdict.REJECTED, "blocked:stale_feed"),
        (T0 + 2 * HOUR_NS, RiskVerdict.APPROVED, None),  # quotes are fresh again by the next close
    ]


def _path(closes):
    # One trade minute per close, opening at the previous close, 2 ticks beyond each end.
    rows, previous = [], closes[0]
    for m, close in enumerate(closes):
        o, c = D(previous), D(close)
        rows.append(CandleRow(T0 + (m + 1) * MINUTE_NS, o, max(o, c) + 2, min(o, c) - 2, c, D(1), False))
        previous = close
    return rows


MACD_FAST = {"notional_usd": "10000", "bar_spec": "1-MINUTE-LAST", "fast": 2, "slow": 3, "signal": 2, "trend_ema": 3}


# An accelerating fall (histogram below 0), then a jump: bar 20 crosses up above the trend (enter long)
# and bar 21's smaller rise weakens the histogram (exit).
FALL = [60000 - i * i for i in range(20)]


def test_macd_baseline_enters_rests_a_stop_from_the_fill_and_cancels_it_on_its_exit(tmp_path):
    closes = FALL + [59839 + 80 * i for i in range(8)]
    closes += [60200] * (HOURS * 60 - len(closes))
    result = run_candle_unit(_plan(tmp_path, _path(closes), strategy_name="macd_baseline", strategy_config=MACD_FAST))

    decisions = [d for _, d in result.decisions]
    entry_d, exit_d = decisions[0], decisions[1]
    assert (entry_d.intent, entry_d.side, entry_d.reason, entry_d.risk_verdict) == (
        Intent.ENTER,
        OrderSide.BUY,
        "macd_entry",
        RiskVerdict.APPROVED,
    )
    assert (exit_d.intent, exit_d.side, exit_d.reason) == (Intent.EXIT, OrderSide.SELL, "macd_exit")
    assert (entry_d.decided_ns, exit_d.decided_ns) == (T0 + 21 * MINUTE_NS, T0 + 22 * MINUTE_NS)
    # The entry was judged with a stop 3 % below the mid.
    assert D(str(entry_d.inputs["stop_px"])) == D(int(D(str(entry_d.inputs["mid"])) * D("0.97")))

    orders = {o.client_order_id: o for _, o in result.orders}
    [stop] = [o for o in orders.values() if o.trigger_price is not None and o.decision_id == entry_d.id]
    entry_fill, exit_fill = [f for _, f in result.fills][:2]
    # The stop rests 3 % from the entry's fill (at the ask), not from the mid, and is reduce only.
    assert stop.trigger_price == D(int(entry_fill.price * D("0.97")))
    assert (stop.reduce_only, stop.side, stop.decision_id) == (True, OrderSide.SELL, entry_d.id)
    assert stop.status.value == "canceled"  # the exit cancelled it
    assert (exit_fill.side, exit_fill.quantity) == (OrderSide.SELL, entry_fill.quantity)
    assert exit_fill.client_order_id != stop.client_order_id
    assert all(p.quantity == 0 for _, p in result.positions)


def test_macd_baseline_stops_out_at_the_quote_that_crosses_its_stop(tmp_path):
    closes = [*FALL, 59839]
    closes += [55000] * (HOURS * 60 - len(closes))  # a crash in the minute after the entry
    result = run_candle_unit(_plan(tmp_path, _path(closes), strategy_name="macd_baseline", strategy_config=MACD_FAST))

    entry_fill, stop_fill = [f for _, f in result.fills][:2]
    orders = {o.client_order_id: o for _, o in result.orders}
    stop = orders[stop_fill.client_order_id]
    assert stop.trigger_price is not None
    assert stop.status.value == "filled"
    # The crash minute's first point is far below the trigger: it fills there, never at the trigger.
    assert stop_fill.price < stop.trigger_price
    assert stop_fill.quantity == entry_fill.quantity


def _crash(low):
    # A 1.9x long at the 01:00 close; minute 70 falls to ``low`` (a long walks the low first, at + 20 s).
    rows = [_minute(m) for m in range(70)]
    rows.append(CandleRow(T0 + 71 * MINUTE_NS, D(60070), D(60072), D(low), D(low + 300), D(1), False))
    flat = D(low + 300)
    rows += [CandleRow(T0 + (m + 1) * MINUTE_NS, flat, flat + 5, flat - 5, flat, D(1), False) for m in range(71, 180)]
    return rows


CRASH_AT = T0 + 70 * MINUTE_NS + 20 * 1_000_000_000


def test_a_long_through_its_liquidation_price_is_closed_by_the_guard_and_tries_again(tmp_path):
    """covers: spec 0014 AC-14 (liquidation at the crossing quote, then a new entry)."""
    result = run_candle_unit(_plan(tmp_path, _crash(28700), strategy_config={"notional_usd": "19000"}))

    decisions = [d for _, d in result.decisions]
    entry, liquidated, *later = decisions
    assert (entry.intent, entry.risk_verdict) == (Intent.ENTER, RiskVerdict.APPROVED)
    assert (liquidated.intent, liquidated.side, liquidated.reason) == (Intent.EXIT, OrderSide.SELL, LIQUIDATED)
    assert liquidated.decided_ns == CRASH_AT
    _, close = (f for _, f in result.fills)
    # It closes at market on the crossing quote: the bid around the low, floor(28700 x 0.9998).
    assert close.price == D(28694) <= D(str(liquidated.inputs["liq_px"]))
    # The attempt count reset, so the next hour close decides again. The liquidation left about the
    # maintenance margin, so the same notional is now over the leverage cap.
    assert [(d.intent, d.decided_ns, d.risk_reason) for d in later[:2]] == [
        (Intent.ENTER, T0 + 2 * HOUR_NS, "leverage_cap"),
        (Intent.ENTER, T0 + 3 * HOUR_NS, "leverage_cap"),
    ]
    assert result.ruined_ns is None
    assert all(p.quantity == 0 for _, p in result.positions)


def test_equity_at_zero_ends_the_variant_early_as_ruined(tmp_path):
    """covers: spec 0014 AC-14 (the ruin stop)."""
    result = run_candle_unit(_plan(tmp_path, _crash(15000), strategy_config={"notional_usd": "19000"}))

    decisions = [d for _, d in result.decisions]
    assert [(d.intent, d.reason) for d in decisions] == [
        (Intent.ENTER, "buy_and_hold_entry"),
        (Intent.EXIT, LIQUIDATED),
    ]
    assert result.ruined_ns == CRASH_AT  # nothing reaches the strategy after it, not even the range end close
    _, close = (f for _, f in result.fills)
    assert close.price == D(14997)
    assert all(p.quantity == 0 for _, p in result.positions)


def test_buy_and_hold_is_hour_level_and_its_bar_spec_is_checked():
    entry = registry_entry("buy_and_hold")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert registry_entry("macd_baseline").kind == StrategyKind.HOUR_LEVEL
    assert registry_entry("random_taker").kind == StrategyKind.SCALPING
    assert "stale_quote_s" not in entry.user_fields()  # the run file's, not [strategy.config]'s
    assert bar_spec_problem("1-HOUR-LAST") is None
    assert bar_spec_problem("4-HOUR-LAST") is None
    for bad in ("1-MINUTE-MID", "1-TICK-LAST", "1-SECOND-LAST", "nonsense", "0-HOUR-LAST"):
        assert bar_spec_problem(bad) is not None
