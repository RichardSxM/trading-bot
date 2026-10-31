"""covers: spec 0027 AC-2 (migration 0014 widens the strategies kind check, and its downgrade refuses trend rows)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from .conftest import create_database, drop_database, migrate, owner_url

INSERT = text("INSERT INTO strategies (id, name, kind) VALUES (:id, :name, :kind)")


def _insert(engine: Engine, name: str, kind: str) -> None:
    with engine.begin() as conn:
        conn.execute(INSERT, {"id": uuid.uuid4(), "name": name, "kind": kind})


def _kind_check(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_strategies_kind_valid'")
        ).scalar_one()


@pytest.fixture
def at_0013() -> Iterator[Engine]:
    name = create_database()
    engine = create_engine(owner_url(name), poolclass=pool.NullPool)
    migrate(engine, "0013_alerter_fx_rates")
    yield engine
    engine.dispose()
    drop_database(name)


def test_upgrade_keeps_old_rows_accepts_trend_and_refuses_other_kinds(at_0013):
    _insert(at_0013, "a_scalp", "scalping")
    _insert(at_0013, "an_hour", "hour_level")
    with pytest.raises(IntegrityError):
        _insert(at_0013, "early_trend", "trend")

    migrate(at_0013)

    _insert(at_0013, "tsmom", "trend")
    with pytest.raises(IntegrityError):
        _insert(at_0013, "odd", "other")
    with at_0013.connect() as conn:
        kinds = conn.execute(text("SELECT kind FROM strategies")).scalars().all()
    assert sorted(kinds) == ["hour_level", "scalping", "trend"]


def test_downgrade_without_trend_rows_restores_the_two_value_check(at_0013):
    before = _kind_check(at_0013)
    migrate(at_0013)
    assert "trend" in _kind_check(at_0013)
    _insert(at_0013, "a_scalp", "scalping")

    migrate(at_0013, "0013_alerter_fx_rates", down=True)

    assert _kind_check(at_0013) == before
    with pytest.raises(IntegrityError):
        _insert(at_0013, "tsmom", "trend")


def test_downgrade_refuses_while_a_trend_row_exists(at_0013):
    migrate(at_0013)
    _insert(at_0013, "tsmom", "trend")

    with pytest.raises(RuntimeError, match="1 strategies row"):
        migrate(at_0013, "0013_alerter_fx_rates", down=True)

    assert "trend" in _kind_check(at_0013)
    with at_0013.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014_trend_kind"
