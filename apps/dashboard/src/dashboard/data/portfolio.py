"""The Portfolio page's Postgres figures, read as ``dashboard_app``.

Realized P&L since its run started (spec 0030 AC-21): the run is the one the page's ``trader_state`` names
(``run_id``). Like Overview's figures (``data.pnl``), it is NautilusTrader's realized P&L net of commissions
plus booked funding: ``realized_pnl + funding_total`` over the run's closed positions, plus ``funding_total``
over its open ones (funding is cash already paid or received).

The churn (spec 0032 AC-8): for each of the last ``days`` UTC days, the notional traded (``quantity x price``)
and the fees paid, over the fills of every run of one trader, so a daily rebalance's cost can be judged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from dashboard.ui.format import money
from trading_core.recording import models
from trading_core.recording.enums import PositionStatus


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
