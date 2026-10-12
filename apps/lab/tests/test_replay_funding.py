"""covers: spec 0009 AC-14 (own replays apply their recorded funding, all or nothing)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid7

import pyarrow.parquet as pq
import pytest
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.catalog import FUNDING_TABLE, SourceTree
from lab.replay import Batch, _report, _run_config, _unit_plans, run_funding
from lab.report import report_markdown
from lab.runfile import Overrides, load_run
from trading_core.lab.segments import Segment, SegmentPlan
from trading_core.lab.variants import BASE, VariantSpec

from .conftest import H17, HOUR, lab_settings

INSTRUMENT = "PF_XBTUSD.KRAKEN"
RUN_FILE = Path(__file__).parents[1] / "runs" / "random_taker_own.toml"
H18 = H17 + HOUR
MINUTE = 60_000_000_000
# One segment over 17:00 to 18:59: 18:00 is its only boundary.
PLAN = SegmentPlan(segments=(Segment(1, (H17, H18)),), skipped=())


def _tree(tmp_path, *rate_ts):
    tree = SourceTree(tmp_path / "own", INSTRUMENT)
    iid = InstrumentId.from_str(INSTRUMENT)
    by_hour: dict[int, list[Any]] = {}
    for n, ts in enumerate(rate_ts):
        by_hour.setdefault(ts - ts % HOUR, []).append(FundingRateUpdate(iid, Decimal(n + 1) / 10_000, ts, ts))
    for hour, rows in by_hour.items():
        path = tree.hour_file(FUNDING_TABLE, hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(ArrowSerializer.serialize_batch(rows, data_cls=FundingRateUpdate), path)
    return tree


DAY = datetime.fromtimestamp(H17 // 1_000_000_000, UTC).date()


def test_own_rates_covering_every_boundary_apply_as_own(tmp_path):
    tree = _tree(tmp_path, H17 + 10 * MINUTE, H18 - MINUTE)

    rates, source, skipped = run_funding(tree, "own", [DAY], PLAN)

    assert source == "own"
    assert skipped is None
    assert [r.rate for r in rates] == [Decimal("0.0001"), Decimal("0.0002")]


def test_an_own_boundary_without_a_rate_from_the_period_before_applies_none(tmp_path):
    tree = _tree(tmp_path, H18 - HOUR)  # exactly one period old: too old

    assert run_funding(tree, "own", [DAY], PLAN) == ((), "none", H18)


def test_an_own_run_without_any_rate_applies_none(tmp_path):
    assert run_funding(_tree(tmp_path), "own", [DAY], PLAN) == ((), "none", H18)


def test_a_tardis_run_always_applies_its_rates(tmp_path):
    tree = _tree(tmp_path, H18 - 2 * HOUR)

    rates, source, skipped = run_funding(tree, "tardis", [DAY], PLAN)

    assert (len(rates), source, skipped) == (1, "tardis", None)


@pytest.fixture
def batch(tmp_path):
    loaded = load_run(RUN_FILE, Overrides())
    settings = lab_settings(tmp_path, tmp_path / "reports")
    return Batch(
        batch_id=uuid7(),
        loaded=loaded,
        settings=settings,
        plan=PLAN,
        version_id=uuid7(),
        folder=tmp_path / "reports" / "batch",
    )


def test_the_funding_source_decides_sampling_the_config_and_the_label(batch):
    variant = VariantSpec(name=BASE, latency_ms=25, queue_position=True)
    now = datetime.now(UTC)
    own = replace(batch, funding_source="own")

    (plan,) = _unit_plans(own, variant)
    report = _report(own, variant, started_at=now, ended_at=now)

    assert plan.funding is True
    assert _run_config(own, variant)["funding_source"] == "own"
    assert report.before_funding is False
    assert "funding skipped" not in report_markdown(report)


def test_a_skipped_run_keeps_the_label_and_says_why(batch):
    variant = VariantSpec(name=BASE, latency_ms=25, queue_position=True)
    now = datetime.now(UTC)
    skipped = replace(batch, funding_source="none", funding_skipped_ns=H18)

    (plan,) = _unit_plans(skipped, variant)
    report = _report(skipped, variant, started_at=now, ended_at=now)

    assert plan.funding is False
    assert _run_config(skipped, variant)["funding_source"] == "none"
    assert report.before_funding is True
    assert "- funding skipped: no rate before 2026-10-08 18:00 UTC" in report_markdown(report)


def test_run_config_holds_the_context_values(batch):
    """covers: spec 0009 AC-13 (replay results never depend on the laptop's environment)."""
    config = _run_config(batch, VariantSpec(name=BASE, latency_ms=25, queue_position=True))

    assert config["context"] == {
        "warmup_s": 7_200,
        "max_age_mark_s": 30,
        "max_age_index_s": 30,
        "max_age_open_interest_s": 30,
        "max_age_funding_s": 7_200,
        "liquidation_window_s": 60,
        "liquidations_available": True,
    }
    (plan,) = _unit_plans(batch, VariantSpec(name=BASE, latency_ms=25, queue_position=True))
    assert (plan.context_warmup_s, plan.market.liquidations_available) == (7_200, True)
