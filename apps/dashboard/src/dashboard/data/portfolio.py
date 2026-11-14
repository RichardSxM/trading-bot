"""The Portfolio page's Postgres figures, read as ``dashboard_app``.

Realized P&L since its run started (spec 0030 AC-21): the run is the one the page's ``trader_state`` names
(``run_id``). Like Overview's figures (``data.pnl``), it is NautilusTrader's realized P&L net of commissions
plus booked funding: ``realized_pnl + funding_total`` over the run's closed positions, plus ``funding_total``
over its open ones (funding is cash already paid or received).

The churn (spec 0032 AC-8): for each of the last ``days`` UTC days, the notional traded (``quantity x price``)
and the fees paid, over the fills of every run of one trader, so a daily rebalance's cost can be judged.

The sizing history (spec 0033 AC-8): the last 30 ``book_plans`` rows of the page's run, newest first, each with
three counts from that run's decisions decided from its rebalance up to the next row's (or now): orders sent
(approved), refused (rejected), and no order (plan coins with no decision: band, too small, missing close, stale
quote).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from dashboard.ui.format import money
from trading_core.recording import models
from trading_core.recording.enums import PositionStatus, RiskVerdict


def run_realized(sessions: Callable[[], Session], env: str, run_id: UUID) -> Decimal:
    """Realized P&L plus funding of ``run_id`` in ``env``, in USD; 0 for a run with no positions yet."""
    closed = select(func.coalesce(func.sum(models.Position.realized_pnl + models.Position.funding_total), 0)).where(
        models.Position.env == env,
        models.Position.run_id == run_id,
        models.Position.status == PositionStatus.CLOSED,
    )
    open_funding = select(func.coalesce(func.sum(models.Position.funding_total), 0)).where(
        models.Position.env == env,
        models.Position.run_id == run_id,
        models.Position.status == PositionStatus.OPEN,
    )
    with sessions() as session:
        return money(
            Decimal(session.execute(closed).scalar_one()) + Decimal(session.execute(open_funding).scalar_one())
        )


@dataclass(frozen=True)
class DayChurn:
    """One UTC day's fills: the notional traded and the fees paid, in USD."""

    day: date
    notional: Decimal
    fees: Decimal


def daily_churn(
    sessions: Callable[[], Session], env: str, trader_id: str, today: date, days: int = 7
) -> tuple[DayChurn, ...]:
    """The last ``days`` UTC days up to ``today``, newest first, over every fill of ``trader_id``'s runs in ``env``;
    a day with no fill is 0."""
    first = today - timedelta(days=days - 1)
    day = cast(func.timezone("UTC", models.Fill.filled_at), Date)
    query = (
        select(
            day,
            func.sum(models.Fill.quantity * models.Fill.price),
            func.sum(models.Fill.fee),
        )
        .join(models.StrategyRun, models.StrategyRun.id == models.Fill.run_id)
        .where(
            models.Fill.env == env,
            models.StrategyRun.env == env,
            models.StrategyRun.trader_id == trader_id,
            models.Fill.filled_at >= datetime.combine(first, time(), UTC),
        )
        .group_by(day)
    )
    with sessions() as session:
        found = {row[0]: (Decimal(row[1]), Decimal(row[2])) for row in session.execute(query)}
    rows = []
    for offset in range(days):
        at = today - timedelta(days=offset)
        notional, fees = found.get(at, (Decimal(0), Decimal(0)))
        rows.append(DayChurn(at, money(notional), money(fees)))
    return tuple(rows)


BOOK_PLAN_ROWS = 30
_DAYS_PER_YEAR = Decimal(365)


@dataclass(frozen=True)
class BookPlanLine:
    """One rebalance on the Portfolio page: when and why, the annual unit vol in percent (None when not vol
    sized), the book gross, and the decision counts of its window."""

    rebalanced_at: datetime
    reason: str
    unit_vol_pct: Decimal | None
    book_gross: Decimal | None
    sent: int
    refused: int
    no_order: int


def _annual_pct(unit_vol: Decimal | None) -> Decimal | None:
    if unit_vol is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 28
        return (unit_vol * _DAYS_PER_YEAR.sqrt() * 100).quantize(Decimal("0.1"))


@dataclass(frozen=True)
class PlanHead:
    """One ``book_plans`` row as the panel reads it."""

    id: UUID
    rebalanced_at: datetime
    rebalanced_ns: int
    reason: str
    unit_vol: Decimal | None
    book_gross: Decimal | None


@dataclass(frozen=True)
class DecisionMark:
    """One decision of the run: when, on which coin, and the risk verdict."""

    decided_ns: int
    instrument_id: str
    risk_verdict: str


def count_lines(
    plans: Sequence[PlanHead], coins: Mapping[UUID, set[str]], decisions: Sequence[DecisionMark], now_ns: int
) -> tuple[BookPlanLine, ...]:
    """The panel lines for ``plans`` (newest first): each row's window runs from its rebalance up to the next
    newer row's, the newest up to ``now_ns``."""
    lines = []
    end = now_ns
    for row in plans:
        window = [d for d in decisions if row.rebalanced_ns <= d.decided_ns < end]
        decided = {d.instrument_id for d in window}
        lines.append(
            BookPlanLine(
                rebalanced_at=row.rebalanced_at,
                reason=row.reason,
                unit_vol_pct=_annual_pct(row.unit_vol),
                book_gross=None if row.book_gross is None else row.book_gross.quantize(Decimal("0.01")),
                sent=sum(1 for d in window if d.risk_verdict == RiskVerdict.APPROVED),
                refused=sum(1 for d in window if d.risk_verdict == RiskVerdict.REJECTED),
                no_order=len(coins.get(row.id, set()) - decided),
            )
        )
        end = row.rebalanced_ns
    return tuple(lines)


def book_plan_lines(
    sessions: Callable[[], Session], env: str, run_id: UUID, now_ns: int, limit: int = BOOK_PLAN_ROWS
) -> tuple[BookPlanLine, ...]:
    """The last ``limit`` ``book_plans`` rows of ``run_id`` in ``env``, newest first, with their counts."""
    plan = models.BookPlan
    decision = models.Decision
    with sessions() as session:
        plans = [
            PlanHead(*row)
            for row in session.execute(
                select(plan.id, plan.rebalanced_at, plan.rebalanced_ns, plan.reason, plan.unit_vol, plan.book_gross)
                .where(plan.env == env, plan.run_id == run_id)
                .order_by(plan.rebalanced_ns.desc())
                .limit(limit)
            )
        ]
        if not plans:
            return ()
        coins: dict[UUID, set[str]] = {row.id: set() for row in plans}
        for plan_id, instrument in session.execute(
            select(models.BookPlanCoin.book_plan_id, models.BookPlanCoin.instrument_id).where(
                models.BookPlanCoin.book_plan_id.in_(list(coins))
            )
        ):
            coins[plan_id].add(instrument)
        decisions = [
            DecisionMark(*row)
            for row in session.execute(
                select(decision.decided_ns, decision.instrument_id, decision.risk_verdict).where(
                    decision.env == env, decision.run_id == run_id, decision.decided_ns >= plans[-1].rebalanced_ns
                )
            )
        ]
    return count_lines(plans, coins, decisions, now_ns)
