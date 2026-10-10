"""covers: spec 0008 AC-12 (the same inputs give the same reports and rows, with 1 or 2 workers), AC-6 and
AC-16 (variants are written in plan order whatever order their units finish in)."""

import json
import re
import time
from pathlib import Path

from lab.replay import replay
from lab.runfile import Overrides, load_run
from lab.units import UnitPlan, UnitResult, run_unit
from trading_core.lab.variants import BASE, expand_variants

from .conftest import FIXTURES, MemoryStore, worker_settings

RUN_FILE = Path(__file__).parents[1] / "runs" / "random_taker_own.toml"
# What may differ between two runs of the same inputs (AC-12): ids and wall clock stamps.
_VARYING = {
    "run_id",
    "batch_id",
    "started_at",
    "ended_at",
    "computed_at",
    "id",
    "decision_id",
    "position_id",
    "strategy_version_id",
    "event_id",
}


def slow_base(plan: UnitPlan) -> UnitResult:
    """``run_unit``, but ``base`` starts late, so the other variant finishes first."""
    if plan.variant == BASE:
        time.sleep(3)
    return run_unit(plan)


# An id inside a value, such as the decision id in a risk event's ``event_key``.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _without_varying(value):
    if isinstance(value, dict):
        return {k: _without_varying(v) for k, v in value.items() if k not in _VARYING}
    if isinstance(value, list):
        return [_without_varying(v) for v in value]
    if isinstance(value, str):
        return _UUID.sub("<id>", value)
    return value


def _differences(a, b, path: str = "") -> list[str]:
    """Every path where ``a`` and ``b`` differ, with both values, for a readable failure."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(a.keys() | b.keys()) for d in _differences(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for n, (x, y) in enumerate(zip(a, b, strict=True)) for d in _differences(x, y, f"{path}[{n}]")]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def _batch_files(reports: Path):
    (folder,) = list(reports.iterdir())
    return {
        p.name: _without_varying(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(folder.iterdir())
        if p.suffix == ".json"
    }


def test_one_and_two_workers_give_the_same_reports_and_rows_and_write_in_variant_order(tmp_path):
    loaded = load_run(RUN_FILE, Overrides())
    variants = expand_variants(25, None, queue_bound=True)
    one, two = MemoryStore(), MemoryStore()

    assert replay(worker_settings(FIXTURES, tmp_path / "one", 1), loaded, one, variants=variants) == 0
    assert replay(worker_settings(FIXTURES, tmp_path / "two", 2), loaded, two, variants=variants, target=slow_base) == 0

    first, second = _batch_files(tmp_path / "one"), _batch_files(tmp_path / "two")
    assert sorted(first) == [
        "base.bundle.json",
        "base.json",
        "queue-optimistic.bundle.json",
        "queue-optimistic.json",
    ]
    assert _differences(first, second) == []
    assert first["base.json"]["results"]["trade_count"] > 0
    assert [b.run.run_config["variant"] for b in two.bundles] == ["base", "queue=optimistic"]
