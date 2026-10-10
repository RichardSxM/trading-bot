"""covers: spec 0008 AC-17 (the lab imports no Kraken adapter, so it has no Kraken execution client factory)."""

import subprocess
import sys


def test_the_lab_loads_no_kraken_adapter():
    code = (
        "import sys, lab.main, lab.replay, lab.units, lab.pool, lab.tardis_import\n"
        "print(sorted(m for m in sys.modules if m.startswith('nautilus_trader.adapters.kraken')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "[]"
