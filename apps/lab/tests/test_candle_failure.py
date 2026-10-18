"""covers: spec 0014 AC-18 (a candle variant that raises, or whose worker dies, fails alone: no rows, a ``failed``
report, the others complete, exit 1; SIGTERM stops every unit, writes ``interrupted`` reports, and exits 130)."""

import json
import logging
import os
import signal
import threading
import time

import pytest

from lab.candle_backtest import backtest
from lab.candle_units import CandleUnitPlan, CandleUnitResult, run_candle_unit
from lab.runfile import Overrides, load_candle_run

from .candle_fixtures import synthetic_month
from .conftest import MemoryStore, worker_settings

RUN = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2026-02-01
to = 2026-02-02
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"

[strategy]
name = "macd_baseline"
[strategy.config]
notional_usd = "10000"
bar_spec = "15-MINUTE-LAST"
trend_ema = 20
[strategy.grid]
fast = [8, 12]
"""


class UnitBoomError(Exception):
    """The deliberate failure of ``raise_for_grid_1``."""


def raise_for_grid_1(plan: CandleUnitPlan) -> CandleUnitResult:
    """A unit that raises in its worker for ``grid-1`` only."""
    if plan.variant == "grid-1":
        raise UnitBoomError("grid-1 raised as configured")
    return run_candle_unit(plan)


def die_for_grid_1(plan: CandleUnitPlan) -> CandleUnitResult:
    """A worker that exits without a result for ``grid-1`` (killed, out of memory)."""
    if plan.variant == "grid-1":
        os._exit(3)
    return run_candle_unit(plan)


def hang(plan: CandleUnitPlan) -> CandleUnitResult:
    """A unit that never finishes on its own."""
    time.sleep(120)
    raise AssertionError("the parent should have stopped this worker")


@pytest.fixture
def loaded(tmp_path):
    synthetic_month(tmp_path / "catalog", days=2)
    path = tmp_path / "run.toml"
    path.write_text(RUN, encoding="utf-8")
    return load_candle_run(path, Overrides())


def _settings(tmp_path):
    return worker_settings(tmp_path / "catalog", tmp_path / "reports", 2)


def _report(tmp_path, name):
    (folder,) = list((tmp_path / "reports").iterdir())
    return folder, json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("target", "error_type"), [(raise_for_grid_1, "UnitBoomError"), (die_for_grid_1, "WorkerDiedError")]
)
def test_a_failed_unit_fails_only_its_variant(tmp_path, loaded, caplog, target, error_type):
    store = MemoryStore()

    with caplog.at_level(logging.ERROR):
        code = backtest(_settings(tmp_path), loaded, store, target=target)

    assert code == 1
    folder, failed = _report(tmp_path, "grid-1")
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == error_type
    assert failed["run_id"] is None
    assert not (folder / "grid-1.bundle.json").exists()
    _, other = _report(tmp_path, "grid-2")
    assert other["status"] == "completed"
    assert [b.run.run_config["variant"] for b in store.bundles] == ["grid-2"]
    index = (folder / "index.md").read_text(encoding="utf-8")
    assert "failed" in index
    assert "completed" in index
    assert any(r.getMessage() == "lab_variant_failed" for r in caplog.records)


def test_sigterm_stops_every_unit_writes_interrupted_reports_and_exits_130(tmp_path, loaded):
    store = MemoryStore()
    timer = threading.Timer(2.0, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        code = backtest(_settings(tmp_path), loaded, store, target=hang)
    finally:
        timer.cancel()

    assert code == 130
    assert time.monotonic() - started < 30
    folder, first = _report(tmp_path, "grid-1")
    assert first["status"] == "interrupted"
    assert _report(tmp_path, "grid-2")[1]["status"] == "interrupted"
    assert "interrupted" in (folder / "index.md").read_text(encoding="utf-8")
    assert store.bundles == []
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
