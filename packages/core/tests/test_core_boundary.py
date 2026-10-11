"""covers: spec 0008 Code placement, the core boundary (Build plan task 21).

Only ``node/`` and ``catalog/`` carry the ``node`` extra: ``nautilus_trader`` is allowed in those
two, ``pyarrow`` and ``numpy`` only in ``catalog/``. The files are parsed, never imported, so the
check runs on any host.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "trading_core"

# top level package -> the core folders allowed to import it
ALLOWED = {
    "nautilus_trader": {"node", "catalog"},
    "pyarrow": {"catalog"},
    "numpy": {"catalog"},
}


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
