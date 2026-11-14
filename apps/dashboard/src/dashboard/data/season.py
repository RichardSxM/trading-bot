"""The season curve on Overview: the book's equity per hour over every run of its current strategy.

Read as ``dashboard_app``. The season is every paper run in ``env`` of the run's trader whose version belongs to
the same strategy as the run, so a deploy or a settings change (a new version, a new run) continues the curve and a
switch to another strategy starts a new one. Each hour's equity is the last ``balance_total`` (USD) recorded in
that hour plus the last ``unrealized_pnl`` of each position snapshotted in that hour and still open at its end
(a position closed within the hour is in that hour's balance already). Hours without a balance row
are skipped; the curve is at most ``MAX_DAYS`` long.
"""

from collections.abc import Callable
from datetime import UTC
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from dashboard.live.standings import Season, SeasonPoint

MAX_DAYS: Final = 120

SEASON_SQL: Final = text(
    """
    WITH current AS (
        SELECT r.trader_id, v.strategy_id, s.name
        FROM strategy_runs r
        JOIN strategy_versions v ON v.id = r.strategy_version_id
        JOIN strategies s ON s.id = v.strategy_id
        WHERE r.id = :run AND r.env = :env
    ),
    runs AS (
        SELECT r.id
        FROM strategy_runs r
        JOIN strategy_versions v ON v.id = r.strategy_version_id
        JOIN current c ON c.trader_id = r.trader_id AND c.strategy_id = v.strategy_id
        WHERE r.env = :env AND r.mode = 'paper'
    ),
    balance AS (
        SELECT date_bin('1 hour', a.captured_at, TIMESTAMPTZ '2000-01-01') AS hour,
               (array_agg(a.balance_total ORDER BY a.captured_ns DESC))[1] AS total
        FROM account_snapshots a
        WHERE a.env = :env AND a.currency = 'USD' AND a.run_id IN (SELECT id FROM runs)
          AND a.captured_at > now() - make_interval(days => :days)
        GROUP BY 1
    ),
    open_pnl AS (
        SELECT hour, sum(last_pnl) AS total
        FROM (
            SELECT date_bin('1 hour', s.captured_at, TIMESTAMPTZ '2000-01-01') AS hour,
                   (array_agg(s.unrealized_pnl ORDER BY s.captured_ns DESC))[1] AS last_pnl
            FROM position_snapshots s
            JOIN positions p ON p.id = s.position_id
            WHERE s.env = :env AND p.run_id IN (SELECT id FROM runs)
              AND s.captured_at > now() - make_interval(days => :days)
              -- a position closed within the hour is in that hour's balance already
              AND (p.closed_at IS NULL
                   OR p.closed_at >= date_bin('1 hour', s.captured_at, TIMESTAMPTZ '2000-01-01') + interval '1 hour')
            GROUP BY 1, s.position_id
        ) per_position
        GROUP BY hour
    )
    SELECT b.hour, b.total + coalesce(o.total, 0) AS equity, (SELECT name FROM current) AS strategy
    FROM balance b LEFT JOIN open_pnl o ON o.hour = b.hour
    ORDER BY b.hour
    """
)


def season_curve(sessions: Callable[[], Session], env: str, run: UUID, days: int = MAX_DAYS) -> Season:
    """The season of ``run`` (the book trader's current run) in ``env``; empty when the run is not recorded."""
    with sessions() as session:
        rows = session.execute(SEASON_SQL, {"run": run, "env": env, "days": days}).all()
    points = tuple(SeasonPoint(hour.astimezone(UTC), Decimal(equity)) for hour, equity, _ in rows)
    strategy = rows[0][2] if rows else None
    return Season(strategy if isinstance(strategy, str) else None, points)
