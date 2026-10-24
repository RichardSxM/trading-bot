"""covers: spec 0019 AC-9 (each intraday swing study file: the shared settings unchanged, its config and grid)
and 0019.1 (``connors_rsi_15m`` sets 15 minute bars and a 12 bar time stop)."""

from pathlib import Path

import pytest

from lab.study import load_study

STUDIES = Path(__file__).resolve().parents[1] / "studies"
# The hour level study whose shared settings every spec 0019 study keeps (spec 0018 Shared study settings).
SHARED_FROM = "connors_rsi.toml"
INTRADAY = ["connors_rsi_15m.toml"]


def _shared(study):
    return {k: v for k, v in study.run_file.items() if k != "strategy"}


@pytest.mark.parametrize("name", INTRADAY)
def test_each_intraday_study_keeps_the_shared_hour_level_settings(name):
    reference = load_study(STUDIES / SHARED_FROM)
    study = load_study(STUDIES / name)

    assert _shared(study) == _shared(reference)
    assert study.section.lab == "candle"
    assert study.section.seed == 42
    assert study.loaded.bar_spec == "15-MINUTE-LAST"
    assert 4 <= len(study.points) <= 6


def test_connors_rsi_15m_is_the_1_hour_rule_on_15_minute_bars_with_a_3_hour_time_stop():
    study = load_study(STUDIES / "connors_rsi_15m.toml")

    assert study.section.name == "connors_rsi_15m"
    assert study.loaded.run.strategy.name == "connors_rsi"
    config = dict(study.loaded.run.strategy.config)
    assert config["time_stop_bars"] == 12
    assert config["bar_spec"] == "15-MINUTE-LAST"
    assert sorted((p["rsi_period"], p["rsi_lower"]) for _, p in study.points) == [
        (2, 5),
        (2, 10),
        (3, 5),
        (3, 10),
        (4, 5),
        (4, 10),
    ]
