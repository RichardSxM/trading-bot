"""covers: spec 0008 AC-13 (a strategy that raises, or a worker that dies, fails only its own variant: no rows,
a ``failed`` report, the other variants complete, exit 1; SIGTERM stops every unit and exits 130)."""

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from lab.replay import replay
from lab.runfile import Overrides, RunFileError, load_run
from lab.units import UnitPlan, UnitResult, run_unit
from trading_core.lab.variants import expand_variants

from .conftest import FIXTURES, H17, HOUR, MemoryStore, worker_settings

MAKER_FILE = Path(__file__).parents[1] / "runs" / "maker_probe.toml"
OWN_FILE = Path(__file__).parents[1] / "runs" / "random_taker_own.toml"


def die_when_optimistic(plan: UnitPlan) -> UnitResult:
    """A worker that exits without a result for the ``queue=optimistic`` variant (killed, out of memory)."""
    if plan.variant == "queue=optimistic":
        os._exit(3)
    return run_unit(plan)


def hang(plan: UnitPlan) -> UnitResult:
    """A unit that never finishes on its own."""
    time.sleep(120)
    raise AssertionError("the parent should have stopped this worker")


def _raise_probe_file(tmp_path: Path) -> Path:
    """``maker_probe.toml`` over the own fixture day, as ``raise_probe`` raising in the second hour."""
    text = MAKER_FILE.read_text(encoding="utf-8")
    text = text.replace('source = "tardis"', 'source = "own"')
    text = text.replace("2026-09-01", "2026-10-08")
    text = text.replace('name = "maker_probe"', 'name = "raise_probe"')
    text += f"raise_at_ns = {H17 + HOUR}\n"
    path = tmp_path / "raise_probe.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _report(folder: Path, name: str):
    return json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


def test_a_strategy_that_raises_fails_only_its_variant(tmp_path, caplog):
    loaded = load_run(_raise_probe_file(tmp_path), Overrides(), test_strategies=True)
    settings = worker_settings(FIXTURES, tmp_path / "reports", 2, lab_test_strategies=True)
    store = MemoryStore()

    with caplog.at_level(logging.ERROR):
        code = replay(settings, loaded, store, variants=expand_variants(25, "maker", pair_entry=True))

    assert code == 1
    (folder,) = list((tmp_path / "reports").iterdir())
    base, taker = _report(folder, "base"), _report(folder, "entry-taker")
    assert base["status"] == "failed"
    assert base["error"]["type"] == "ProbeError"
    assert base["run_id"] is None
    assert not (folder / "base.bundle.json").exists()
    assert taker["status"] == "completed"
    assert [b.run.run_config["variant"] for b in store.bundles] == ["entry=taker"]
    index = (folder / "index.md").read_text(encoding="utf-8")
    assert "[base](base.md) | failed" in index
    assert "[entry=taker](entry-taker.md) | completed" in index
    assert any(r.getMessage() == "lab_variant_failed" for r in caplog.records)


def test_raise_probe_is_unknown_without_the_test_switch(tmp_path):
    with pytest.raises(RunFileError, match="raise_probe"):
        load_run(_raise_probe_file(tmp_path), Overrides())


def test_a_worker_that_dies_fails_only_its_variant(tmp_path):
    loaded = load_run(OWN_FILE, Overrides())
    store = MemoryStore()

    code = replay(
        worker_settings(FIXTURES, tmp_path, 2),
        loaded,
        store,
        variants=expand_variants(25, None, queue_bound=True),
        target=die_when_optimistic,
    )

    assert code == 1
    (folder,) = list(tmp_path.iterdir())
    failed = _report(folder, "queue-optimistic")
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "WorkerDiedError"
    assert "exit code 3" in failed["error"]["message"]
    assert _report(folder, "base")["status"] == "completed"
    assert [b.run.run_config["variant"] for b in store.bundles] == ["base"]


def test_sigterm_stops_every_unit_writes_interrupted_reports_and_exits_130(tmp_path):
    loaded = load_run(OWN_FILE, Overrides())
    store = MemoryStore()
    timer = threading.Timer(2.0, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        code = replay(
            worker_settings(FIXTURES, tmp_path, 2),
            loaded,
            store,
            variants=expand_variants(25, None, queue_bound=True),
            target=hang,
        )
    finally:
        timer.cancel()

    assert code == 130
    assert time.monotonic() - started < 30
    (folder,) = list(tmp_path.iterdir())
    assert _report(folder, "base")["status"] == "interrupted"
    assert _report(folder, "queue-optimistic")["status"] == "interrupted"
    assert "interrupted" in (folder / "index.md").read_text(encoding="utf-8")
    assert store.bundles == []
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
