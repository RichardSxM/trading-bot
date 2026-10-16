"""covers: spec 0013 AC-8 (the fill lookup and its grants), AC-9 (one open episode per fingerprint, restart
load), AC-14 (failed rows for the retry cycle), and the ``alerts`` write rules: saves only move forward, a
resolved row never reopens.

Runs as ``alerter_app``, the role the alerter starts with, so its grants are checked too.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

from trading_core.alerts.model import Delivery, Episode, Tier
from trading_core.db_roles import ALERTER_ROLE
from trading_core.recording import repository
from trading_core.recording.alerts import AlertRepository
from trading_core.recording.enums import OrderOrigin
from trading_core.recording.errors import AlertConflictError, EnvMismatchError

from .conftest import role_url

# Far from the other tests' times, and a fingerprint per test, so tests sharing the database never meet.
NOW = datetime(2031, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(database: str, trading_env: None) -> Iterator[AlertRepository]:
    engine: Engine = create_engine(role_url(database, ALERTER_ROLE), poolclass=pool.NullPool)
    yield AlertRepository(sessionmaker(engine))
    engine.dispose()


def episode(fingerprint: str, **overrides: object) -> Episode:
    values: dict[str, object] = {
        "env": "demo",
        "rule": "trader_down",
        "fingerprint": fingerprint,
        "severity": Tier.CRITICAL,
        "source": "redis",
        "title": "[demo] Trader down",
        "message": "No heartbeat from the trader for 50 s.",
        "details": {"age_s": 50},
        "opened_at": NOW,
        "last_seen_at": NOW,
    }
    values.update(overrides)
    return Episode(**values)  # type: ignore[arg-type]


def _row(engine: Engine, row_id: int) -> dict[str, object]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT * FROM alerts WHERE id = :id"), {"id": row_id}).mappings().one())


def test_save_inserts_then_updates_the_same_row(repo, engine):
    first = episode("fp-update")
    row_id = repo.save(first)

    again = repo.save(replace(first, sends=1, last_sent_at=NOW, delivery=Delivery.SENT))

    assert again == row_id
    row = _row(engine, row_id)
    assert (row["delivery"], row["sends"], row["occurrences"], row["resolved_at"]) == ("sent", 1, 1, None)
    assert row["details"] == {"age_s": 50}


def test_a_stale_save_never_moves_a_row_back_or_reopens_it(repo, engine):
    first = episode("fp-stale")
    resolved = replace(first, resolved_at=NOW + timedelta(minutes=3), sends=1, occurrences=4)
    row_id = repo.save(resolved)

    repo.save(first)  # an older state, e.g. a push outcome recorded late

    row = _row(engine, row_id)
    assert row["resolved_at"] == NOW + timedelta(minutes=3)
    assert (row["sends"], row["occurrences"]) == (1, 4)


def test_one_open_episode_per_fingerprint_and_a_new_one_after_it_resolves(repo, engine):
    first = episode("fp-open")
    first_id = repo.save(first)

    with pytest.raises(AlertConflictError):
        repo.save(episode("fp-open", opened_at=NOW + timedelta(minutes=1)))

    repo.save(replace(first, resolved_at=NOW + timedelta(minutes=2)))
    second_id = repo.save(episode("fp-open", opened_at=NOW + timedelta(minutes=5)))
    assert second_id != first_id


def test_load_active_returns_open_episodes_and_recent_failed_ones(repo):
    repo.save(episode("fp-load-open", details={"missing": True}))
    repo.save(episode("fp-load-failed", resolved_at=NOW, delivery=Delivery.FAILED))
    repo.save(episode("fp-load-old", opened_at=NOW - timedelta(days=2), resolved_at=NOW, delivery=Delivery.FAILED))
    repo.save(episode("fp-load-done", resolved_at=NOW, delivery=Delivery.SENT))

    loaded = {
        e.fingerprint: e for e in repo.load_active(NOW - timedelta(days=1)) if e.fingerprint.startswith("fp-load")
    }

    assert set(loaded) == {"fp-load-open", "fp-load-failed"}
    opened = loaded["fp-load-open"]
    assert opened.id is not None
    assert (opened.severity, opened.delivery, opened.opened_at, dict(opened.details)) == (
        Tier.CRITICAL,
        Delivery.PENDING,
        NOW,
        {"missing": True},
    )
    assert opened.opened_at.utcoffset() == timedelta(0)


def test_another_env_is_refused(repo):
    with pytest.raises(EnvMismatchError):
        repo.save(episode("fp-env", env="live"))


def test_the_alerter_cannot_delete_alerts(database, trading_env):
    engine = create_engine(role_url(database, ALERTER_ROLE), poolclass=pool.NullPool)
    try:
        with Session(engine) as session, pytest.raises(Exception, match="permission denied"):
            session.execute(text("DELETE FROM alerts"))
    finally:
        engine.dispose()


def test_fill_fields_reads_the_fill_and_its_instrument_as_alerter_app(repo, session, world):
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    fill = world.fill(order.client_order_id, quantity=Decimal("0.001"), price=Decimal("67412.5"))
    repository.record_fill(session, fill)
    session.commit()

    fields = repo.fill_fields(fill.trade_id)

    assert fields is not None
    assert {k: fields[k] for k in ("side", "base_currency", "fee_currency", "lot_size", "tick_size")} == {
        "side": "buy",
        "base_currency": "BTC",
        "fee_currency": "USD",
        "lot_size": "0.0001",
        "tick_size": "0.5",
    }
    assert Decimal(fields["quantity"]) == Decimal("0.001")
    assert Decimal(fields["price"]) == Decimal("67412.5")
    assert repo.fill_fields("T-not-stored") is None
