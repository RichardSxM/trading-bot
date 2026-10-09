"""covers: spec 0008 AC-3 (missing days exit 2, nothing written), AC-4 and AC-7 (the fixture's back to back hours as one
segment, S<n> order tags), AC-5 (fees, latency, quotes), AC-9 (funding samples and payments), AC-11 (bundle and
reports), AC-14 (the band on real data).

The fixture is 17:55 to 18:05 UTC of real own recorder data (see fixtures/make_fixtures.py).
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from uuid import uuid7

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from lab.catalog import SourceTree
from lab.replay import BASE, Batch, Variant, _unit_plans, build_bundle, plan_run, replay
from lab.runfile import LoadedRun, Overrides, load_run
from lab.units import UNIT_RUN_ID, UnitResult, run_unit
from trading_core.lab.errors import FundingRateMissingError
from trading_core.lab.funding import FundingRate, FundingSample, PositionKey
from trading_core.recording import models
from trading_core.recording.enums import FundingSource, Intent, Liquidity, PositionSide, PositionStatus
from trading_core.recording.mappers import decision_id
from trading_core.secrets import POSTGRES_PASSWORD, read_secret
from trading_core.settings import Settings

from .conftest import FIXTURES, H17, HOUR, lab_settings

RUN_FILE = Path(__file__).parents[1] / "runs" / "random_taker_own.toml"
H18 = H17 + HOUR
DATA_END = H18 + 5 * 60 * 1_000_000_000


def _boom() -> Session:
    raise AssertionError("nothing may touch the database here")


@pytest.fixture(scope="module")
def loaded() -> LoadedRun:
    return load_run(RUN_FILE, Overrides())


@pytest.fixture(scope="module")
def batch(loaded, tmp_path_factory) -> Batch:
    settings = lab_settings(FIXTURES, tmp_path_factory.mktemp("reports"))
    plan = plan_run(SourceTree(FIXTURES / "own", loaded.run.instrument_id), loaded)
    return Batch(
        batch_id=uuid7(),
        loaded=loaded,
        settings=settings,
        plan=plan,
        version_id=uuid7(),
        folder=settings.lab_reports_dir / "batch",
    )


@pytest.fixture(scope="module")
def unit(batch) -> UnitResult:
    (plan,) = _unit_plans(batch, Variant(name=BASE, latency_ms=25, queue_position=True))
    return run_unit(plan)


def test_a_missing_day_exits_2_and_writes_nothing(tmp_path, loaded):
    (tmp_path / "own").mkdir()
    settings = lab_settings(tmp_path, tmp_path / "reports")

    assert replay(settings, loaded, _boom) == 2
    assert not (tmp_path / "reports").exists()


def test_a_tardis_range_without_a_first_of_month_day_exits_2(tmp_path, loaded):
    run = loaded.run.model_copy(update={"source": "tardis", "from_": date(2026, 9, 2), "to": date(2026, 9, 30)})
    settings = lab_settings(tmp_path, tmp_path / "reports")

    assert replay(settings, LoadedRun(path="x", sha256="0" * 64, run=run), _boom) == 2


def test_the_fixture_is_one_segment_across_the_hour_boundary(batch):
    plan = batch.plan

    assert [s.hours for s in plan.segments] == [(H17, H18)]
    assert plan.skipped_by_reason() == {"missing": 22}


def test_the_strategy_trades_on_both_sides_of_the_hour_boundary_and_stops_with_the_data(unit):
    entries = [row for _, row in unit.decisions if row.intent == Intent.ENTER]

    assert any(d.decided_ns < H18 for d in entries)
    assert any(d.decided_ns >= H18 for d in entries)
    assert all(row.filled_ns <= DATA_END for _, row in unit.fills)
    assert unit.last_ts_init is not None
    assert unit.last_ts_init < DATA_END


def test_order_ids_carry_the_segment_tag_and_every_order_its_decision(unit):
    decisions = {row.id for _, row in unit.decisions}

    for _, order in unit.orders:
        assert "-S1-" in order.client_order_id
        assert order.decision_id in decisions
        assert order.client_order_id in unit.spreads
    assert all(row.run_id == UNIT_RUN_ID for _, row in unit.fills)


def test_taker_fills_pay_the_taker_fee_rounded_to_cents(unit, loaded):
    fills = [row for _, row in unit.fills]

    assert fills
    for fill in fills:
        assert fill.liquidity == Liquidity.TAKER
        expected = (loaded.run.taker_fee * fill.quantity * fill.price).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        assert abs(fill.fee - expected) <= Decimal("0.01")


def test_entries_fill_25_ms_after_the_decision(unit):
    decided = {row.id: row.decided_ns for _, row in unit.decisions}
    orders = {row.client_order_id: row for _, row in unit.orders}

    for _, fill in unit.fills:
        order = orders[fill.client_order_id]
        assert order.decision_id is not None
        assert fill.filled_ns >= decided[order.decision_id] + 25_000_000


def test_the_bundle_stamps_one_run_id_and_scores_the_variant(batch, unit):
    now = datetime.now(UTC)
    variant = Variant(name=BASE, latency_ms=25, queue_position=True)

    bundle, results, band = build_bundle(batch, variant, [unit], started_at=now, ended_at=now)

    run_id = bundle.run.id
    run_ids = {row.run_id for row in bundle.decisions} | {row.run_id for row in bundle.orders}
    run_ids |= {row.run_id for row in bundle.order_events} | {row.run_id for row in bundle.fills}
    run_ids |= {row.run_id for row in bundle.positions}
    assert run_ids == {run_id}
    assert bundle.run.run_config["funding_source"] == "none"
    assert bundle.run.run_config["queue_model"] == "pessimistic"
    closed = [p for p in bundle.positions if p.status == PositionStatus.CLOSED]
    assert results.trade_count == len(closed) >= 8
    assert results.fees_total == sum(f.fee for f in bundle.fills)
    assert results.net_pnl < 0
    assert band is not None
    assert band.trades_used == len(closed)
    for order in bundle.orders:
        assert decision_id([f"decision:{order.decision_id}"]) == order.decision_id


def test_an_own_run_samples_no_funding(unit):
    assert unit.funding_samples == ()


def test_a_tardis_unit_samples_the_mid_and_position_at_each_boundary_inside_it(batch):
    (plan,) = _unit_plans(batch, Variant(name=BASE, latency_ms=25, queue_position=True))

    funded = run_unit(replace(plan, funding=True))

    # The segment is 17:55 to 18:05 of data: 18:00 is its only boundary.
    (sample,) = funded.funding_samples
    assert sample.boundary_ns == H18
    assert Decimal(50_000) < sample.mid < Decimal(200_000)
    held = [
        row
        for _, row in funded.positions
        if row.opened_ns <= H18 and (row.closed_at is None or row.last_event_ns > H18)
    ]
    if sample.position is None:
        assert sample.signed_qty == 0
    else:
        (position,) = [p for p in held if PositionKey(p.position_ref, p.opened_ns) == sample.position]
        sign = 1 if position.side == PositionSide.LONG else -1
        assert sample.signed_qty == sign * position.peak_qty


def _funded(batch, unit, rate_ts=0):
    closed = next(row for _, row in unit.positions if row.status == PositionStatus.CLOSED)
    sign = 1 if closed.side == PositionSide.LONG else -1
    key = PositionKey(closed.position_ref, closed.opened_ns)
    sample = FundingSample(closed.opened_ns + 1, Decimal(60_000), sign * closed.peak_qty, key)
    rates = (FundingRate(rate_ts, Decimal("0.0001")),)
    return closed, replace(batch, funding_rates=rates), replace(unit, funding_samples=(sample,))


def test_funding_payments_go_into_the_bundle_the_position_and_the_results(batch, unit):
    closed, funded_batch, funded_unit = _funded(batch, unit)
    now = datetime.now(UTC)
    variant = Variant(name=BASE, latency_ms=25, queue_position=True)
    _, plain_results, plain_band = build_bundle(batch, variant, [unit], started_at=now, ended_at=now)

    bundle, results, band = build_bundle(funded_batch, variant, [funded_unit], started_at=now, ended_at=now)

    sign = 1 if closed.side == PositionSide.LONG else -1
    amount = -sign * closed.peak_qty * Decimal(60_000) * Decimal("0.0001")
    (row,) = bundle.funding
    assert (row.source, row.rate, row.amount) == (FundingSource.COMPUTED, Decimal("0.0001"), amount)
    assert row.run_id == bundle.run.id
    (position,) = [p for p in bundle.positions if p.id == row.position_id]
    assert (position.position_ref, position.funding_total) == (closed.position_ref, amount)
    assert position.realized_pnl == closed.realized_pnl  # before funding, as in paper
    assert results.funding_total == amount
    assert results.net_pnl == plain_results.net_pnl + amount
    assert band is not None
    assert plain_band is not None
    assert band.trades_used == plain_band.trades_used - 1  # a trade across a funding boundary leaves the band


def test_a_boundary_without_an_earlier_rate_fails_the_bundle(batch, unit):
    _, funded_batch, funded_unit = _funded(batch, unit, rate_ts=H18 + HOUR)
    now = datetime.now(UTC)

    with pytest.raises(FundingRateMissingError):
        build_bundle(funded_batch, Variant(BASE, 25, True), [funded_unit], started_at=now, ended_at=now)


@pytest.mark.postgres
def test_replay_writes_one_bundle_and_the_reports(tmp_path, loaded):
    """Runs in the lab service: ``docker compose run --rm -T --entrypoint pytest lab -q -m postgres``."""
    base = Settings(trading_env="demo", postgres_user="lab_app")
    engine = create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=1)
    sessions = sessionmaker(engine)
    settings = lab_settings(FIXTURES, tmp_path)
    started = datetime.now(UTC)
    try:
        assert replay(settings, loaded, sessions) == 0
        (folder,) = list(tmp_path.iterdir())
        assert sorted(p.name for p in folder.iterdir()) == ["base.bundle.json", "base.json", "base.md", "index.md"]
        with sessions() as session:
            runs = session.execute(
                select(func.count()).select_from(models.StrategyRun).where(models.StrategyRun.started_at >= started)
            ).scalar_one()
        assert runs == 1
    finally:
        engine.dispose()
