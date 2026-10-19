"""covers: spec 0015 AC-1 (code_hash over the strategy's trading_core closure, frozen_hash with config and engine)."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from trading_core.recording.rows import JsonObject
from trading_core.versioning import (
    DynamicImportError,
    UnknownStrategyModuleError,
    code_closure,
    code_hash,
    frozen_hash,
    package_root,
)

# Every strategy module the trader or the lab freezes.
STRATEGY_MODULES = (
    "trading_core.node.placeholder",
    "trading_core.node.random_taker",
    "trading_core.node.maker_probe",
    "trading_core.node.buy_and_hold",
    "trading_core.node.macd_baseline",
)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A fake installed ``trading_core`` under ``tmp_path``; returns the package folder."""
    root = tmp_path / "trading_core"
    for name, source in {"__init__.py": "", "node/__init__.py": "", **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def _paths(closure):
    return [path for path, _ in closure]


def test_the_closure_follows_trading_core_imports_and_skips_third_party(tmp_path):
    root = _tree(
        tmp_path,
        {
            "node/alpha.py": "import json\nimport msgspec\nfrom trading_core.strategy import rules\n",
            "strategy/__init__.py": "",
            "strategy/rules.py": "from trading_core.strategy.indicators import Ema\n",
            "strategy/indicators.py": "class Ema: ...\n",
            "unused.py": "",
        },
    )

    assert _paths(code_closure("trading_core.node.alpha", root)) == [
        "trading_core/__init__.py",
        "trading_core/node/__init__.py",
        "trading_core/node/alpha.py",
        "trading_core/strategy/__init__.py",
        "trading_core/strategy/indicators.py",
        "trading_core/strategy/rules.py",
    ]


def test_imports_inside_functions_and_relative_imports_count(tmp_path):
    root = _tree(
        tmp_path,
        {
            "node/alpha.py": "def later():\n    from . import helper\n    from ..risk import check\n",
            "node/helper.py": "",
            "risk/__init__.py": "",
            "risk/check.py": "",
        },
    )

    paths = _paths(code_closure("trading_core.node.alpha", root))

    assert "trading_core/node/helper.py" in paths
    assert "trading_core/risk/check.py" in paths


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\n",
        "from importlib import import_module\n",
        "def f():\n    return __import__('trading_core.x')\n",
    ],
)
def test_a_dynamic_import_anywhere_in_the_closure_is_refused(tmp_path, source):
    root = _tree(tmp_path, {"node/alpha.py": "from trading_core import sneaky\n", "sneaky.py": source})

    with pytest.raises(DynamicImportError):
        code_closure("trading_core.node.alpha", root)


def test_a_module_outside_node_or_without_source_is_refused(tmp_path):
    root = _tree(tmp_path, {"node/alpha.py": ""})

    with pytest.raises(UnknownStrategyModuleError):
        code_closure("trading_core.node.missing", root)
    with pytest.raises(UnknownStrategyModuleError):
        code_closure("lab.probes", root)


def test_the_same_closure_on_another_commit_gives_the_same_hash_and_one_edit_changes_it(tmp_path):
    files = {"node/alpha.py": "from trading_core.strategy import indicators\n", "strategy/indicators.py": "A = 1\n"}
    first = _tree(tmp_path / "commit_a", files)
    second = _tree(tmp_path / "commit_b", files | {"node/unrelated.py": "B = 2\n"})
    config: JsonObject = {"window": 20, "size": Decimal("0.001")}

    same = [
        frozen_hash(code_hash(code_closure("trading_core.node.alpha", r)), config, "1.231.0") for r in (first, second)
    ]
    assert same[0] == same[1]

    (second / "strategy/indicators.py").write_text("A = 2\n")
    edited = frozen_hash(code_hash(code_closure("trading_core.node.alpha", second)), config, "1.231.0")
    assert edited != same[0]


def test_config_and_the_nautilus_version_are_part_of_the_frozen_hash():
    code = "ab" * 32
    base = frozen_hash(code, {"window": 20}, "1.231.0")

    assert frozen_hash(code, {"window": 21}, "1.231.0") != base
    assert frozen_hash(code, {"window": 20}, "1.232.0") != base
    assert frozen_hash(code, {"window": 20}, "1.231.0") == base


@pytest.mark.parametrize("module", STRATEGY_MODULES)
def test_every_real_strategy_has_a_static_closure(module):
    closure = code_closure(module)

    assert f"{module.replace('.', '/')}.py" in _paths(closure)
    assert all(path.startswith("trading_core/") for path in _paths(closure))


def test_the_macd_closure_holds_its_indicators_and_not_the_versioning_module():
    paths = _paths(code_closure("trading_core.node.macd_baseline"))

    assert "trading_core/strategy/indicators.py" in paths
    assert "trading_core/versioning.py" not in paths


def test_the_installed_package_hashes_the_same_when_copied(tmp_path):
    copy = tmp_path / "trading_core"
    shutil.copytree(package_root(), copy, ignore=shutil.ignore_patterns("__pycache__"))

    installed = code_hash(code_closure("trading_core.node.macd_baseline"))
    copied = code_hash(code_closure("trading_core.node.macd_baseline", copy))

    assert installed == copied
