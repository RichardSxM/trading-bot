"""covers: spec 0025 AC-13 (both donchian scalps registered as candle screens in the scalping family, and their
two study files, each one ``base`` run on the shared settings).
"""

from decimal import Decimal

import pytest

from lab.candle_units import run_candle_unit
from lab.runfile import LoadedCandleRun
from lab.strategies import registry_entry
from lab.study import load_study
from trading_core.recording.enums import Intent, StrategyKind

from .test_intraday_trend_candidates import STUDIES, _month_with_empty_windows, _plan, _shared

D = Decimal
R1_EXITS = {"midline_exit", "time_stop", "stop_through", "range_end", "liquidated"}


@pytest.mark.parametrize("name", ["donchian_scalp", "donchian_scalp_r1"])
def test_both_are_candle_screens_in_the_scalping_family(name):
    entry = registry_entry(name)
    assert entry.kind == StrategyKind.SCALPING
    assert (entry.candle_screen, entry.candle_gate) == (True, False)
    assert entry.module == f"trading_core.node.{name}"
    assert entry.run_fields.__name__ == "_candle_fields"


@pytest.mark.parametrize(
    ("name", "extra"),
    [("donchian_scalp", {}), ("donchian_scalp_r1", {"ema_period": 200})],
)
def test_each_study_is_one_base_run_on_the_shared_settings(name, extra):
    reference = load_study(STUDIES / "connors_rsi_15m.toml")
    study = load_study(STUDIES / f"{name}.toml")

    assert _shared(study) == _shared(reference)
    assert (study.section.name, study.section.lab, study.section.seed) == (name, "candle", 42)
    assert isinstance(study.loaded, LoadedCandleRun)
    assert study.loaded.bar_spec == "5-MINUTE-LAST"
    config = dict(study.loaded.run.strategy.config)
    assert config == {
        "bar_spec": "5-MINUTE-LAST",
        "entry_bars": 20,
        "stop_atr": "3",
        "atr_period": 14,
        "atr_floor_mult": "1",
        "time_stop_bars": 60,
        **extra,
    }
    assert "spread_max_atr" not in config
    assert len(study.points) == 1


def test_r1_trades_on_5_minute_bars_through_the_candle_lab_with_an_empty_seed(tmp_path):
    _month_with_empty_windows(tmp_path / "catalog")
    result = run_candle_unit(_plan(tmp_path / "catalog", "donchian_scalp_r1", {"entry_bars": 2, "ema_period": 2}))

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    assert entries
    assert {d.reason for d in entries} <= {"donchian_long", "donchian_short"}
    assert {d.reason for d in decisions if d.intent == Intent.EXIT} <= R1_EXITS
    assert all({"ema", "midline"} <= set(d.inputs) for d in entries)
    assert all(d.risk_reason != "spread_wide" for d in decisions)  # the filter is off in the lab
