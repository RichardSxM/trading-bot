"""The alert episodes Overview's Activity feed shows (spec 0013 AC-16), read from Postgres as ``dashboard_app``.

This env's episodes opened in the last 24 h, newest first. The dashboard only reads ``alerts``;
the alerter writes it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.alerts.model import Delivery, Tier
from trading_core.recording.models import Alert

WINDOW: Final = timedelta(hours=24)
# The feed lists at most this many episodes, so an alert storm can't bury the commands.
LIMIT: Final = 50


@dataclass(frozen=True)
class AlertLine:
    """One episode as the feed shows it: when it opened, how loud, its title (without the env tag), its state."""

    id: int
    opened_at: datetime
    severity: Tier
    title: str
    resolved: bool
    delivery: Delivery


def recent_alerts(sessions: Callable[[], Session], env: str, now: datetime) -> Sequence[AlertLine]:
    """``env``'s episodes opened since ``now`` minus 24 h, newest first, at most ``LIMIT``."""
    query = (
        select(Alert.id, Alert.opened_at, Alert.severity, Alert.title, Alert.resolved_at, Alert.delivery)
        .where(Alert.env == env, Alert.opened_at >= now - WINDOW)
        .order_by(Alert.opened_at.desc(), Alert.id.desc())
        .limit(LIMIT)
    )
    with sessions() as session:
        rows = session.execute(query).all()
    prefix = f"[{env}] "
    return [
        AlertLine(
            id=row.id,
            opened_at=row.opened_at.astimezone(UTC),
            severity=Tier(row.severity),
            title=row.title.removeprefix(prefix),
            resolved=row.resolved_at is not None,
            delivery=Delivery(row.delivery),
        )
        for row in rows
    ]
