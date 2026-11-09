"""covers: spec 0030 AC-14 (migration 0015: ``control_commands.slot``, key ``(id, slot)``, old rows become
``main``'s, and the downgrade deletes the portfolio rows and restores the key on ``id``)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from .conftest import create_database, drop_database, migrate, owner_url

INSERT = text(
    "INSERT INTO control_commands (id, env, cmd, issued_at, expires_at, received_at, outcome, details)"
    " VALUES (:id, 'demo', 'pause', now(), now(), now(), 'received', '{}')"
)
INSERT_SLOT = text(
    "INSERT INTO control_commands (id, env, cmd, issued_at, expires_at, received_at, outcome, details, slot)"
    " VALUES (:id, 'demo', 'pause', now(), now(), now(), 'received', '{}', :slot)"
)


def _key(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'pk_control_commands'")
        ).scalar_one()


@pytest.fixture
def at_0014() -> Iterator[Engine]:
    name = create_database()
    engine = create_engine(owner_url(name), poolclass=pool.NullPool)
    migrate(engine, "0014_trend_kind")
    yield engine
    engine.dispose()
    drop_database(name)


def test_upgrade_makes_old_rows_main_s_and_keys_by_id_and_slot(at_0014):
    old = uuid.uuid4()
    with at_0014.begin() as conn:
        conn.execute(INSERT, {"id": old})

    migrate(at_0014)

    assert _key(at_0014) == "PRIMARY KEY (id, slot)"
    with at_0014.begin() as conn:
        assert (
            conn.execute(text("SELECT slot FROM control_commands WHERE id = :id"), {"id": old}).scalar_one() == "main"
        )
        conn.execute(INSERT_SLOT, {"id": old, "slot": "portfolio"})
    with pytest.raises(IntegrityError), at_0014.begin() as conn:
        conn.execute(INSERT_SLOT, {"id": old, "slot": "portfolio"})
    with pytest.raises(IntegrityError), at_0014.begin() as conn:
        conn.execute(INSERT_SLOT, {"id": uuid.uuid4(), "slot": "third"})


def test_downgrade_deletes_portfolio_rows_and_restores_the_key_on_id(at_0014):
    migrate(at_0014)
    both = uuid.uuid4()
    with at_0014.begin() as conn:
        conn.execute(INSERT_SLOT, {"id": both, "slot": "main"})
        conn.execute(INSERT_SLOT, {"id": both, "slot": "portfolio"})

    migrate(at_0014, "0014_trend_kind", down=True)

    assert _key(at_0014) == "PRIMARY KEY (id)"
    with at_0014.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM control_commands")).scalar_one() == 1
        columns = conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'control_commands'")
        ).scalars()
        assert "slot" not in set(columns)
