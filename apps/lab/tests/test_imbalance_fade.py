"""covers: spec 0021 AC-4 (``imbalance_fade`` registered as a maker scalping strategy in its own module, replay
only with no candle flags, listed in sorted order, its factory refusing a plain ``BookImbalanceConfig``), AC-1
and AC-9 (``book_imbalance``'s code closure stays free of the fade), AC-6 (no study file before the pre check
passes)."""

from pathlib import Path

import pytest

from lab.runfile import Overrides, RunFileError, load_candle_run
from lab.strategies import _CHECK_CONTEXT, REGISTRY, registry_entry
from trading_core.node.imbalance_fade import ImbalanceFade, ImbalanceFadeConfig
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import StrategyKind
from trading_core.strategy.blocks import EntryBlocks
from trading_core.versioning import code_closure

from .test_scalp_admission import CANDLE_RUN, _write

STUDIES = Path(__file__).resolve().parents[1] / "studies"


def test_imbalance_fade_is_a_maker_scalping_strategy_in_its_own_module():
    entry = registry_entry("imbalance_fade")

    assert (entry.kind, entry.default_entry_mode) == (StrategyKind.SCALPING, "maker")
    assert (entry.candle_screen, entry.candle_gate) == (False, False)
    assert entry.module == "trading_core.node.imbalance_fade"
    assert entry.config_cls is ImbalanceFadeConfig
    assert entry.user_fields() == registry_entry("book_imbalance").user_fields()


def test_the_registry_builds_an_imbalance_fade_from_its_config():
    entry = registry_entry("imbalance_fade")
    config = entry.build_config({"imb_min": "0.5", "target_bps": "9"}, _CHECK_CONTEXT)

    assert isinstance(entry.build(config, PauseGate(), EntryBlocks(), lambda item: None), ImbalanceFade)


def test_the_factory_refuses_a_plain_book_imbalance_config():
    config = registry_entry("book_imbalance").build_config({}, _CHECK_CONTEXT)

    with pytest.raises(TypeError, match="ImbalanceFadeConfig"):
        registry_entry("imbalance_fade").build(config, PauseGate(), EntryBlocks(), lambda item: None)


def test_the_candle_lab_refuses_it(tmp_path):
    with pytest.raises(RunFileError, match="scalping candidates with a candle screen"):
        load_candle_run(_write(tmp_path, CANDLE_RUN.replace("STRATEGY", "imbalance_fade")), Overrides())


def test_the_replay_scalping_strategies_list_it_in_sorted_order():
    names = sorted(
        name for name, entry in REGISTRY.items() if entry.kind == StrategyKind.SCALPING and not entry.candle_screen
    )

    assert names == [
        "book_imbalance",
        "delta_divergence",
        "imbalance_fade",
        "maker_probe",
        "placeholder_timed",
        "random_taker",
    ]


# covers: AC-1, AC-9 (the fade adds its own module without touching book_imbalance's frozen code)
def test_book_imbalances_code_closure_never_reaches_the_fade():
    parent = {path for path, _ in code_closure("trading_core.node.book_imbalance")}
    fade = {path for path, _ in code_closure("trading_core.node.imbalance_fade")}

    assert not {path for path in parent if "imbalance_fade" in path}
    assert fade - parent == {"trading_core/node/imbalance_fade.py", "trading_core/strategy/imbalance_fade.py"}


def test_no_study_file_exists_before_the_pre_check_passes():
    assert not (STUDIES / "imbalance_fade.toml").exists()
