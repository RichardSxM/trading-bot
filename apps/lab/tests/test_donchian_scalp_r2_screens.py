"""covers: spec 0026 AC-17 (``donchian_scalp_r2`` registered as a candle screen in the scalping family, and its two
study files, each r1's matching file with the r2 name and the four maker lines spelled out).
"""

import tomllib
from decimal import Decimal

import pytest

from lab.candle_units import run_candle_unit
from lab.strategies import registry_entry
from lab.study import load_study
from trading_core.recording.enums import Intent, StrategyKind

from .test_intraday_trend_candidates import STUDIES, _month_with_empty_windows, _plan

D = Decimal
MAKER_LINES = {"entry_offset_atr": "0.5", "entry_wait_bars": 3, "exit_wait_bars": 3, "atr_min_bps": "10"}
R2_EXITS = {"midline_exit", "time_stop", "stop_through", "late_entry_fill", "range_end", "liquidated"}


def test_r2_is_a_candle_screen_in_the_scalping_family():
    entry = registry_entry("donchian_scalp_r2")
    assert entry.kind == StrategyKind.SCALPING
    assert (entry.candle_screen, entry.candle_gate) == (True, False)
    assert entry.module == "trading_core.node.donchian_scalp_r2"
    assert entry.run_fields.__name__ == "_candle_fields"


@pytest.mark.parametrize(
    ("r2", "r1"), [("donchian_scalp_r2", "donchian_scalp_r1"), ("donchian_scalp_r2_pepe", "donchian_scalp_r1_pepe")]
)
def test_each_study_is_r1_s_file_with_the_r2_name_and_the_maker_lines(r2, r1):
    with (STUDIES / f"{r2}.toml").open("rb") as f:
        mine = tomllib.load(f)
    with (STUDIES / f"{r1}.toml").open("rb") as f:
        theirs = tomllib.load(f)

    assert mine["strategy"]["name"] == "donchian_scalp_r2"
    assert mine["study"] == {**theirs["study"], "name": r2}
    assert mine["strategy"]["config"] == {**theirs["strategy"]["config"], **MAKER_LINES}
    assert "spread_max_atr" not in mine["strategy"]["config"]
    rest = {key: value for key, value in mine.items() if key not in ("strategy", "study")}
    assert rest == {key: value for key, value in theirs.items() if key not in ("strategy", "study")}
    study = load_study(STUDIES / f"{r2}.toml")
    assert (study.section.name, study.section.lab, len(study.points)) == (r2, "candle", 1)


def test_r2_trades_maker_limits_through_the_candle_lab_with_an_empty_seed(tmp_path):
    _month_with_empty_windows(tmp_path / "catalog")
    config = {"entry_bars": 2, "ema_period": 2, "atr_min_bps": "0"}
    result = run_candle_unit(_plan(tmp_path / "catalog", "donchian_scalp_r2", config))

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    assert entries
    assert {d.reason for d in entries} <= {"donchian_long", "donchian_short"}
    assert {d.reason for d in decisions if d.intent == Intent.EXIT} <= R2_EXITS
    assert all({"ema", "midline", "limit_px"} <= set(d.inputs) for d in entries if d.risk_reason is None)
    orders = [o for _, o in result.orders]
    entry_orders = [o for o in orders if not o.reduce_only and o.order_type == "limit"]
    assert entry_orders
    assert all(o.post_only for o in entry_orders)
