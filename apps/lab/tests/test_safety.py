"""covers: spec 0008 AC-17 (the lab imports no Kraken adapter, so it has no Kraken execution client factory) and
spec 0014 AC-22 (``candle-parity`` refuses live and never connects to Postgres)."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.main import main
from lab.settings import LabSettings

PARITY_FILE = Path(__file__).parents[1] / "runs" / "macd_parity.toml"


def test_the_lab_loads_no_kraken_adapter():
    code = (
        "import sys, lab.main, lab.replay, lab.units, lab.pool, lab.tardis_import, lab.candle_backtest,"
        " lab.candle_parity\n"
        "print(sorted(m for m in sys.modules if m.startswith('nautilus_trader.adapters.kraken')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "[]"


def _exit_code(argv):
    with pytest.raises(SystemExit) as raised:
        main(argv)
    return raised.value.code


def test_candle_parity_refuses_live_with_exit_78(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")

    assert _exit_code(["candle-parity", str(PARITY_FILE)]) == 78


def test_candle_parity_boots_without_the_postgres_secret_and_never_connects(monkeypatch):
    seen = {}

    def boot(*args, **kwargs):
        seen["secrets"] = kwargs.get("secrets", ())
        return SimpleNamespace(settings=LabSettings(trading_env="demo"), secrets={})

    monkeypatch.setattr("lab.main.boot", boot)
    monkeypatch.setattr("lab.main.create_engine", lambda *a, **k: pytest.fail("connected to Postgres"))
    monkeypatch.setattr("lab.main.candle_parity", lambda settings, loaded: 0)

    assert _exit_code(["candle-parity", str(PARITY_FILE)]) == 0
    assert seen["secrets"] == ()


def test_candle_parity_refuses_a_grid_with_exit_64(tmp_path, capsys):
    path = tmp_path / "grid.toml"
    path.write_text(
        PARITY_FILE.read_text(encoding="utf-8").replace("[parity]", "[strategy.grid]\nfast = [8, 12]\n\n[parity]")
    )

    assert _exit_code(["candle-parity", str(path)]) == 64
    assert "remove the grid" in capsys.readouterr().err
