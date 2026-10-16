"""The ``alerts`` write and read API (spec 0013): the only way an episode reaches Postgres.

An episode is identified by ``(env, fingerprint, opened_at)``. ``save`` writes its latest state
and is safe to repeat: counts and times only move forward, and a resolved row never reopens, so a
stale or replayed save cannot undo a newer one. Sync, for a worker thread.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import RowMapping, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading_core.alerts.model import Delivery, Episode, Tier
from trading_core.recording.errors import AlertConflictError
from trading_core.recording.models import Alert
from trading_core.recording.repository import _check_env, _table
from trading_core.settings import get_settings

_OPEN = "resolved_at IS NULL"


class AlertRepository:
    """Stores and loads alert episodes for this process's ``TRADING_ENV``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def save(self, episode: Episode) -> int:
        """Insert or update the episode's row; returns its id.

        Raises ``EnvMismatchError`` for another env, and ``AlertConflictError`` when a different
        open episode already holds the fingerprint.
        """
        _check_env(episode.env)
        table = _table(Alert)
        with self._sessions.begin() as session:
            changed = session.execute(
                update(table)
                .where(
                    table.c.env == episode.env,
                    table.c.fingerprint == episode.fingerprint,
                    table.c.opened_at == episode.opened_at,
                )
                .values(
                    last_seen_at=func.greatest(table.c.last_seen_at, episode.last_seen_at),
                    resolved_at=func.coalesce(table.c.resolved_at, episode.resolved_at),
                    occurrences=func.greatest(table.c.occurrences, episode.occurrences),
                    sends=func.greatest(table.c.sends, episode.sends),
                    last_sent_at=func.greatest(table.c.last_sent_at, episode.last_sent_at),
                    delivery=episode.delivery.value,
                )
                .returning(table.c.id)
            ).first()
            if changed is not None:
                return int(changed[0])
            inserted = session.execute(
                insert(table)
                .values(_values(episode))
                .on_conflict_do_nothing(index_elements=["env", "fingerprint"], index_where=text(_OPEN))
                .returning(table.c.id)
            ).first()
            if inserted is None:
                raise AlertConflictError(f"alerts {episode.fingerprint!r} already has another open episode")
            return int(inserted[0])

    def load_active(self, failed_since: datetime) -> list[Episode]:
        """The open episodes, plus failed ones opened at or after ``failed_since`` (for the retry cycle)."""
        env = get_settings().trading_env
        table = _table(Alert)
        query = (
            select(table)
            .where(
                table.c.env == env,
                or_(
                    table.c.resolved_at.is_(None),
                    (table.c.delivery == Delivery.FAILED.value) & (table.c.opened_at >= failed_since),
                ),
            )
            .order_by(table.c.opened_at)
        )
        with self._sessions() as session:
            return [_episode(row) for row in session.execute(query).mappings()]


def _values(episode: Episode) -> dict[str, object]:
    return {
        "env": episode.env,
        "rule": episode.rule,
        "fingerprint": episode.fingerprint,
        "severity": episode.severity.value,
        "source": episode.source,
        "title": episode.title,
        "message": episode.message,
        "details": dict(episode.details),
        "opened_at": episode.opened_at,
        "last_seen_at": episode.last_seen_at,
        "resolved_at": episode.resolved_at,
        "occurrences": episode.occurrences,
        "sends": episode.sends,
        "last_sent_at": episode.last_sent_at,
        "delivery": episode.delivery.value,
    }


def _episode(row: RowMapping) -> Episode:
    details = row["details"]
    return Episode(
        env=str(row["env"]),
        rule=str(row["rule"]),
        fingerprint=str(row["fingerprint"]),
        severity=Tier(str(row["severity"])),
        source=str(row["source"]),
        title=str(row["title"]),
        message=str(row["message"]),
        details=dict(details) if isinstance(details, Mapping) else {},
        opened_at=_at(row["opened_at"]),
        last_seen_at=_at(row["last_seen_at"]),
        resolved_at=_at_or_none(row["resolved_at"]),
        occurrences=_int(row["occurrences"]),
        sends=_int(row["sends"]),
        last_sent_at=_at_or_none(row["last_sent_at"]),
        delivery=Delivery(str(row["delivery"])),
        id=_int(row["id"]),
    )


def _at(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {value!r}")
    return value.astimezone(UTC)


def _at_or_none(value: object) -> datetime | None:
    return None if value is None else _at(value)


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected an int, got {value!r}")
    return value
