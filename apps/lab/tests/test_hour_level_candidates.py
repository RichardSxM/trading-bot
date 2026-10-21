"""covers: spec 0018 AC-7 (each candidate registered as hour_level with its own module), AC-8 (a bad setting is
refused at the startup check, before any run), AC-9 (connors_rsi trades through the candle lab)."""

from decimal import Decimal

import pytest

from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.strategies import MarketContextValues, StrategyConfigError, registry_entry
from trading_core.recording.enums import Intent, StrategyKind
from trading_core.risk.check import RiskLimits

from .candle_fixtures import DAY, FEB, synthetic_month

D = Decimal
CONNORS = {"rsi_period": 2, "rsi_lower": 10}
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


def test_connors_rsi_is_an_hour_level_strategy_in_its_own_module():
    entry = registry_entry("connors_rsi")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert entry.module == "trading_core.node.connors_rsi"
    assert {"rsi_period", "rsi_lower", "trend_sma", "time_stop_bars", "atr_floor_mult"} <= entry.user_fields()
    assert "stale_quote_s" not in entry.user_fields()  # the run file's


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"rsi_lower": 50}, "rsi_lower"),
        ({"rsi_period": 1}, "rsi_period"),
        ({"time_stop_bars": 0}, "time_stop_bars"),
        ({"stop_atr_mult": "0"}, "stop_atr_mult"),
        ({"bar_spec": "1-MINUTE-MID"}, "bar_spec"),
    ],
)
def test_a_bad_connors_setting_is_refused_by_the_startup_check(change, problem):
    with pytest.raises(StrategyConfigError, match=problem):
        registry_entry("connors_rsi").check_strategy({**CONNORS, **change})


def test_the_grid_settings_have_no_default():
    with pytest.raises(StrategyConfigError, match="rsi_lower"):
        registry_entry("connors_rsi").check_strategy({"rsi_period": 2})


def test_connors_rsi_trades_with_floored_risk_sized_stops_through_the_candle_lab(tmp_path):
    root = tmp_path / "catalog"
    synthetic_month(root, days=10)
    plan = CandleUnitPlan(
        variant="base",
        candles_root=root / "candles",
        instrument_id="PF_XBTUSD.KRAKEN",
        start_ns=FEB.start_ns,
        end_ns=FEB.start_ns + 10 * DAY,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name="connors_rsi",
        strategy_config={**CONNORS, "trend_sma": 20},
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    result = run_candle_unit(plan)

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    exits = [d for d in decisions if d.intent == Intent.EXIT]
    assert entries
    assert {d.reason for d in entries} <= {"connors_long", "connors_short"}
    assert {d.reason for d in exits} <= {"target", "time_stop", "stop_through", "range_end", "liquidated"}
    for d in entries:
        mid, atr, stop = (D(str(d.inputs[k])) for k in ("mid", "atr", "stop"))
        assert abs(mid - stop) >= atr  # never closer than 1 ATR to the mid (AC-2)
        assert D(str(d.inputs["structural_stop"])) != stop or d.inputs["floor_applied"] is False
        assert D(str(d.inputs["funding_periods"])) >= 9  # a 10 hour hold crosses 9 or 10 hour boundaries
    assert all(p.quantity == 0 for _, p in result.positions)
