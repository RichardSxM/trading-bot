"""covers: spec 0014 AC-19 (look ahead: the prefix check over every candle strategy, and --lookahead-check)."""

import json
import logging
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.candle_backtest import backtest, decided_keys
from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.probes import PEEK_REASON
from lab.runfile import Overrides, load_candle_run
from lab.strategies import REGISTRY, MarketContextValues
from trading_core.lab.lookahead import cut_times, first_mismatch
from trading_core.node.bar_rule import bar_interval_ns
from trading_core.recording.enums import StrategyKind
from trading_core.risk.check import RiskLimits

from .candle_fixtures import DAY, FEB, synthetic_month
from .conftest import MemoryStore, worker_settings

D = Decimal
# Ten days of the seeded February: enough hour bars past the 50 bar warmup for many MACD trades.
DAYS = 10
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
HOUR_LEVEL = sorted(name for name, entry in REGISTRY.items() if entry.kind == StrategyKind.HOUR_LEVEL)
# Each candle strategy's settings for the synthetic days; a short trend average gives connors_rsi room to trade.
CONFIGS: dict[str, dict[str, int | str | bool]] = {
    "connors_rsi": {"rsi_period": 2, "rsi_lower": 10, "trend_sma": 20},
    "donchian_4h": {"entry_bars": 2, "bar_spec": "4-HOUR-LAST"},
    "donchian_4h_r1": {"entry_bars": 2, "bar_spec": "1-HOUR-LAST"},
    "ema_pullback": {"rsi_low": 1, "rsi_high": 50},
    # The bar_spec is set so the cuts use its 30 minute interval (the test reads it from the config).
    "noise_breakout": {"lookback_days": 1, "band_mult": "0.1", "bar_spec": "30-MINUTE-LAST"},
}
NOTIONAL = {"notional_usd": "10000"}
RUN = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2026-02-01
to = 2026-02-10
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"

[strategy]
name = "{name}"
[strategy.config]
notional_usd = "10000"
"""


@pytest.fixture(scope="module")
def month(tmp_path_factory):
    """Ten seeded synthetic days of February, shared by the module's tests (they only read it)."""
    root = tmp_path_factory.mktemp("catalog")
    synthetic_month(root, days=DAYS)
    return root


def _plan(catalog, name, **overrides):
    plan = CandleUnitPlan(
        variant="base",
        candles_root=catalog / "candles",
        instrument_ids=("PF_XBTUSD.KRAKEN",),
        start_ns=FEB.start_ns,
        end_ns=FEB.start_ns + DAYS * DAY,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name=name,
        strategy_config=CONFIGS.get(name, NOTIONAL),
        stale_quote_s=D(300),
        test_strategies=True,
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    return replace(plan, **overrides)


def _prefix_mismatches(plan, count):
    """Run ``plan`` in full, then cut at ``count`` seeded times between bar closes; each cut that differs."""
    full = decided_keys(run_candle_unit(plan))
    interval = bar_interval_ns(str(plan.strategy_config.get("bar_spec", "1-HOUR-LAST")))
    found = []
    for cut in cut_times(plan.seed, plan.start_ns, plan.end_ns, count, interval):
        mismatch = first_mismatch(full, decided_keys(run_candle_unit(replace(plan, end_ns=cut, cut_ns=cut))), cut)
        if mismatch is not None:
            found.append(mismatch)
    return full, found


@pytest.mark.parametrize("name", HOUR_LEVEL)
def test_every_candle_strategy_decides_the_same_before_each_of_five_cuts(month, name):
    full, mismatches = _prefix_mismatches(_plan(month, name), 5)
    assert full, f"{name} made no decision over the synthetic month"
    assert mismatches == []


# covers: spec 0019 AC-2 and 0019.1 (connors_rsi on 15 minute bars is prefix tested too)
def test_connors_rsi_on_15_minute_bars_decides_the_same_before_each_of_five_cuts(month):
    config = {**CONFIGS["connors_rsi"], "bar_spec": "15-MINUTE-LAST", "time_stop_bars": 12}
    full, mismatches = _prefix_mismatches(_plan(month, "connors_rsi", strategy_config=config), 5)
    assert full, "connors_rsi made no decision on 15 minute bars over the synthetic days"
    assert mismatches == []


def test_the_registry_has_the_candle_strategies():
    assert HOUR_LEVEL == [
        "buy_and_hold",
        "connors_rsi",
        "donchian_4h",
        "donchian_4h_r1",
        "ema_pullback",
        "macd_baseline",
        "noise_breakout",
    ]


# A cut lies 1 to 59 minutes past an hour, so the cut run has the bar that closed before it but not
# the next one: a decision about that bar made from the next one is in the full run only.
def test_a_strategy_that_reads_the_next_bar_fails_the_prefix_check(month):
    full, mismatches = _prefix_mismatches(_plan(month, "peek_probe"), 1)
    assert {k.reason for k in full} == {PEEK_REASON}
    [mismatch] = mismatches
    # The full run decided about the bar just before the cut; the cut run never saw the bar after it.
    assert mismatch.full is not None
    assert mismatch.cut is None


def _settings(tmp_path, month):
    return worker_settings(month, tmp_path / "reports", 1, lab_test_strategies=True)


def test_lookahead_check_fails_the_peeking_variant_and_writes_no_bundle(tmp_path, month, caplog):
    path = tmp_path / "run.toml"
    path.write_text(RUN.format(name="peek_probe"), encoding="utf-8")
    loaded = load_candle_run(path, Overrides(), test_strategies=True)
    store = MemoryStore()
    caplog.set_level(logging.INFO)

    assert backtest(_settings(tmp_path, month), loaded, store, lookahead_check=True) == 1

    assert store.bundles == []
    [folder] = list((tmp_path / "reports").iterdir())
    report = json.loads((folder / "base.json").read_text())
    assert report["status"] == "failed"
    assert report["error"]["type"] == "LookaheadMismatchError"
    assert "cut at 2026-02-" in report["error"]["message"]
    assert "lab_lookahead_mismatch" in [r.getMessage() for r in caplog.records]


def test_lookahead_check_passes_macd_baseline_and_writes_its_bundle(tmp_path, month, caplog):
    path = tmp_path / "run.toml"
    path.write_text(RUN.format(name="macd_baseline"), encoding="utf-8")
    loaded = load_candle_run(path, Overrides())
    store = MemoryStore()
    caplog.set_level(logging.INFO)

    assert backtest(_settings(tmp_path, month), loaded, store, lookahead_check=True) == 0

    [bundle] = store.bundles
    assert bundle.decisions
    assert "lab_lookahead_passed" in [r.getMessage() for r in caplog.records]
