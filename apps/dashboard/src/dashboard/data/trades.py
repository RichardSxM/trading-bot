"""The two Trades lists (spec 0006 AC-19), read from Postgres as ``dashboard_app``.

Paper runs in this env only; the queries themselves live in ``trading_core.recording.queries``.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from trading_core.recording.enums import Env
from trading_core.recording.queries import FillLine, RoundTrip, fills_page, round_trips_page


@dataclass(frozen=True)
class TradesView:
    """One page of closed round trips and one page of fills, each newest first."""

    round_trips: Sequence[RoundTrip]
    fills: Sequence[FillLine]


def trades_view(sessions: Callable[[], Session], env: str, page: int, fills_page_no: int) -> TradesView:
    """Page ``page`` of the round trips and page ``fills_page_no`` of the fills (both from 1)."""
    with sessions() as session:
        return TradesView(
            round_trips=round_trips_page(session, Env(env), page),
            fills=fills_page(session, Env(env), fills_page_no),
        )
