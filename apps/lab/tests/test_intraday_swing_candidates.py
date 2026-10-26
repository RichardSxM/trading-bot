"""covers: spec 0019 AC-2 (each new candidate registered as hour_level with its own module), AC-4 (a bad
setting, or bars other than 15 minutes, refused before any run), AC-6 (ema_pullback trades through the
candle lab on 15 minute bars)."""

from decimal import Decimal
from pathlib import Path

import pytest

from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.runfile import RunFileError
from lab.strategies import MarketContextValues, StrategyConfigError, registry_entry
from lab.study import load_study
from trading_core.recording.enums import Intent, StrategyKind
from trading_core.risk.check import RiskLimits

from .candle_fixtures import DAY, FEB, synthetic_month

D = Decimal
STUDIES = Path(__file__).resolve().parents[1] / "studies"
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
EXIT_REASONS = {"target", "time_stop", "stop_through", "range_end", "liquidated"}


def test_ema_pullback_is_an_hour_level_strategy_in_its_own_module():
    entry = registry_entry("ema_pullback")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert entry.module == "trading_core.node.ema_pullback"
    assert {"fast", "slow", "rsi_period", "rsi_low", "rsi_high", "arm_bars", "target_r"} <= entry.user_fields()
    assert "stale_quote_s" not in entry.user_fields()  # the run file's


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"fast": 21, "slow": 8}, "slow"),
        ({"bar_spec": "1-HOUR-LAST"}, "bar_spec"),
        ({"rsi_high": 55}, "rsi_high"),
        ({"target_r": "0"}, "target_r"),
    ],
)
def test_a_bad_ema_pullback_setting_is_refused_by_the_startup_check(change, problem):
    with pytest.raises(StrategyConfigError, match=problem):
        registry_entry("ema_pullback").check_strategy(change)


def test_an_ema_pullback_study_on_hour_bars_is_refused_before_any_run(tmp_path):
    text = (STUDIES / "ema_pullback.toml").read_text(encoding="utf-8")
    path = tmp_path / "hourly.toml"
    path.write_text(text.replace('bar_spec = "15-MINUTE-LAST"', 'bar_spec = "1-HOUR-LAST"'), encoding="utf-8")
    with pytest.raises(RunFileError, match="bar_spec"):
        load_study(path)


def test_ema_pullback_trades_with_floored_stops_on_15_minute_bars_through_the_candle_lab(tmp_path):
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
        strategy_name="ema_pullback",
        strategy_config={"rsi_low": 1, "rsi_high": 50},
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    result = run_candle_unit(plan)

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    exits = [d for d in decisions if d.intent == Intent.EXIT]
    assert entries
    assert {d.reason for d in entries} <= {"ema_pullback_long", "ema_pullback_short"}
    assert {d.reason for d in exits} <= EXIT_REASONS
    for d in entries:
        mid, atr, stop = (D(str(d.inputs[k])) for k in ("mid", "atr", "stop"))
        assert abs(mid - stop) >= atr  # never closer than 1 ATR to the mid (spec 0018 AC-2)
        assert {"ema_fast", "ema_slow", "rsi", "trend", "armed"} <= set(d.inputs)
    assert all(p.quantity == 0 for _, p in result.positions)
