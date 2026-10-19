"""covers: spec 0014 AC-17 (the same candle run gives the same reports and rows with 1 or 2 workers) and AC-7
(variants are written in plan order whatever order their units finish in)."""

import time

from lab.candle_backtest import backtest
from lab.candle_units import CandleUnitPlan, CandleUnitResult, run_candle_unit
from lab.runfile import Overrides, load_candle_run

from .candle_fixtures import synthetic_month
from .conftest import MemoryStore, worker_settings
from .test_determinism import _batch_files, _differences

RUN = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2026-02-01
to = 2026-02-03
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
name = "macd_baseline"
[strategy.config]
notional_usd = "10000"
bar_spec = "15-MINUTE-LAST"
trend_ema = 20
[strategy.grid]
fast = [8, 12]
slow = [21, 26]
"""


def slow_first(plan: CandleUnitPlan) -> CandleUnitResult:
    """``run_candle_unit``, but ``grid-1`` starts late, so later variants finish first."""
    if plan.variant == "grid-1":
        time.sleep(3)
    return run_candle_unit(plan)


def test_one_and_two_workers_give_the_same_reports_and_rows_and_write_in_plan_order(tmp_path):
    synthetic_month(tmp_path / "catalog", days=3)
    path = tmp_path / "run.toml"
    path.write_text(RUN, encoding="utf-8")
    loaded = load_candle_run(path, Overrides())
    one, two = MemoryStore(), MemoryStore()

    assert backtest(worker_settings(tmp_path / "catalog", tmp_path / "one", 1), loaded, one) == 0
    assert backtest(worker_settings(tmp_path / "catalog", tmp_path / "two", 2), loaded, two, target=slow_first) == 0

    first, second = _batch_files(tmp_path / "one"), _batch_files(tmp_path / "two")
    names = [f"grid-{n}" for n in range(1, 5)]
    assert sorted(first) == sorted([f"{n}.json" for n in names] + [f"{n}.bundle.json" for n in names])
    assert _differences(first, second) == []
    assert first["grid-1.json"]["results"]["trade_count"] > 0
    assert [b.run.run_config["variant"] for b in two.bundles] == names
    assert [b.run.run_config["variant"] for b in one.bundles] == names
