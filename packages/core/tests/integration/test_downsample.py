"""covers: spec 0002 AC-12 (downsample_snapshots thins raw snapshots older than 90 days to hourly)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import Env, Granularity

from .conftest import World

STEP = timedelta(minutes=20)
# Leave a margin around the 90 day cutoff so seeding and the function's now() cannot disagree.
MARGIN = timedelta(hours=2)


def _ns(at: datetime) -> int:
    return int((at - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) * 1_000_000_000


def _seed(now: datetime) -> list[datetime]:
    """Capture times every 20 minutes over 100 days, skipping the hours around the cutoff."""
    cutoff = now - timedelta(days=90)
    times, at = [], now - timedelta(days=100)
    while at < now:
        if not cutoff - MARGIN < at < cutoff + MARGIN:
            times.append(at)
        at += STEP
    return times


def test_downsample_keeps_the_last_raw_row_per_series_hour(engine: Engine, session: Session, world: World):
    position = world.position()
    repository.upsert_position(session, position)
    session.flush()
    position_id = session.execute(
        select(models.Position.id).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    account_ref = f"ACC-{uuid7().hex[-8:]}"
    now = datetime.now(UTC)
    times = _seed(now)
    session.execute(
        insert(models.PositionSnapshot),
        [
            {
                "id": uuid7(),
                "env": Env.DEMO,
                "position_id": position_id,
                "quantity": Decimal("0.001"),
                "mark_px": Decimal(60000) + i,
                "unrealized_pnl": Decimal(0),
                "currency": "USD",
                "granularity": Granularity.RAW,
                "captured_at": at,
                "captured_ns": _ns(at),
            }
            for i, at in enumerate(times)
        ],
    )
    session.execute(
        insert(models.AccountSnapshot),
        [
            {
                "id": uuid7(),
                "env": Env.DEMO,
                "run_id": None,
                "account_ref": account_ref,
                "balance_total": Decimal(10000) + i,
                "balance_free": Decimal(9000),
                "margin_used": Decimal(1000),
                "currency": "USD",
                "granularity": Granularity.RAW,
                "captured_at": at,
                "captured_ns": _ns(at),
            }
            for i, at in enumerate(times)
        ],
    )
    session.commit()

    cutoff = now - timedelta(days=90)
    old = [at for at in times if at < cutoff]
    recent = [at for at in times if at >= cutoff]
    last_in_hour: dict[datetime, datetime] = {}
    for at in old:
        hour = at.replace(minute=0, second=0, microsecond=0)
        last_in_hour[hour] = max(last_in_hour.get(hour, at), at)

    with engine.begin() as conn:
        deleted: int = conn.execute(text("SELECT downsample_snapshots()")).scalar_one()
    with engine.begin() as conn:
        deleted_again: int = conn.execute(text("SELECT downsample_snapshots()")).scalar_one()

    for model, series in (
        (models.PositionSnapshot, models.PositionSnapshot.position_id == position_id),
        (models.AccountSnapshot, models.AccountSnapshot.account_ref == account_ref),
    ):
        rows = session.execute(select(model.captured_at, model.granularity).where(series)).all()
        hourly = sorted(at for at, granularity in rows if granularity == Granularity.HOURLY)
        raw = sorted(at for at, granularity in rows if granularity == Granularity.RAW)
        assert hourly == sorted(last_in_hour.values())
        assert raw == recent
    assert deleted >= 2 * (len(old) - len(last_in_hour))
    assert deleted_again == 0
    assert session.execute(
        select(func.count())
        .select_from(models.PositionSnapshot)
        .where(models.PositionSnapshot.position_id == position_id)
    ).scalar_one() == len(last_in_hour) + len(recent)


def test_a_late_raw_row_in_a_thinned_hour_leaves_one_row(engine: Engine, session: Session, world: World):
    position = world.position()
    repository.upsert_position(session, position)
    session.flush()
    position_id = session.execute(
        select(models.Position.id).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    hour = (datetime.now(UTC) - timedelta(days=100)).replace(minute=0, second=0, microsecond=0)
    kept, late = hour + timedelta(minutes=10), hour + timedelta(minutes=30)
    session.execute(
        insert(models.PositionSnapshot),
        [
            {
                "id": uuid7(),
                "env": Env.DEMO,
                "position_id": position_id,
                "quantity": Decimal("0.001"),
                "mark_px": Decimal(60000),
                "unrealized_pnl": Decimal(0),
                "currency": "USD",
                "granularity": granularity,
                "captured_at": at,
                "captured_ns": _ns(at),
            }
            for at, granularity in ((kept, Granularity.HOURLY), (late, Granularity.RAW))
        ],
    )
    session.commit()

    with engine.begin() as conn:
        conn.execute(text("SELECT downsample_snapshots()"))

    rows = session.execute(
        select(models.PositionSnapshot.captured_at, models.PositionSnapshot.granularity).where(
            models.PositionSnapshot.position_id == position_id
        )
    ).all()
    assert [tuple(row) for row in rows] == [(late, Granularity.HOURLY)]
