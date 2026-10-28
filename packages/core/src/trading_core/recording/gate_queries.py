"""What the promotion gate reads from Postgres (spec 0015 *Value sourcing*), as ``trading_core.gate`` inputs.

Read only. The pure gate never touches the database; this adapter turns a study's stored rows into
frozen dataclasses: every study run with its daily equity, results, and measures, the holdout's closed
trades, the trial count, and the missing cost flags.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_core.gate.inputs import CandidateInputs, Lab, Measures, Role, RunSummary, StudyRunInput, TradeReturn
from trading_core.gate.paper import (
    EntryFill,
    EntryOrder,
    PaperEvent,
    PaperInputs,
    entry_slippage_bps,
    evaluate_paper,
)
from trading_core.gate.series import DayEquity
from trading_core.gate.thresholds import Thresholds
from trading_core.lab.funding import funding_boundaries
from trading_core.recording import models
from trading_core.recording.enums import (
    DecidedBy,
    Intent,
    OrderOrigin,
    OrderSide,
    PositionStatus,
    RunStatus,
    Sample,
    Stage,
    StudyLab,
    StudyRole,
    Verdict,
)
from trading_core.recording.repository import final_grid_point, latest_promotion
from trading_core.thresholds_file import packaged_thresholds

# The run_config keys specs 0014 and 0008 write for slippage (candle) and latency (replay).
COST_SETTING = {StudyLab.CANDLE: "slippage_bps", StudyLab.REPLAY: "latency_ms"}


class UnknownStudyError(LookupError):
    """No study with that name."""


@dataclass(frozen=True)
class StudyEvidence:
    """A study's gate inputs, plus what the promotion row needs that the pure gate does not."""

    inputs: CandidateInputs
    study: models.Study
    final_version_id: UUID | None
    final_frozen_hash: str | None
    run_ids: tuple[UUID, ...]


def _starting_balance(run_config: Mapping[str, object]) -> Decimal | None:
    """The run's starting balance; None (the gate then says insufficient) for a run that never recorded one."""
    value = run_config.get("starting_balance_usd")
    return Decimal(value) if isinstance(value, str | int) else None


def _funding_missing(session: Session, run_id: UUID) -> bool:
    """True when a position spanned a funding charge time and the run holds no funding payment."""
    paid = session.execute(
        select(func.count()).select_from(models.FundingPayment).where(models.FundingPayment.run_id == run_id)
    ).scalar_one()
    if paid:
        return False
    for opened_ns, last_ns in session.execute(
        select(models.Position.opened_ns, models.Position.last_event_ns).where(models.Position.run_id == run_id)
    ):
        if funding_boundaries(opened_ns, last_ns):
            return True
    return False


def holdout_trades(session: Session, run_id: UUID, starting_balance: Decimal) -> tuple[TradeReturn, ...]:
    """The run's closed positions by close time: net P&L (realized, already after fees, plus funding) and
    its return over the equity before it, rebuilt from the starting balance (AC-11). Ties in close time
    go by open time, then coin, so a multi coin run orders them the same every time (spec 0022 AC-13)."""
    rows = session.execute(
        select(models.Position.realized_pnl, models.Position.funding_total, models.Position.instrument_id)
        .where(models.Position.run_id == run_id, models.Position.status == PositionStatus.CLOSED)
        .order_by(models.Position.closed_at, models.Position.opened_ns, models.Position.instrument_id)
    ).all()
    equity = starting_balance
    trades = []
    for realized, funding, instrument_id in rows:
        net = realized + funding
        trades.append(TradeReturn(net, net / equity if equity > 0 else Decimal(0), instrument_id))
        equity += net
    return tuple(trades)


def _run_input(session: Session, study_run: models.StudyRun, lab: StudyLab) -> StudyRunInput:
    run = session.get(models.StrategyRun, study_run.run_id)
    assert run is not None  # FK
    strategy, version_frozen_hash = session.execute(
        select(models.Strategy.name, models.StrategyVersion.frozen_hash)
        .join(models.StrategyVersion, models.StrategyVersion.strategy_id == models.Strategy.id)
        .where(models.StrategyVersion.id == run.strategy_version_id)
    ).one()
    result = session.get(models.RunResult, (run.id, Sample.FULL))
    measures = session.get(models.RunMeasures, run.id)
    days = session.execute(
        select(models.RunDailyEquity).where(models.RunDailyEquity.run_id == run.id).order_by(models.RunDailyEquity.day)
    ).scalars()
    role = Role(study_run.role)
    # A benchmark is always a candle run (AC-4), whatever the study's lab.
    run_lab = StudyLab.CANDLE if role == Role.BENCHMARK else lab
    return StudyRunInput(
        run_id=run.id,
        role=role,
        strategy=strategy,
        grid_point=study_run.grid_point,
        frozen_hash=study_run.frozen_hash,
        status=study_run.status,
        starting_balance=_starting_balance(run.run_config),
        daily=tuple(DayEquity(d.day, d.equity_close, d.trades_closed, d.fees, d.funding) for d in days),
        summary=None
        if result is None
        else RunSummary(
            result.return_after_costs,
            result.max_drawdown,
            result.win_rate,
            result.trade_count,
            result.fees_total,
            result.funding_total,
        ),
        measures=None
        if measures is None
        else Measures(
            measures.drawdown_basis,
            measures.marked_max_drawdown,
            measures.long_trades,
            measures.long_net_pnl,
            measures.short_trades,
            measures.short_net_pnl,
            measures.top_trades_profit_share,
            measures.avg_gross_edge,
            measures.avg_round_trip_cost,
        ),
        # Only the holdout's costs are checked (AC-12), so only its positions are scanned.
        funding_missing=role == Role.HOLDOUT and _funding_missing(session, run.id),
        cost_setting_missing=COST_SETTING[run_lab] not in run.run_config,
        version_frozen_hash=version_frozen_hash,
    )


_TRIAL_ROLES = (StudyRole.DEVELOPMENT, StudyRole.HOLDOUT)
_DAYS_PER_YEAR_ROOT = math.sqrt(365)


def family_trials(session: Session, kind: str, lab: StudyLab) -> tuple[int, tuple[float, ...]]:
    """N and the deflated Sharpe's trial Sharpes over the study's family (spec 0018 AC-19).

    The family is every study whose strategy has the same ``kind`` and whose ``lab`` is the same. N is
    the count of distinct ``frozen_hash`` over its development and holdout runs, failed ones too
    (benchmarks never count). The trial Sharpes are one per day Sharpe per distinct ``frozen_hash``
    with a completed development run: that hash's earliest such run with a ratio, its
    ``risk_adjusted_ratio ÷ √365``, ordered by hash.
    """
    family = (
        select(models.StudyRun)
        .join(models.Study, models.Study.id == models.StudyRun.study_id)
        .join(models.Strategy, models.Strategy.id == models.Study.strategy_id)
        .where(models.Strategy.kind == kind, models.Study.lab == lab)
    )
    trials = session.execute(
        family.with_only_columns(func.count(func.distinct(models.StudyRun.frozen_hash))).where(
            models.StudyRun.role.in_(_TRIAL_ROLES)
        )
    ).scalar_one()
    rows = session.execute(
        family.with_only_columns(models.StudyRun.frozen_hash, models.RunResult.risk_adjusted_ratio)
        .join(
            models.RunResult,
            (models.RunResult.run_id == models.StudyRun.run_id) & (models.RunResult.sample == Sample.FULL),
        )
        .where(
            models.StudyRun.role == StudyRole.DEVELOPMENT,
            models.StudyRun.status == RunStatus.COMPLETED,
            models.RunResult.risk_adjusted_ratio.is_not(None),
        )
        .order_by(models.StudyRun.frozen_hash, models.StudyRun.created_at, models.StudyRun.run_id)
    ).all()
    earliest: dict[str, Decimal] = {}
    for frozen, ratio in rows:
        if frozen is not None and ratio is not None:
            earliest.setdefault(frozen, ratio)
    return trials, tuple(float(earliest[h]) / _DAYS_PER_YEAR_ROOT for h in sorted(earliest))


def study_evidence(session: Session, name: str) -> StudyEvidence:
    """Everything ``gate evaluate`` reads for the study called ``name``; ``UnknownStudyError`` when there is none."""
    study = session.execute(select(models.Study).where(models.Study.name == name)).scalar_one_or_none()
    if study is None:
        raise UnknownStudyError(name)
    lab = StudyLab(study.lab)
    study_runs = (
        session.execute(
            select(models.StudyRun)
            .where(models.StudyRun.study_id == study.id)
            .order_by(models.StudyRun.grid_point, models.StudyRun.created_at, models.StudyRun.run_id)
        )
        .scalars()
        .all()
    )
    runs = tuple(_run_input(session, r, lab) for r in study_runs)
    strategy_name, kind = session.execute(
        select(models.Strategy.name, models.Strategy.kind).where(models.Strategy.id == study.strategy_id)
    ).one()
    trials, trial_sharpes = family_trials(session, kind, lab)
    plan_study = study.plan.get("study")
    seed = plan_study.get("seed") if isinstance(plan_study, Mapping) else None
    folds = study.plan.get("folds")
    inputs_runs = {r.run_id: r for r in runs}
    # Two completed holdouts score neither (the gate says inconsistent_evidence), so none is chosen here.
    holdouts = [r for r in runs if r.role == Role.HOLDOUT and r.completed]
    holdout = holdouts[0] if len(holdouts) == 1 else None
    final_version_id: UUID | None = None
    final_frozen: str | None = None
    code_hash: str | None = None
    trades: tuple[TradeReturn, ...] = ()
    chosen = holdout or next((r for r in runs if r.role == Role.DEVELOPMENT and r.completed), None)
    chosen = chosen or next((r for r in runs if r.role != Role.BENCHMARK), None)
    if chosen is not None:
        run = session.get(models.StrategyRun, chosen.run_id)
        assert run is not None
        version = session.get(models.StrategyVersion, run.strategy_version_id)
        assert version is not None
        final_version_id, final_frozen, code_hash = version.id, version.frozen_hash, version.code_hash
    if holdout is not None and holdout.starting_balance is not None:
        trades = holdout_trades(session, holdout.run_id, holdout.starting_balance)
    inputs = CandidateInputs(
        study_id=study.id,
        lab=Lab(study.lab),
        strategy_name=strategy_name,
        strategy_kind=kind,
        thresholds=Thresholds.from_json(study.thresholds),
        seed=seed if isinstance(seed, int) else 0,
        code_hash=code_hash,
        runs=runs,
        holdout_trades=trades,
        trial_count=max(1, trials),
        trial_sharpes=trial_sharpes,
        fold_overrides=tuple(sorted((k, v) for k, v in folds.items() if isinstance(v, int)))
        if isinstance(folds, Mapping)
        else (),
        final_grid_point=final_grid_point(session, study.id),
    )
    return StudyEvidence(inputs, study, final_version_id, final_frozen, tuple(inputs_runs))


# The /strategies ranking (spec 0015 AC-18) ---------------------------------------------------

DEVELOPING = "developing"
NOT_EVALUATED = "not evaluated"


@dataclass(frozen=True)
class StudyRanking:
    """One row of the ranking; every metric is from the study's latest ``candidate → paper`` promotion.

    ``stage`` is derived: ``developing`` without a completed holdout, else from the latest promotions
    of the holdout's frozen hash (``candidate``, ``paper``, or ``live``). ``verdict`` is ``not evaluated``
    while a held out study has no promotion yet.
    """

    study_id: UUID
    study: str
    strategy: str
    lab: str
    entry_mode: str | None
    stage: str
    verdict: str | None
    metrics: Mapping[str, object]

    def decimal(self, name: str) -> Decimal | None:
        """A metric as a Decimal, or None when missing."""
        value = self.metrics.get(name)
        return Decimal(value) if isinstance(value, str) else None

    def integer(self, name: str) -> int | None:
        """A count metric, or None when missing."""
        value = self.metrics.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None


def stage_of(session: Session, frozen_hash: str) -> str:
    """The stage of a frozen version: ``live``, ``paper``, or ``candidate`` (spec 0015 *State transitions*)."""
    live = latest_promotion(session, frozen_hash, Stage.LIVE)
    if live is not None and live.verdict == Verdict.PASS and live.decided_by == DecidedBy.GATE:
        return Stage.LIVE.value
    paper = latest_promotion(session, frozen_hash, Stage.PAPER)
    if paper is not None and paper.verdict == Verdict.PASS:
        return Stage.PAPER.value
    return Stage.CANDIDATE.value


def _sort_key(row: StudyRanking) -> tuple[bool, Decimal, bool, Decimal, str]:
    sharpe, ret = row.decimal("walk_forward_sharpe"), row.decimal("walk_forward_return")
    return (sharpe is None, -(sharpe or Decimal(0)), ret is None, -(ret or Decimal(0)), row.study)


def study_rankings(session: Session, study_id: UUID | None = None) -> list[StudyRanking]:
    """Every study (or only ``study_id``), by walk forward Sharpe (descending, nulls last), ties by walk
    forward return (AC-18)."""
    rows: list[StudyRanking] = []
    query = select(models.Study, models.Strategy.name).join(
        models.Strategy, models.Strategy.id == models.Study.strategy_id
    )
    if study_id is not None:
        query = query.where(models.Study.id == study_id)
    studies = session.execute(query).all()
    for study, strategy in studies:
        plan_study = study.plan.get("study")
        mode = plan_study.get("entry_mode") if isinstance(plan_study, Mapping) else None
        holdout = session.execute(
            select(models.StudyRun.frozen_hash).where(
                models.StudyRun.study_id == study.id,
                models.StudyRun.role == StudyRole.HOLDOUT,
                models.StudyRun.status == "completed",
            )
        ).scalar_one_or_none()
        promotion = session.execute(
            select(models.Promotion)
            .where(models.Promotion.study_id == study.id, models.Promotion.to_stage == Stage.PAPER)
            .order_by(models.Promotion.decided_at.desc(), models.Promotion.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        metrics: Mapping[str, object]
        if holdout is None:
            stage, verdict, metrics = DEVELOPING, None, {}
        elif promotion is None:
            stage, verdict, metrics = Stage.CANDIDATE.value, NOT_EVALUATED, {}
        else:
            stage, verdict, metrics = stage_of(session, holdout), promotion.verdict, promotion.metrics
        rows.append(
            StudyRanking(
                study_id=study.id,
                study=study.name,
                strategy=strategy,
                lab=study.lab,
                entry_mode=mode if isinstance(mode, str) else None,
                stage=stage,
                verdict=verdict,
                metrics=metrics,
            )
        )
    return sorted(rows, key=_sort_key)


# Paper → live (spec 0015 AC-16) ---------------------------------------------------------------


class UnknownFrozenHashError(LookupError):
    """No strategy version carries that frozen hash."""


@dataclass(frozen=True)
class PaperEvidence:
    """The paper inputs of one frozen hash, plus what the promotion row needs."""

    inputs: PaperInputs
    version_id: UUID
    study_id: UUID | None
    thresholds: Mapping[str, object]
    run_ids: tuple[UUID, ...]


def _mid(inputs: Mapping[str, object]) -> Decimal | None:
    value = inputs.get("mid")
    return Decimal(value) if isinstance(value, str | int) and not isinstance(value, bool) else None


def entry_fills(session: Session, run_ids: Sequence[UUID]) -> list[EntryFill]:
    """Every entry fill of the runs: fills of strategy orders whose decision intends to enter, with its mid."""
    rows = session.execute(
        select(models.Fill.side, models.Fill.price, models.Decision.inputs)
        .join(models.Order, models.Order.id == models.Fill.order_id)
        .join(models.Decision, models.Decision.id == models.Order.decision_id)
        .where(
            models.Fill.run_id.in_(run_ids),
            models.Order.origin == OrderOrigin.STRATEGY,
            models.Decision.intent == Intent.ENTER,
        )
        .order_by(models.Fill.filled_ns)
    ).all()
    return [EntryFill(side == OrderSide.BUY, price, _mid(inputs)) for side, price, inputs in rows]


def paper_evidence(session: Session, frozen_hash: str, now: datetime) -> PaperEvidence:
    """Everything ``gate evaluate --step paper-live`` reads; ``UnknownFrozenHashError`` when no version has it."""
    versions = (
        session.execute(select(models.StrategyVersion).where(models.StrategyVersion.frozen_hash == frozen_hash))
        .scalars()
        .all()
    )
    if not versions:
        raise UnknownFrozenHashError(frozen_hash)
    version_ids = [v.id for v in versions]
    kind = session.execute(
        select(models.Strategy.kind).where(models.Strategy.id == versions[0].strategy_id)
    ).scalar_one()
    candidate = latest_promotion(session, frozen_hash, Stage.PAPER)
    passed = candidate is not None and candidate.verdict == Verdict.PASS
    study = None if candidate is None or candidate.study_id is None else session.get(models.Study, candidate.study_id)
    holdout_id = None
    if study is not None:
        holdout_id = session.execute(
            select(models.StudyRun.run_id).where(
                models.StudyRun.study_id == study.id,
                models.StudyRun.role == StudyRole.HOLDOUT,
                models.StudyRun.status == "completed",
                models.StudyRun.frozen_hash == frozen_hash,
            )
        ).scalar_one_or_none()
    gate_pass = passed and candidate is not None and candidate.decided_by == DecidedBy.GATE and holdout_id is not None
    runs = []
    if passed and candidate is not None:
        runs = list(
            session.execute(
                select(models.StrategyRun)
                .where(
                    models.StrategyRun.env == "demo",
                    models.StrategyRun.mode == "paper",
                    models.StrategyRun.strategy_version_id.in_(version_ids),
                    models.StrategyRun.started_at > candidate.decided_at,
                )
                .order_by(models.StrategyRun.started_at)
            ).scalars()
        )
    run_ids = [r.id for r in runs]
    events = [
        PaperEvent(closed_at, realized, True, funding)
        for closed_at, realized, funding in session.execute(
            select(models.Position.closed_at, models.Position.realized_pnl, models.Position.funding_total).where(
                models.Position.run_id.in_(run_ids), models.Position.status == PositionStatus.CLOSED
            )
        )
        if closed_at is not None
    ] + [
        PaperEvent(paid_at, amount, False)
        for paid_at, amount in session.execute(
            select(models.FundingPayment.paid_at, models.FundingPayment.amount).where(
                models.FundingPayment.run_id.in_(run_ids)
            )
        )
    ]
    entries = [
        EntryOrder(post_only, status == "filled")
        for post_only, status in session.execute(
            select(models.Order.post_only, models.Order.status)
            .join(models.Decision, models.Decision.id == models.Order.decision_id)
            .where(
                models.Order.run_id.in_(run_ids),
                models.Order.origin == OrderOrigin.STRATEGY,
                models.Decision.intent == Intent.ENTER,
            )
        )
    ]
    plan_study = study.plan.get("study") if study is not None else None
    mode = plan_study.get("entry_mode") if isinstance(plan_study, Mapping) else None
    lab_fill_rate = lab_slippage = None
    bound = None
    if holdout_id is not None and study is not None and candidate is not None:
        stats = session.get(models.RunFillStats, (holdout_id, "maker"))
        lab_fill_rate = None if stats is None else stats.fill_rate
        holdout_run = session.get(models.StrategyRun, holdout_id)
        assert holdout_run is not None
        if study.lab == StudyLab.CANDLE:
            configured = holdout_run.run_config.get("slippage_bps")
            lab_slippage = Decimal(configured) if isinstance(configured, str | int) else None
        else:
            measured = [v for v in map(entry_slippage_bps, entry_fills(session, [holdout_id])) if v is not None]
            lab_slippage = sum(measured, Decimal(0)) / len(measured) if measured else None
        raw = candidate.metrics.get("trade_return_lower_bound")
        bound = Decimal(raw) if isinstance(raw, str) else None
    thresholds = study.thresholds if study is not None else packaged_thresholds().to_json()
    inputs = PaperInputs(
        frozen_hash=frozen_hash,
        strategy_kind=kind,
        thresholds=Thresholds.from_json(thresholds),
        evaluated_at=now,
        gate_pass=gate_pass,
        entry_mode=mode if isinstance(mode, str) else None,
        first_started_at=runs[0].started_at if runs else None,
        starting_balance=_starting_balance(runs[0].run_config) if runs else None,
        events=tuple(events),
        entries=tuple(entries),
        entry_fills=tuple(entry_fills(session, run_ids)),
        holdout_trade_bound=bound,
        lab_fill_rate=lab_fill_rate,
        lab_slippage_bps=lab_slippage,
    )
    version_id = candidate.strategy_version_id if candidate is not None else versions[0].id
    evidence = (*run_ids, *([holdout_id] if holdout_id is not None else []))
    return PaperEvidence(inputs, version_id, None if study is None else study.id, thresholds, evidence)


# Dashboard depth (spec 0015 AC-19 to AC-21) ----------------------------------------------------

LOG_PAGE_SIZE = 50


@dataclass(frozen=True)
class AttemptLine:
    """One promotion of a study: when, the verdict, its reasons, and its metrics."""

    promotion_id: UUID
    decided_at: datetime
    verdict: str
    reasons: Sequence[Mapping[str, object]]
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class StudyDetail:
    """A study's latest evaluation in depth, and every attempt."""

    ranking: StudyRanking
    folds: Sequence[models.PromotionFold]
    attempts: Sequence[AttemptLine]
    trials: int


def study_detail(session: Session, study_id: UUID) -> StudyDetail | None:
    """The study's detail (AC-19), or None for an unknown id."""
    ranking = next(iter(study_rankings(session, study_id)), None)
    if ranking is None:
        return None
    promotions = (
        session.execute(
            select(models.Promotion)
            .where(models.Promotion.study_id == study_id)
            .order_by(models.Promotion.decided_at.desc(), models.Promotion.id.desc())
        )
        .scalars()
        .all()
    )
    folds = (
        []
        if not promotions
        else session.execute(
            select(models.PromotionFold)
            .where(models.PromotionFold.promotion_id == promotions[0].id)
            .order_by(models.PromotionFold.fold)
        )
        .scalars()
        .all()
    )
    trials = session.execute(
        select(func.count(func.distinct(models.StudyRun.frozen_hash))).where(
            models.StudyRun.study_id == study_id, models.StudyRun.role != StudyRole.BENCHMARK
        )
    ).scalar_one()
    attempts = [
        AttemptLine(p.id, p.decided_at, p.verdict, [r for r in p.reasons if isinstance(r, Mapping)], p.metrics)
        for p in promotions
    ]
    return StudyDetail(ranking, list(folds), attempts, trials)


@dataclass(frozen=True)
class PromotionLine:
    """One row of the promotion log (AC-20)."""

    decided_at: datetime
    strategy: str
    version_id: UUID
    frozen_hash: str | None
    step: str
    verdict: str
    decided_by: str
    reason_codes: tuple[str, ...]
    notes: str | None


def promotion_log(session: Session, page: int) -> list[PromotionLine]:
    """Page ``page`` (from 1) of every promotion, newest first, ``LOG_PAGE_SIZE`` per page."""
    rows = session.execute(
        select(models.Promotion, models.Strategy.name)
        .join(models.StrategyVersion, models.StrategyVersion.id == models.Promotion.strategy_version_id)
        .join(models.Strategy, models.Strategy.id == models.StrategyVersion.strategy_id)
        .order_by(models.Promotion.decided_at.desc(), models.Promotion.id.desc())
        .limit(LOG_PAGE_SIZE)
        .offset((page - 1) * LOG_PAGE_SIZE)
    ).all()
    return [
        PromotionLine(
            decided_at=p.decided_at,
            strategy=name,
            version_id=p.strategy_version_id,
            frozen_hash=p.frozen_hash,
            step=f"{p.from_stage} → {p.to_stage}",
            verdict=p.verdict,
            decided_by=p.decided_by,
            reason_codes=tuple(f"{r.get('check')}:{r.get('code')}" for r in p.reasons if isinstance(r, Mapping)),
            notes=p.notes,
        )
        for p, name in rows
    ]


@dataclass(frozen=True)
class PaperProgress:
    """A version in paper and how its record stands against the paper → live checks, right now (AC-21)."""

    frozen_hash: str
    strategy: str
    metrics: Mapping[str, object]
    reasons: Sequence[Mapping[str, object]]
    min_days: int | None = None
    trade_floor: int | None = None


def paper_progress(session: Session, now: datetime) -> list[PaperProgress]:
    """Every frozen hash whose stage is ``paper``, scored by the gate's own paper functions when read."""
    hashes = session.execute(
        select(models.Promotion.frozen_hash)
        .distinct()
        .where(models.Promotion.to_stage == Stage.PAPER, models.Promotion.frozen_hash.is_not(None))
    ).scalars()
    found = []
    for frozen in sorted(h for h in hashes if h is not None):
        if stage_of(session, frozen) != Stage.PAPER.value:
            continue
        evidence = paper_evidence(session, frozen, now)
        result = evaluate_paper(evidence.inputs)
        strategy = session.execute(
            select(models.Strategy.name)
            .join(models.StrategyVersion, models.StrategyVersion.strategy_id == models.Strategy.id)
            .where(models.StrategyVersion.id == evidence.version_id)
        ).scalar_one()
        t = evidence.inputs.thresholds
        found.append(
            PaperProgress(
                frozen,
                strategy,
                result.metrics,
                [r.to_json() for r in result.reasons],
                min_days=None if t is None else t.paper.min_days,
                trade_floor=None if t is None else t.paper.trade_floor(evidence.inputs.strategy_kind),
            )
        )
    return found
