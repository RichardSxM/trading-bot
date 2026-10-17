"""covers: spec 0014 AC-8, AC-10, AC-11, AC-12, AC-13, AC-14, AC-15 (one candle unit with buy_and_hold)."""

from dataclasses import replace
from decimal import Decimal

from lab.actors import RANGE_END
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
    tree = candle_tree(tmp_path)
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


def test_buy_and_hold_is_hour_level_and_its_bar_spec_is_checked():
    entry = registry_entry("buy_and_hold")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert registry_entry("random_taker").kind == StrategyKind.SCALPING
    assert "stale_quote_s" not in entry.user_fields()  # the run file's, not [strategy.config]'s
    assert bar_spec_problem("1-HOUR-LAST") is None
    assert bar_spec_problem("4-HOUR-LAST") is None
    for bad in ("1-MINUTE-MID", "1-TICK-LAST", "1-SECOND-LAST", "nonsense", "0-HOUR-LAST"):
        assert bar_spec_problem(bad) is not None
