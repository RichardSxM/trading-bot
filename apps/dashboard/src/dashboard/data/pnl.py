"""The P&L figures under Overview's position card (spec 0006 AC-17), read from Postgres as ``dashboard_app``.

Paper runs in this env only. Realized P&L is NautilusTrader's, net of commissions; funding is
not recorded yet, so every figure is before funding.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_core.recording import models
from trading_core.recording.enums import PositionStatus, RunMode


@dataclass(frozen=True)
class PnlFigures:
    """Realized P&L today (UTC) and all time over closed positions, and fees today, in USD."""

    realized_today: Decimal
    realized_all: Decimal
    fees_today: Decimal


def _realized(session: Session, env: str, since: datetime | None) -> Decimal:
    query = (
        select(func.coalesce(func.sum(models.Position.realized_pnl), 0))
        .join(models.StrategyRun, models.StrategyRun.id == models.Position.run_id)
        .where(
            models.StrategyRun.mode == RunMode.PAPER,
            models.StrategyRun.env == env,
            models.Position.env == env,
            models.Position.status == PositionStatus.CLOSED,
        )
    )
    if since is not None:
        query = query.where(models.Position.closed_at >= since)
    return Decimal(session.execute(query).scalar_one())


def _fees(session: Session, env: str, since: datetime) -> Decimal:
    query = (
        select(func.coalesce(func.sum(models.Fill.fee), 0))
        .join(models.StrategyRun, models.StrategyRun.id == models.Fill.run_id)
        .where(
            models.StrategyRun.mode == RunMode.PAPER,
            models.StrategyRun.env == env,
            models.Fill.env == env,
            models.Fill.filled_at >= since,
        )
    )
    return Decimal(session.execute(query).scalar_one())


def pnl_figures(sessions: Callable[[], Session], env: str, now: datetime) -> PnlFigures:
    """The three figures as of ``now`` (timezone aware UTC); today starts at UTC midnight."""
    if now.utcoffset() is None:
        raise ValueError("naive datetime: now must be timezone aware UTC")
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    with sessions() as session:
        return PnlFigures(
            realized_today=_realized(session, env, midnight),
            realized_all=_realized(session, env, None),
            fees_today=_fees(session, env, midnight),
        )
