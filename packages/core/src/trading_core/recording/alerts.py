"""The ``alerts`` write and read API (spec 0013): the only way an episode reaches Postgres.

An episode is identified by ``(env, fingerprint, opened_at)``. ``save`` writes its latest state
and is safe to repeat: counts and times only move forward, and a resolved row never reopens, so a
stale or replayed save cannot undo a newer one. Sync, for a worker thread.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import RowMapping, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading_core.alerts import daily
from trading_core.alerts.messages import FILL_FIELDS
from trading_core.alerts.model import Delivery, Episode, Tier
from trading_core.alerts.rules import DAILY, TRADER_DOWN
from trading_core.recording.errors import AlertConflictError
from trading_core.recording.models import Alert, Fill, Instrument, Position
from trading_core.recording.repository import check_env, table_of
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
        check_env(episode.env)
        table = table_of(Alert)
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
                    # A save older than the row (fewer sends, or seen earlier) keeps the row's delivery.
                    delivery=case(
                        (
                            (table.c.sends <= episode.sends) & (table.c.last_seen_at <= episode.last_seen_at),
                            episode.delivery.value,
                        ),
                        else_=table.c.delivery,
                    ),
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
        table = table_of(Alert)
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

    def fill_fields(self, trade_id: str) -> dict[str, str] | None:
        """The ``fills`` row of ``trade_id`` and its instrument, as the fields a fill push names (AC-8).

        Keys are ``messages.FILL_FIELDS``, values text. The newest row wins should two runs share
        the trade id. None until the trader's writer has stored the fill.
        """
        env = get_settings().trading_env
        fill, instrument = Fill, Instrument
        query = (
            select(
                fill.instrument_id,
                fill.side,
                fill.quantity,
                fill.price,
                fill.fee,
                fill.fee_currency,
                instrument.base_currency,
                instrument.lot_size,
                instrument.tick_size,
            )
            .join(instrument, instrument.instrument_id == fill.instrument_id)
            .where(fill.env == env, fill.trade_id == trade_id)
            .order_by(fill.filled_at.desc())
            .limit(1)
        )
        with self._sessions() as session:
            row = session.execute(query).first()
        if row is None:
            return None
        return {name: str(value) for name, value in zip(FILL_FIELDS, row, strict=True)}

    def daily_sent(self, fingerprint: str) -> bool:
        """True when this env holds a ``daily`` row with ``fingerprint`` (AC-18's sent marker), any delivery."""
        env = get_settings().trading_env
        table = table_of(Alert)
        query = select(table.c.id).where(
            table.c.env == env, table.c.rule == DAILY.id, table.c.fingerprint == fingerprint
        )
        with self._sessions() as session:
            return session.execute(query.limit(1)).first() is not None

    def daily_figures(self, now: datetime) -> daily.DailyFigures:
        """The note's numbers at ``now`` (AC-18, *Value sourcing*); ``backup`` is left for the caller.

        Alerts opened in the last 24 h by tier (``daily`` rows left out), rows still open, rows still
        ``failed`` (newest titles first), ``trader_down`` time in the last 24 h, and the count and
        realized P&L per currency of positions closed in the UTC day before ``now``'s.
        """
        env = get_settings().trading_env
        alerts, positions = table_of(Alert), table_of(Position)
        since = now - daily.WINDOW
        day_start, day_end = daily.yesterday(now)
        with self._sessions() as session:
            opened = session.execute(
                select(alerts.c.severity, func.count())
                .where(alerts.c.env == env, alerts.c.opened_at >= since, alerts.c.rule != DAILY.id)
                .group_by(alerts.c.severity)
            ).all()
            still_open = session.execute(
                select(func.count()).where(alerts.c.env == env, alerts.c.resolved_at.is_(None))
            ).scalar_one()
            failed: Sequence[str] = (
                session.execute(
                    select(alerts.c.title)
                    .where(alerts.c.env == env, alerts.c.delivery == Delivery.FAILED.value)
                    .order_by(alerts.c.opened_at.desc())
                )
                .scalars()
                .all()
            )
            spans = session.execute(
                select(alerts.c.opened_at, alerts.c.resolved_at).where(
                    alerts.c.env == env,
                    alerts.c.rule == TRADER_DOWN.id,
                    alerts.c.opened_at < now,
                    or_(alerts.c.resolved_at.is_(None), alerts.c.resolved_at > since),
                )
            ).all()
            closed = session.execute(
                select(positions.c.currency, func.count(), func.sum(positions.c.realized_pnl))
                .where(
                    positions.c.env == env,
                    positions.c.closed_at >= day_start,
                    positions.c.closed_at < day_end,
                )
                .group_by(positions.c.currency)
            ).all()
        return daily.DailyFigures(
            opened={Tier(str(severity)): _int(count) for severity, count in opened},
            still_open=_int(still_open),
            failed=len(failed),
            failed_titles=tuple(str(t) for t in failed[: daily.FAILED_TITLES]),
            down_s=daily.down_seconds(((_at(o), _at_or_none(r)) for o, r in spans), now),
            trades=sum(_int(count) for _, count, _ in closed),
            pnl={str(currency): _decimal(total) for currency, _, total in closed},
            trade_day=day_start.date(),
        )

    def ping(self) -> None:
        """``SELECT 1``; raises a SQLAlchemy error when Postgres is unreachable (spec 0013 AC-4)."""
        with self._sessions() as session:
            session.execute(text("SELECT 1"))


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


def _decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"expected a Decimal, got {value!r}")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected an int, got {value!r}")
    return value
