"""A complete spec 0015 study bundle for tests: every row kind, the study parts, and the version it needs."""

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

from trading_core.recording.enums import (
    Env,
    EquityBasis,
    FundingSource,
    Intent,
    Liquidity,
    OrderEventKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RiskVerdict,
    RunMode,
    RunStatus,
    Sample,
    Severity,
    StrategyKind,
    StudyLab,
    StudyRole,
    TimeInForce,
)
from trading_core.recording.rows import (
    DailyEquityRow,
    DecisionRow,
    FillRow,
    FundingRow,
    InstrumentRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
    RunBundle,
    RunMeasuresRow,
    RunResultRow,
    RunRow,
    StrategyVersionRow,
    StudyRow,
    StudyRunRow,
    canonical_json,
    version_hash,
)
from trading_core.thresholds_file import packaged_thresholds

INSTRUMENT = "PF_XBTUSD.KRAKEN"
LAB_TRADER = "BACKTESTER-LAB"
THRESHOLDS = packaged_thresholds().to_json()
T0 = 1_760_000_000_000_000_000


def instrument(tick: Decimal = Decimal(1)) -> InstrumentRow:
    """The BTC perpetual; ``tick`` matches the database the test writes into (the real tick is 1)."""
    return InstrumentRow(
        instrument_id=INSTRUMENT,
        venue="KRAKEN",
        symbol="PF_XBTUSD",
        base_currency="BTC",
        quote_currency="USD",
        settle_currency="USD",
        tick_size=tick,
        lot_size=Decimal("0.0001"),
        multiplier=Decimal(1),
        is_inverse=False,
    )


def sha(text: str) -> str:
    """A stand in hash."""
    return hashlib.sha256(text.encode()).hexdigest()


def version(strategy_id: UUID, config: dict[str, object], commit: str = "a" * 40) -> StrategyVersionRow:
    """A frozen version of ``config``."""
    return StrategyVersionRow(
        id=uuid7(),
        strategy_id=strategy_id,
        git_commit=commit,
        config=config,  # type: ignore[arg-type]
        version_hash=version_hash(commit, config),  # type: ignore[arg-type]
        code_hash=sha("code"),
        frozen_hash=sha(f"frozen{canonical_json(config)}"),  # type: ignore[arg-type]
    )


def study(strategy_id: UUID, name: str, plan: dict[str, object] | None = None) -> StudyRow:
    """A candle study with a small plan."""
    plan = plan or {"study": {"name": name}}
    return StudyRow(
        strategy_id=strategy_id,
        name=name,
        lab=StudyLab.CANDLE,
        code_hash=sha("code"),
        plan=plan,  # type: ignore[arg-type]
        plan_hash=sha(canonical_json(plan)),  # type: ignore[arg-type]
        thresholds=THRESHOLDS,  # type: ignore[arg-type]
        thresholds_hash=sha(canonical_json(THRESHOLDS)),  # type: ignore[arg-type]
    )


def study_bundle(
    the_study: StudyRow,
    the_version: StrategyVersionRow,
    *,
    strategy: str = "macd_test",
    role: StudyRole = StudyRole.DEVELOPMENT,
    grid_point: int | None = 0,
    status: RunStatus = RunStatus.COMPLETED,
    days: int = 3,
    start: date = date(2026, 1, 1),
    tick: Decimal = Decimal(1),
) -> RunBundle:
    """One lab run of ``the_version`` in ``the_study`` with a decision, order, fill, position, funding, and days."""
    run = RunRow(
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        status=status,
        strategy_version_id=the_version.id,
        trader_id=LAB_TRADER,
        run_config={"starting_balance_usd": Decimal(10_000), "slippage_bps": Decimal(1), "source": "candles"},
        data_start_at=datetime(start.year, start.month, start.day, tzinfo=UTC),
        data_end_at=datetime(start.year, start.month, start.day, tzinfo=UTC) + timedelta(days=days),
        started_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
        ended_at=datetime(2026, 10, 19, 13, tzinfo=UTC),
    )
    bundle = RunBundle(
        run=run,
        strategy_name=strategy,
        strategy_kind=StrategyKind.HOUR_LEVEL,
        version=the_version,
        instrument=instrument(tick),
        study=the_study,
        study_strategy_name="macd_test",
        study_strategy_kind=StrategyKind.HOUR_LEVEL,
        study_run=StudyRunRow(
            study_id=the_study.id,
            run_id=run.id,
            role=role,
            grid_point=None if role == StudyRole.BENCHMARK else grid_point,
            frozen_hash=the_version.frozen_hash or "",
            status=status,
        ),
    )
    if status != RunStatus.COMPLETED:
        return bundle
    decision = DecisionRow(
        env=Env.DEMO,
        run_id=run.id,
        instrument_id=INSTRUMENT,
        intent=Intent.ENTER,
        side=OrderSide.BUY,
        reason="signal",
        inputs={"mid": Decimal("60000.5")},
        risk_verdict=RiskVerdict.APPROVED,
        decided_ns=T0,
    )
    order = OrderRow(
        env=Env.DEMO,
        run_id=run.id,
        decision_id=decision.id,
        origin=OrderOrigin.STRATEGY,
        client_order_id="O-1",
        instrument_id=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        quantity=Decimal("0.01"),
        status=OrderStatus.FILLED,
        filled_qty=Decimal("0.01"),
        avg_px=Decimal(60001),
        last_event_ns=T0 + 2,
    )
    position = PositionRow(
        env=Env.DEMO,
        run_id=run.id,
        instrument_id=INSTRUMENT,
        position_ref="P-1",
        side=PositionSide.FLAT,
        status=PositionStatus.CLOSED,
        quantity=Decimal(0),
        peak_qty=Decimal("0.01"),
        avg_entry_px=Decimal(60001),
        avg_exit_px=Decimal(60500),
        realized_pnl=Decimal("4.39"),
        fees_total=Decimal("0.6"),
        funding_total=Decimal("-0.1"),
        currency="USD",
        opened_ns=T0 + 2,
        closed_at=datetime(2026, 1, 2, tzinfo=UTC),
        last_event_ns=T0 + 5,
    )
    days_rows = [
        DailyEquityRow(
            run_id=run.id,
            env=Env.DEMO,
            day=start + timedelta(days=i),
            equity_close=Decimal(10_000) + Decimal(i) * 2,
            basis=EquityBasis.MARKED,
            trades_closed=1 if i == 1 else 0,
            fees=Decimal("0.6") if i == 1 else Decimal(0),
            funding=Decimal("-0.1") if i == 1 else Decimal(0),
            currency="USD",
        )
        for i in range(days)
    ]
    return replace(
        bundle,
        results=[
            RunResultRow(
                run_id=run.id,
                env=Env.DEMO,
                sample=Sample.FULL,
                return_after_costs=Decimal("0.000429"),
                max_drawdown=Decimal(0),
                risk_adjusted_ratio=Decimal("1.5"),
                ratio_kind="sharpe_daily",
                win_rate=Decimal(1),
                trade_count=1,
                fees_total=Decimal("0.6"),
                funding_total=Decimal("-0.1"),
                currency="USD",
                computed_at=datetime(2026, 10, 19, 13, tzinfo=UTC),
            )
        ],
        decisions=[decision],
        orders=[order],
        order_events=[
            OrderEventRow(
                env=Env.DEMO,
                run_id=run.id,
                client_order_id="O-1",
                event_id=uuid7(),
                kind=OrderEventKind.ORDER_FILLED,
                details={"px": Decimal(60001)},
                occurred_ns=T0 + 2,
            )
        ],
        fills=[
            FillRow(
                env=Env.DEMO,
                run_id=run.id,
                client_order_id="O-1",
                position_id=position.id,
                instrument_id=INSTRUMENT,
                trade_id="T-1",
                side=OrderSide.BUY,
                quantity=Decimal("0.01"),
                price=Decimal(60001),
                fee=Decimal("0.3"),
                fee_currency="USD",
                liquidity=Liquidity.TAKER,
                filled_ns=T0 + 2,
            )
        ],
        positions=[position],
        funding=[
            FundingRow(
                env=Env.DEMO,
                run_id=run.id,
                position_id=position.id,
                instrument_id=INSTRUMENT,
                amount=Decimal("-0.1"),
                currency="USD",
                rate=Decimal("0.0001"),
                source=FundingSource.COMPUTED,
                paid_ns=T0 + 3,
            )
        ],
        risk_events=[
            RiskEventRow(
                env=Env.DEMO,
                event_key=f"lab:{run.id}:1",
                kind=RiskEventKind.LIMIT_HIT,
                severity=Severity.INFO,
                message="test",
                run_id=run.id,
                occurred_ns=T0 + 1,
            )
        ],
        daily_equity=days_rows,
        measures=RunMeasuresRow(
            run_id=run.id,
            env=Env.DEMO,
            drawdown_basis=EquityBasis.MARKED,
            marked_max_drawdown=Decimal("0.01"),
            longest_drawdown_hours=3,
            exposure=Decimal("0.2"),
            long_trades=1,
            long_net_pnl=Decimal("4.29"),
            short_trades=0,
            short_net_pnl=Decimal(0),
            top_trades_profit_share=Decimal(1),
            avg_gross_edge=Decimal("4.99"),
            avg_round_trip_cost=Decimal("0.7"),
            currency="USD",
        ),
    )
