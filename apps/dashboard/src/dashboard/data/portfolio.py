"""The Portfolio page's realized P&L since its run started (spec 0030 AC-21), read from Postgres as ``dashboard_app``.

The run is the one ``portfolio_trader_state`` names (``run_id``). Like Overview's figures (``data.pnl``), it is
NautilusTrader's realized P&L net of commissions plus booked funding: ``realized_pnl + funding_total`` over
the run's closed positions, plus ``funding_total`` over its open ones (funding is cash already paid or received).
"""

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
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
