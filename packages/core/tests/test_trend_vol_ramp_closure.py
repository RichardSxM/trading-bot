"""covers: spec 0034 AC-9 (every existing strategy hash is unchanged).

``trend_vol_ramp`` adds two modules and edits three shared ``trading_core`` files (the stop query and the alert
rule table). A strategy's ``code_hash`` covers exactly its closure, so no other strategy's hash can change as
long as none of these files is in its closure; ``trend_vol_ramp`` itself is the one closure that holds them.
"""

import ast

import pytest

from trading_core.versioning import code_closure, package_root

TOUCHED = frozenset(
    {
        "trading_core/node/trend_vol_ramp.py",
        "trading_core/strategy/trend_vol_ramp.py",
        "trading_core/recording/queries.py",
        "trading_core/alerts/rules.py",
        "trading_core/alerts/promises.py",
    }
)


def _strategy_modules():
    # Every ``trading_core.node`` module that names a strategy (a module level ``STRATEGY_NAME``).
    names = []
    for path in sorted((package_root() / "node").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            if any(isinstance(t, ast.Name) and t.id == "STRATEGY_NAME" for t in targets):
                names.append(f"trading_core.node.{path.stem}")
                break
    return names


def test_the_strategy_list_is_found():
    modules = _strategy_modules()
    assert "trading_core.node.trend_vol" in modules
    assert "trading_core.node.trend_vol_ramp" in modules
    assert len(modules) > 20


@pytest.mark.parametrize("module", [m for m in _strategy_modules() if m != "trading_core.node.trend_vol_ramp"])
def test_no_other_strategy_closure_holds_a_file_this_feature_touched(module):
    paths = {path for path, _ in code_closure(module)}

    assert not paths & TOUCHED


def test_the_ramp_closure_holds_its_own_modules_and_trend_vols():
    paths = {path for path, _ in code_closure("trading_core.node.trend_vol_ramp")}

    assert {"trading_core/node/trend_vol_ramp.py", "trading_core/strategy/trend_vol_ramp.py"} <= paths
    assert "trading_core/node/trend_vol.py" in paths
    assert not paths & {"trading_core/recording/queries.py", "trading_core/alerts/rules.py"}
