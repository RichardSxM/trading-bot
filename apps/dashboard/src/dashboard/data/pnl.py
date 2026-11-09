"""The P&L figures under Overview's position card (spec 0006 AC-17), read from Postgres as ``dashboard_app``.

Paper runs in this env of the main trader only (``trader_id``, spec 0030 AC-16). Realized P&L is
NautilusTrader's, net of commissions; the figures add each position's booked funding (spec 0011
AC-18): ``realized_pnl + funding_total`` over closed positions, plus ``funding_total`` over open ones.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from dashboard.ui.format import money
from trading_core.recording import models
from trading_core.recording.enums import PositionStatus, RunMode


@dataclass(frozen=True)
class PnlFigures:
    """Net P&L today (UTC) and all time, fees today, and the funding the open positions booked so far, in USD."""

    realized_today: Decimal
    realized_all: Decimal
    fees_today: Decimal
    open_funding: Decimal = Decimal(0)


def _positions(
    env: str,
    trader_id: str,
    status: PositionStatus,
    figure: ColumnElement[Decimal] | InstrumentedAttribute[Decimal],
) -> Select[Decimal]:
    return (
        select(func.coalesce(func.sum(figure), 0))
        .join(models.StrategyRun, models.StrategyRun.id == models.Position.run_id)
        .where(
            models.StrategyRun.mode == RunMode.PAPER,
            models.StrategyRun.env == env,
            models.StrategyRun.trader_id == trader_id,
            models.Position.env == env,
            models.Position.status == status,
        )
    )


def _realized(session: Session, env: str, trader_id: str, since: datetime | None) -> Decimal:
    query = _positions(
        env, trader_id, PositionStatus.CLOSED, models.Position.realized_pnl + models.Position.funding_total
    )
    if since is not None:
        query = query.where(models.Position.closed_at >= since)
    return Decimal(session.execute(query).scalar_one())


def _open_funding(session: Session, env: str, trader_id: str) -> Decimal:
    query = _positions(env, trader_id, PositionStatus.OPEN, models.Position.funding_total)
    return Decimal(session.execute(query).scalar_one())


def _fees(session: Session, env: str, trader_id: str, since: datetime) -> Decimal:
    query = (
        select(func.coalesce(func.sum(models.Fill.fee), 0))
        .join(models.StrategyRun, models.StrategyRun.id == models.Fill.run_id)
        .where(
            models.StrategyRun.mode == RunMode.PAPER,
            models.StrategyRun.env == env,
            models.StrategyRun.trader_id == trader_id,
            models.Fill.env == env,
            models.Fill.filled_at >= since,
        )
    )
    return Decimal(session.execute(query).scalar_one())


def pnl_figures(sessions: Callable[[], Session], env: str, now: datetime, trader_id: str) -> PnlFigures:
    """The figures of ``trader_id``'s paper runs as of ``now`` (timezone aware UTC); today starts at UTC midnight.

    Funding the open positions booked so far counts in both P&L figures, as well as on its own.
    """
    if now.utcoffset() is None:
        raise ValueError("naive datetime: now must be timezone aware UTC")
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    with sessions() as session:
        open_funding = _open_funding(session, env, trader_id)
        return PnlFigures(
            realized_today=money(_realized(session, env, trader_id, midnight) + open_funding),
            realized_all=money(_realized(session, env, trader_id, None) + open_funding),
            fees_today=money(_fees(session, env, trader_id, midnight)),
            open_funding=money(open_funding),
        )
