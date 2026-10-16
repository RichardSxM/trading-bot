"""covers: spec 0002 AC-11 (what each app role may do), AC-13 (role setup is idempotent), spec 0008 AC-17 (lab_app),
spec 0013 Security model (alerter_app writes only alerts; nobody else writes them)."""

from collections.abc import Callable, Iterator, Sequence

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.exc import ProgrammingError

from trading_core.db_roles import (
    ALERTER_ROLE,
    BACKUP_ROLE,
    DASHBOARD_ROLE,
    LAB_ROLE,
    TRADER_ROLE,
    role_passwords,
    setup_roles,
)

from .conftest import migrate_secrets, owner_url, role_url

TABLES = (
    "instruments",
    "strategies",
    "strategy_versions",
    "promotions",
    "promotion_evidence",
    "fx_rates",
    "strategy_runs",
    "run_results",
    "decisions",
    "orders",
    "order_events",
    "fills",
    "positions",
    "position_snapshots",
    "account_snapshots",
    "funding_payments",
    "risk_events",
    "control_commands",
    "run_fill_stats",
    "alerts",
)
TRADER_UPDATES = {"orders", "positions", "strategy_runs", "control_commands", "fills", "funding_payments"}
# What a replay bundle writes (spec 0008 AC-17): lab_app may read and insert these, nothing else.
LAB_TABLES = {
    "strategies",
    "strategy_versions",
    "instruments",
    "strategy_runs",
    "run_results",
    "run_fill_stats",
    "decisions",
    "orders",
    "order_events",
    "fills",
    "positions",
    "funding_payments",
    "risk_events",
}


# The alerter (spec 0013): reads and writes alerts (no DELETE), reads fills and positions.
ALERTER_GRANTS = {
    ("alerts", "SELECT"),
    ("alerts", "INSERT"),
    ("alerts", "UPDATE"),
    ("fills", "SELECT"),
    ("positions", "SELECT"),
}


def _expected(role: str, table: str, privilege: str) -> bool:
    if role == ALERTER_ROLE:
        return (table, privilege) in ALERTER_GRANTS
    if table == "alerts":
        return privilege == "SELECT" and role != LAB_ROLE
    if role == LAB_ROLE:
        return privilege in {"SELECT", "INSERT"} and table in LAB_TABLES
    if privilege == "SELECT":
        return True
    if role != TRADER_ROLE:
        return False
    return privilege == "INSERT" or (privilege == "UPDATE" and table in TRADER_UPDATES)


@pytest.fixture
def role_engine(database: str) -> Iterator[Callable[[str], Engine]]:
    engines: list[Engine] = []

    def connect(role: str) -> Engine:
        engine = create_engine(role_url(database, role), poolclass=pool.NullPool)
        engines.append(engine)
        return engine

    yield connect
    for engine in engines:
        engine.dispose()


def test_table_privileges_match_the_security_model(engine: Engine):
    actual: dict[tuple[str, str, str], bool] = {}
    expected: dict[tuple[str, str, str], bool] = {}
    with engine.connect() as conn:
        for role in (TRADER_ROLE, DASHBOARD_ROLE, BACKUP_ROLE, LAB_ROLE, ALERTER_ROLE):
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    key = (role, table, privilege)
                    actual[key] = conn.execute(
                        text("SELECT has_table_privilege(:role, :table, :privilege)"),
                        {"role": role, "table": table, "privilege": privilege},
                    ).scalar_one()
                    expected[key] = _expected(*key)

    assert actual == expected


def test_only_backup_app_may_run_the_downsample(engine: Engine):
    with engine.connect() as conn:
        allowed: dict[str, bool] = {
            role: conn.execute(
                text("SELECT has_function_privilege(:role, 'downsample_snapshots()', 'EXECUTE')"), {"role": role}
            ).scalar_one()
            for role in (TRADER_ROLE, DASHBOARD_ROLE, BACKUP_ROLE, "public")
        }

    assert allowed == {TRADER_ROLE: False, DASHBOARD_ROLE: False, BACKUP_ROLE: True, "public": False}


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        (
            DASHBOARD_ROLE,
            "INSERT INTO fx_rates (rate_date, base_currency, quote_currency, rate, source)"
            " VALUES ('2026-01-01', 'EUR', 'USD', 1.1, 'ECB')",
        ),
        (TRADER_ROLE, "DELETE FROM fills"),
        (TRADER_ROLE, "UPDATE decisions SET reason = reason"),
        (TRADER_ROLE, "CREATE TABLE intruder (id int)"),
        (TRADER_ROLE, "SELECT downsample_snapshots()"),
        (BACKUP_ROLE, "DELETE FROM position_snapshots"),
        (LAB_ROLE, "UPDATE orders SET status = status"),
        (LAB_ROLE, "DELETE FROM fills"),
        (
            LAB_ROLE,
            "INSERT INTO control_commands (id, env, cmd, issued_at, expires_at, received_at)"
            " VALUES (gen_random_uuid(), 'demo', 'pause', now(), now(), now())",
        ),
        (LAB_ROLE, "SELECT count(*) FROM control_commands"),
        (ALERTER_ROLE, "DELETE FROM alerts"),
        (ALERTER_ROLE, "SELECT count(*) FROM orders"),
        (
            TRADER_ROLE,
            "INSERT INTO alerts (env, rule, fingerprint, severity, source, title, message, details,"
            " opened_at, last_seen_at, delivery)"
            " VALUES ('demo', 'x', 'x', 'info', 'self', 't', 'm', '{}', now(), now(), 'pending')",
        ),
    ],
)
def test_a_role_is_refused_what_it_may_not_do(role_engine, role, statement):
    with role_engine(role).connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
        conn.execute(text(statement))


@pytest.mark.parametrize("role", [TRADER_ROLE, DASHBOARD_ROLE, BACKUP_ROLE])
def test_every_role_can_read(role_engine, role):
    with role_engine(role).connect() as conn:
        for table in TABLES:
            conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_backup_app_runs_the_downsample(role_engine):
    with role_engine(BACKUP_ROLE).begin() as conn:
        assert conn.execute(text("SELECT downsample_snapshots()")).scalar_one() >= 0


def test_backup_app_can_read_every_table_and_sequence_pg_dump_reads(engine: Engine):
    """covers: spec 0005 AC-19 (pg_dump as backup_app locks every table, alembic_version too, and
    reads every sequence's state, such as alerts_id_seq)."""
    with engine.connect() as conn:
        unreadable: Sequence[str] = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S')"
                    " AND NOT has_table_privilege(:role, c.oid, 'SELECT')"
                ),
                {"role": BACKUP_ROLE},
            )
            .scalars()
            .all()
        )

    assert unreadable == []


def test_role_setup_runs_twice_without_error(database: str):
    passwords = role_passwords(migrate_secrets())
    for _ in range(2):
        with psycopg.connect(owner_url(database).replace("+psycopg", "")) as conn:
            setup_roles(conn, database, passwords)
