"""covers: spec 0008 Code placement, the core boundary (Build plan task 21).

Only ``node/`` and ``catalog/`` carry the ``node`` extra: ``nautilus_trader`` is allowed in those
two, ``pyarrow`` and ``numpy`` only in ``catalog/``. The pure ``alerts/`` folder (spec 0013)
imports no I/O package and, of core, only itself, ``strategy``, and ``secrets`` (the redaction
registry every push text passes). The pure ``reconcile/`` folder (spec 0023) imports, of core, only
itself and the recording enums. The files are parsed, never imported, so the check runs on any
host.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "trading_core"

# top level package -> the core folders allowed to import it
ALLOWED = {
    "nautilus_trader": {"node", "catalog"},
    "pyarrow": {"catalog"},
    "numpy": {"catalog"},
}


# pure core folder -> the trading_core modules it may import (its own folder always)
PURE = {
    "alerts": {"trading_core.strategy", "trading_core.secrets"},
    "reconcile": {"trading_core.recording.enums"},
}
# packages that mean I/O, a framework, or an ORM
IO = {
    "asyncio",
    "httpx",
    "logging",
    "nautilus_trader",
    "os",
    "pathlib",
    "psycopg",
    "redis",
    "socket",
    "sqlalchemy",
    "subprocess",
}


def imports(path):
    """Each absolute import in ``path`` as (line, module name)."""
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def impure(root):
    """One line per import in a pure folder of an I/O package or a core module outside its list."""
    found = []
    for folder, allowed in PURE.items():
        for path in sorted((root / folder).rglob("*.py")):
            relative = path.relative_to(root)
            for line, name in imports(path):
                top = name.split(".")[0]
                core = top == "trading_core" and not any(
                    name == ok or name.startswith(ok + ".") for ok in {f"trading_core.{folder}", *allowed}
                )
                if top in IO or core:
                    found.append(f"{relative}:{line} imports {name}")
    return found


def violations(root):
    """One line per import of a guarded package outside the folders allowed to import it."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        folder = relative.parts[0] if len(relative.parts) > 1 else ""
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in ALLOWED and folder not in ALLOWED[top]:
                    found.append(f"{relative}:{node.lineno} imports {name}")
    return found


def test_only_the_adapter_folders_import_the_node_extra():
    assert SRC.is_dir()
    assert violations(SRC) == []


def test_the_check_catches_a_module_that_breaks_the_rule(tmp_path):
    (tmp_path / "node").mkdir()
    (tmp_path / "marketdata").mkdir()
    (tmp_path / "node" / "ok.py").write_text("from nautilus_trader.model.data import TradeTick\n")
    (tmp_path / "node" / "bad.py").write_text("import pyarrow.parquet as pq\n")
    (tmp_path / "marketdata" / "bad.py").write_text("def f():\n    import numpy as np\n    return np\n")
    (tmp_path / "top.py").write_text("from nautilus_trader.core import nautilus_pyo3\n")
    assert violations(tmp_path) == [
        "marketdata/bad.py:2 imports numpy",
        "node/bad.py:1 imports pyarrow.parquet",
        "top.py:1 imports nautilus_trader.core",
    ]


def test_the_pure_alerts_folder_stays_free_of_io():
    assert (SRC / "alerts").is_dir()
    assert impure(SRC) == []


def test_the_purity_check_catches_io_and_other_core_modules(tmp_path):
    (tmp_path / "alerts").mkdir()
    (tmp_path / "alerts" / "ok.py").write_text(
        "from trading_core.alerts.model import Tier\nfrom trading_core.secrets import redact\n"
    )
    (tmp_path / "alerts" / "bad.py").write_text("import redis\nfrom trading_core.recording.models import Alert\n")
    assert impure(tmp_path) == [
        "alerts/bad.py:1 imports redis",
        "alerts/bad.py:2 imports trading_core.recording.models",
    ]


# Spec 0015 AC-23: the gate imports only the standard library (no I/O or clock module) and itself.
GATE_FORBIDDEN_STDLIB = IO | {"time", "shutil", "sqlite3", "urllib", "http", "importlib"}


def gate_impure(root):
    """One line per import in ``gate/`` of a non stdlib package, an I/O or clock module, or another core module."""
    found = []
    for path in sorted((root / "gate").rglob("*.py")):
        relative = path.relative_to(root)
        for line, name in imports(path):
            top = name.split(".")[0]
            if top == "trading_core":
                ok = name == "trading_core.gate" or name.startswith("trading_core.gate.")
            else:
                ok = top in sys.stdlib_module_names and top not in GATE_FORBIDDEN_STDLIB
            if not ok:
                found.append(f"{relative}:{line} imports {name}")
    return found


def test_the_gate_imports_only_the_stdlib_and_itself():
    assert (SRC / "gate").is_dir()
    assert gate_impure(SRC) == []


def test_the_gate_check_catches_third_party_io_and_other_core_modules(tmp_path):
    (tmp_path / "gate").mkdir()
    (tmp_path / "gate" / "ok.py").write_text("import math\nfrom trading_core.gate.series import sharpe\n")
    (tmp_path / "gate" / "bad.py").write_text(
        "import pydantic\nimport time\nfrom trading_core.recording.rows import canonical_json\n"
    )
    assert gate_impure(tmp_path) == [
        "gate/bad.py:1 imports pydantic",
        "gate/bad.py:2 imports time",
        "gate/bad.py:3 imports trading_core.recording.rows",
    ]
