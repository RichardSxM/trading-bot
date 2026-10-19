"""covers: spec 0002 AC-5 (replays of every record type), AC-10 (run bundles), AC-15 (promotions, live gate);
spec 0008 AC-11 (a replay bundle with fill stats and risk events)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_core.recording import models, queries, repository
from trading_core.recording.enums import (
    ControlCmd,
    ControlOutcome,
    DecidedBy,
    Env,
    FundingSource,
    Liquidity,
    OrderEventKind,
    OrderOrigin,
    RiskEventKind,
    RunMode,
    RunStatus,
    Sample,
    Severity,
    Stage,
    StrategyKind,
    Verdict,
)
from trading_core.recording.errors import (
    ForeignRunError,
    InvalidModeError,
    InvalidTransitionError,
    ManualLivePromotionError,
    MissingEvidenceError,
    MissingParentError,
    PromotionRequiredError,
    RecordConflictError,
    UnknownCommitError,
)
from trading_core.recording.rows import (
    AccountSnapshotRow,
    ControlCommandRow,
    FundingRow,
    OrderEventRow,
    PositionSnapshotRow,
    PromotionRow,
    RiskEventRow,
    RunBundle,
    RunFillStatsRow,
    RunResultRow,
    RunRow,
    column_values,
)
from trading_core.settings import get_settings

from .conftest import INSTRUMENT, WORLD_FROZEN, World


def _count(session: Session, model, **where) -> int:
    clauses = [getattr(model, name).is_not_distinct_from(value) for name, value in where.items()]
    return session.execute(select(func.count()).select_from(model).where(*clauses)).scalar_one()


def _twice(write, session: Session, row) -> None:
    """Write a row, then the same content again under a fresh row id (a replay after a restart)."""
    write(session, row)
    write(session, replace(row, id=uuid7()))


def test_order_events_replay_to_one_row(session: Session, world: World):
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    event = OrderEventRow(
        env=Env.DEMO,
        run_id=world.run_id,
        client_order_id=order.client_order_id,
        event_id=uuid7(),
        kind=OrderEventKind.ORDER_ACCEPTED,
        details={"venue_order_id": "V-1"},
        occurred_ns=world.tick(),
    )

    _twice(repository.record_order_event, session, event)

    assert _count(session, models.OrderEvent, event_id=event.event_id) == 1


def test_an_order_event_for_an_unknown_order_is_refused(session: Session, world: World):
    event = OrderEventRow(
        env=Env.DEMO,
        run_id=world.run_id,
        client_order_id="O-nowhere",
        event_id=uuid7(),
        kind=OrderEventKind.ORDER_ACCEPTED,
        details={},
        occurred_ns=world.tick(),
    )

    with pytest.raises(MissingParentError):
        repository.record_order_event(session, event)


def test_snapshots_replay_to_one_row(session: Session, world: World):
    position = world.position()
    repository.upsert_position(session, position)
    session.flush()
    position_id = session.execute(
        select(models.Position.id).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    snapshot = PositionSnapshotRow(
        env=Env.DEMO,
        position_id=position_id,
        quantity=Decimal("0.001"),
        mark_px=Decimal("60010"),
        unrealized_pnl=Decimal("0.0095"),
        currency="USD",
        captured_ns=world.tick(),
    )
    ref = f"ACC-{uuid7().hex[-8:]}"
    accounts = [
        AccountSnapshotRow(
            env=Env.DEMO,
            run_id=run_id,
            account_ref=ref,
            balance_total=Decimal("10000.12345678"),
            balance_free=Decimal(9000),
            margin_used=Decimal("1000.12345678"),
            currency="USD",
            captured_ns=world.tick(),
        )
        for run_id in (world.run_id, None)
    ]

    _twice(repository.record_position_snapshot, session, snapshot)
    for account in accounts:
        _twice(repository.record_account_snapshot, session, account)

    assert _count(session, models.PositionSnapshot, position_id=position_id) == 1
    assert _count(session, models.AccountSnapshot, account_ref=ref, run_id=world.run_id) == 1
    assert _count(session, models.AccountSnapshot, account_ref=ref, run_id=None) == 1


def test_a_snapshot_of_an_unknown_position_is_refused(session: Session, world: World):
    snapshot = PositionSnapshotRow(
        env=Env.DEMO,
        position_id=uuid7(),
        quantity=Decimal(0),
        mark_px=Decimal(1),
        unrealized_pnl=Decimal(0),
        currency="USD",
        captured_ns=world.tick(),
    )

    with pytest.raises(MissingParentError):
        repository.record_position_snapshot(session, snapshot)


def test_funding_replays_to_one_row_per_source_and_fills_in_its_position(session: Session, world: World):
    position = world.position()
    repository.upsert_position(session, position)
    session.flush()
    position_id = session.execute(
        select(models.Position.id).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    logged = FundingRow(
        env=Env.DEMO,
        run_id=world.run_id,
        instrument_id=INSTRUMENT,
        amount=Decimal("-0.01234567"),
        currency="USD",
        source=FundingSource.KRAKEN_LOG,
        external_id=f"LOG-{uuid7().hex[-8:]}",
        paid_ns=world.tick(),
    )
    computed = replace(logged, id=uuid7(), source=FundingSource.COMPUTED, external_id=None, rate=Decimal("0.0001"))

    _twice(repository.record_funding, session, logged)
    repository.record_funding(session, replace(logged, id=uuid7(), position_id=position_id))
    _twice(repository.record_funding, session, computed)

    assert _count(session, models.FundingPayment, external_id=logged.external_id) == 1
    assert _count(session, models.FundingPayment, run_id=world.run_id, source="computed") == 1
    stored = session.execute(
        select(models.FundingPayment.position_id).where(models.FundingPayment.external_id == logged.external_id)
    ).scalar_one()
    assert stored == position_id
    with pytest.raises(RecordConflictError):
        repository.record_funding(session, replace(logged, id=uuid7(), amount=Decimal("-1")))


def test_risk_events_and_run_results_replay_to_one_row(session: Session, world: World):
    event = RiskEventRow(
        env=Env.DEMO,
        event_key=f"halt:{world.run_id}:{world.tick()}",
        kind=RiskEventKind.HALT,
        severity=Severity.CRITICAL,
        message="daily loss limit",
        details={"loss": Decimal("-12.5")},
        run_id=world.run_id,
        occurred_ns=world.clock,
    )
    result = RunResultRow(
        run_id=world.run_id,
        env=Env.DEMO,
        sample=Sample.FULL,
        return_after_costs=Decimal("0.05"),
        max_drawdown=Decimal("0.02"),
        win_rate=Decimal("0.55"),
        trade_count=40,
        fees_total=Decimal("1.2"),
        funding_total=Decimal("-0.3"),
        currency="USD",
        computed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    _twice(repository.record_risk_event, session, event)
    repository.record_run_result(session, result)
    repository.record_run_result(session, result)

    assert _count(session, models.RiskEvent, event_key=event.event_key) == 1
    assert _count(session, models.RunResult, run_id=world.run_id) == 1


def _command(world: World) -> ControlCommandRow:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    return ControlCommandRow(
        id=uuid7(), env=Env.DEMO, cmd=ControlCmd.STOP, issued_at=now, expires_at=now, received_at=now
    )


def test_control_commands_replay_and_move_to_one_final_outcome(session: Session, world: World):
    command = _command(world)
    repository.record_control_command(session, command)
    repository.record_control_command(session, command)

    repository.set_control_outcome(session, command.id, ControlOutcome.EXECUTED, world.tick(), {"flattened": 0})
    repository.set_control_outcome(session, command.id, ControlOutcome.EXECUTED, world.tick())
    repository.record_control_command(session, replace(command, received_at=datetime(2026, 10, 2, tzinfo=UTC)))

    stored = session.get(models.ControlCommand, command.id)
    assert stored is not None
    assert (stored.outcome, stored.details) == ("executed", {"flattened": 0})
    assert _count(session, models.ControlCommand, id=command.id) == 1
    with pytest.raises(InvalidTransitionError):
        repository.set_control_outcome(session, command.id, ControlOutcome.FAILED, world.tick())
    with pytest.raises(RecordConflictError):
        repository.record_control_command(session, replace(command, cmd=ControlCmd.FLATTEN))


def test_a_control_outcome_must_be_final_and_name_a_known_command(session: Session, world: World):
    command = _command(world)
    repository.record_control_command(session, command)

    with pytest.raises(InvalidTransitionError):
        repository.set_control_outcome(session, command.id, ControlOutcome.RECEIVED, world.tick())
    with pytest.raises(MissingParentError):
        repository.set_control_outcome(session, uuid7(), ControlOutcome.EXECUTED, world.tick())

    stored = session.get(models.ControlCommand, command.id)
    assert stored is not None
    assert stored.outcome == "received"


def test_the_control_outcome_query_tells_unfinished_from_finished(session: Session, world: World):
    # covers: spec 0005 AC-6, review 2026-10-17 major 1: the consumer reruns a command left at received.
    command = _command(world)
    assert queries.control_command_outcome(session, command.id) is None

    repository.record_control_command(session, command)
    assert queries.control_command_outcome(session, command.id) == ControlOutcome.RECEIVED

    repository.set_control_outcome(session, command.id, ControlOutcome.EXECUTED, world.tick())
    assert queries.control_command_outcome(session, command.id) == ControlOutcome.EXECUTED


def _sim_run(world: World, mode: RunMode = RunMode.BACKTEST) -> RunRow:
    return RunRow(
        env=Env.DEMO,
        mode=mode,
        status=RunStatus.COMPLETED,
        strategy_version_id=world.version_id,
        trader_id="BACKTESTER-001",
        run_config={"balance": Decimal(10000), "fee": Decimal("0.0005")},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_a_run_bundle_lands_whole_and_commits(engine, session: Session, world: World):
    run = _sim_run(world)
    on_run = replace(world, run_id=run.id)
    decision = on_run.decision()
    order = on_run.order(decision_id=decision.id)
    bundle = RunBundle(
        run=run,
        decisions=[decision],
        orders=[order],
        order_events=[
            OrderEventRow(
                env=Env.DEMO,
                run_id=run.id,
                client_order_id=order.client_order_id,
                event_id=uuid7(),
                kind=OrderEventKind.ORDER_FILLED,
                details={},
                occurred_ns=on_run.tick(),
            )
        ],
        fills=[on_run.fill(order.client_order_id)],
        positions=[on_run.position()],
    )

    assert repository.write_run_bundle(session, bundle) == run.id

    with Session(engine) as check:
        assert _count(check, models.Fill, run_id=run.id) == 1
        assert _count(check, models.OrderEvent, env="demo", order_id=order.id) == 1


def test_a_replay_bundle_carries_fill_stats_and_risk_events(engine, session: Session, world: World):
    run = _sim_run(world, RunMode.REPLAY)
    on_run = replace(world, run_id=run.id)
    decision = on_run.decision()
    order = on_run.order(decision_id=decision.id)
    stats = RunFillStatsRow(
        run_id=run.id,
        env=Env.DEMO,
        liquidity=Liquidity.MAKER,
        orders_placed=4,
        orders_filled=1,
        fill_rate=Decimal("0.25"),
        fill_count=1,
        markout_1s_bps=Decimal("-1.5"),
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    refused = RiskEventRow(
        env=Env.DEMO,
        event_key=f"limit_hit:{decision.id}",
        kind=RiskEventKind.LIMIT_HIT,
        severity=Severity.INFO,
        message="entry refused: max_order_notional",
        run_id=run.id,
        decision_id=decision.id,
        occurred_ns=on_run.tick(),
    )
    bundle = RunBundle(run=run, decisions=[decision], orders=[order], fill_stats=[stats], risk_events=[refused])

    repository.write_run_bundle(session, bundle)

    with Session(engine) as check:
        stored = check.get(models.RunFillStats, (run.id, "maker"))
        assert stored is not None
        assert (stored.orders_placed, stored.fill_rate, stored.markout_5s_bps) == (4, Decimal("0.25"), None)
        assert _count(check, models.RiskEvent, run_id=run.id, decision_id=decision.id) == 1


def test_a_risk_event_of_another_run_keeps_the_bundle_out(engine, session: Session, world: World):
    run = _sim_run(world, RunMode.REPLAY)
    stray = RiskEventRow(
        env=Env.DEMO,
        event_key=f"stray:{uuid7()}",
        kind=RiskEventKind.LIMIT_HIT,
        severity=Severity.INFO,
        message="stray",
        run_id=world.run_id,
        occurred_ns=world.tick(),
    )

    with pytest.raises(ForeignRunError):
        repository.write_run_bundle(session, RunBundle(run=run, risk_events=[stray]))

    with Session(engine) as check:
        assert check.get(models.StrategyRun, run.id) is None


def test_a_bad_row_rolls_back_the_whole_bundle(engine, session: Session, world: World):
    run = _sim_run(world)
    on_run = replace(world, run_id=run.id)
    good = on_run.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    bad = on_run.order(decision_id=None, origin=OrderOrigin.STRATEGY)  # violates the decision CHECK
    bundle = RunBundle(run=run, orders=[good, bad], fills=[on_run.fill(good.client_order_id)])

    with pytest.raises(IntegrityError):
        repository.write_run_bundle(session, bundle)

    with Session(engine) as check:
        assert check.get(models.StrategyRun, run.id) is None
        assert _count(check, models.Order, run_id=run.id) == 0


def test_a_bundle_with_a_fill_for_an_order_it_does_not_carry_writes_nothing(engine, session: Session, world: World):
    # covers: AC-10
    run = _sim_run(world)
    on_run = replace(world, run_id=run.id)
    order = on_run.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    bundle = RunBundle(run=run, orders=[order], fills=[on_run.fill("O-not-in-bundle")])

    with pytest.raises(MissingParentError):
        repository.write_run_bundle(session, bundle)

    with Session(engine) as check:
        assert check.get(models.StrategyRun, run.id) is None
        assert _count(check, models.Order, run_id=run.id) == 0


def test_a_sim_bundle_of_the_other_env_lands_from_a_demo_process(engine, session: Session, world: World):
    # covers: AC-4 (sim bundles are excepted from the TRADING_ENV check)
    run = replace(_sim_run(world), env=Env.LIVE)
    on_run = replace(world, run_id=run.id)
    order = on_run.order(env=Env.LIVE, decision_id=None, origin=OrderOrigin.RECONCILIATION)
    bundle = RunBundle(run=run, orders=[order], fills=[on_run.fill(order.client_order_id, env=Env.LIVE)])

    assert get_settings().trading_env == "demo"
    repository.write_run_bundle(session, bundle)

    with Session(engine) as check:
        stored = check.get(models.StrategyRun, run.id)
        assert stored is not None
        assert stored.env == "live"
        assert _count(check, models.Fill, run_id=run.id) == 1


@pytest.mark.parametrize("field", ["run_id", "env"])
def test_a_bundle_row_of_another_run_or_env_writes_nothing(engine, session: Session, world: World, field):
    run = _sim_run(world)
    on_run = replace(world, run_id=run.id)
    order = on_run.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    stray = {"run_id": world.run_id} if field == "run_id" else {"env": Env.LIVE}
    bundle = RunBundle(run=run, orders=[order], fills=[on_run.fill(order.client_order_id, **stray)])

    with pytest.raises(ForeignRunError):
        repository.write_run_bundle(session, bundle)

    with Session(engine) as check:
        assert check.get(models.StrategyRun, run.id) is None
        assert _count(check, models.Fill, run_id=world.run_id) == 0


@pytest.mark.parametrize("mode", [RunMode.PAPER, RunMode.LIVE])
def test_a_paper_or_live_bundle_is_refused(session: Session, world: World, mode):
    with pytest.raises(InvalidModeError):
        repository.write_run_bundle(session, RunBundle(run=replace(_sim_run(world), mode=mode)))


def _promotion(
    version_id, verdict: Verdict, day: int, frozen: str = WORLD_FROZEN.frozen_hash, by: DecidedBy = DecidedBy.GATE
) -> PromotionRow:
    return PromotionRow(
        strategy_version_id=version_id,
        from_stage=Stage.PAPER,
        to_stage=Stage.LIVE,
        verdict=verdict,
        thresholds={"min_trades": 30},
        decided_by=by,
        decided_at=datetime(2026, 10, day, tzinfo=UTC),
        frozen_hash=frozen,
    )


def test_a_promotion_needs_evidence_of_the_same_version(session: Session, world: World):
    other = repository.ensure_strategy_version(session, "tracer", StrategyKind.SCALPING, "fedcba", {"window": 1})

    with pytest.raises(MissingEvidenceError):
        repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 1), [])
    with pytest.raises(MissingEvidenceError):
        repository.record_promotion(session, _promotion(other, Verdict.PASS, 1, frozen="0" * 64), [world.run_id])
    with pytest.raises(MissingEvidenceError):
        repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 1), [uuid7()])

    promotion_id = repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 1), [world.run_id])
    assert _count(session, models.PromotionEvidence, promotion_id=promotion_id) == 1


@pytest.fixture
def live_process(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start_live(session: Session, version_id) -> object:
    return repository.start_run(
        session,
        env=Env.LIVE,
        mode=RunMode.LIVE,
        version_id=version_id,
        trader_id=f"TRADER-LIVE-{uuid7().hex[-6:]}",
        run_config={},
        started_ns=1_900_000_000_000_000_000,
    )


def test_a_live_run_needs_a_known_commit_and_a_latest_pass(session: Session, world: World, live_process):
    with pytest.raises(PromotionRequiredError):
        _start_live(session, world.version_id)

    repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 1), [world.run_id])
    repository.record_promotion(session, _promotion(world.version_id, Verdict.FAIL, 2), [world.run_id])
    with pytest.raises(PromotionRequiredError):
        _start_live(session, world.version_id)

    repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 3), [world.run_id])
    run_id = _start_live(session, world.version_id)
    assert session.get(models.StrategyRun, run_id) is not None


def test_a_live_run_matches_on_the_frozen_hash_not_the_version(session: Session, world: World, live_process):
    """covers: spec 0015 AC-17 (a redeploy of the same code on another commit keeps its live pass)."""
    repository.record_promotion(session, _promotion(world.version_id, Verdict.PASS, 4), [world.run_id])
    redeployed = repository.ensure_strategy_version(
        session,
        "tracer",
        StrategyKind.SCALPING,
        "fedcba9876543210",
        {"size": Decimal("0.001"), "window": 20},
        WORLD_FROZEN,
    )

    assert session.get(models.StrategyRun, _start_live(session, redeployed)) is not None


def test_a_version_without_a_frozen_hash_never_goes_live(session: Session, world: World, live_process):
    unfrozen = repository.ensure_strategy_version(session, "tracer", StrategyKind.SCALPING, "abcdef", {"w": 2})

    with pytest.raises(PromotionRequiredError):
        _start_live(session, unfrozen)


def test_only_the_gate_can_promote_to_live(session: Session, world: World):
    """covers: spec 0015 AC-15 (ManualLivePromotionError) and the CHECK behind it."""
    with pytest.raises(ManualLivePromotionError):
        repository.record_promotion(
            session, _promotion(world.version_id, Verdict.PASS, 5, by=DecidedBy.MANUAL), [world.run_id]
        )
    row = _promotion(world.version_id, Verdict.PASS, 5, by=DecidedBy.MANUAL)
    with pytest.raises(IntegrityError, match="ck_promotions_no_manual_live"):
        session.execute(insert(models.Promotion).values(column_values(row)))


def test_a_live_run_refuses_an_unknown_commit(session: Session, world: World, live_process):
    unknown = repository.ensure_strategy_version(session, "tracer", StrategyKind.SCALPING, "unknown", {"w": 1})

    with pytest.raises(UnknownCommitError):
        _start_live(session, unknown)


def test_a_live_run_refuses_a_version_that_does_not_exist(session: Session, world: World, live_process):
    # covers: AC-15
    with pytest.raises(MissingParentError):
        _start_live(session, uuid7())
