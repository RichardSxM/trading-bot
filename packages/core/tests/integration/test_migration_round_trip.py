"""covers: spec 0002 AC-1 (upgrade, downgrade to the baseline, upgrade again; roles survive)."""

from collections.abc import Sequence

from sqlalchemy import Connection, create_engine, pool, text

from trading_core.db_roles import APP_ROLES

from .conftest import create_database, drop_database, migrate, owner_url

SCHEMA = text(
    """
    SELECT 'column' AS kind, table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable AS item
    FROM information_schema.columns WHERE table_schema = 'public'
    UNION ALL
    SELECT 'constraint', conrelid::regclass::text || ' ' || conname || ' ' || pg_get_constraintdef(oid)
    FROM pg_constraint WHERE connamespace = 'public'::regnamespace
    UNION ALL
    SELECT 'index', indexname || ' ' || indexdef FROM pg_indexes WHERE schemaname = 'public'
    UNION ALL
    SELECT 'grant', grantee || ' ' || privilege_type || ' ' || table_name
    FROM information_schema.role_table_grants WHERE grantee LIKE '%\\_app'
    UNION ALL
    SELECT 'function', proname || ' ' || coalesce(array_to_string(proacl, ','), '')
    FROM pg_proc WHERE pronamespace = 'public'::regnamespace
    UNION ALL
    SELECT 'default acl', array_to_string(defaclacl, ',') FROM pg_default_acl
    ORDER BY 1, 2
    """
)


def _schema(conn: Connection) -> list[tuple[str, str]]:
    return [(kind, item) for kind, item in conn.execute(SCHEMA)]


def test_upgrade_downgrade_upgrade_gives_the_same_schema_and_keeps_the_roles():
    name = create_database()
    engine = create_engine(owner_url(name), poolclass=pool.NullPool)
    try:
        migrate(engine)
        with engine.connect() as conn:
            first = _schema(conn)

        migrate(engine, "0001_baseline", down=True)
        with engine.connect() as conn:
            after_down = _schema(conn)
            tables: Sequence[str] = (
                conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars().all()
            )
            roles: Sequence[str] = (
                conn.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE '%\\_app'")).scalars().all()
            )

        migrate(engine)
        with engine.connect() as conn:
            second = _schema(conn)
    finally:
        engine.dispose()
        drop_database(name)

    assert len([item for kind, item in first if kind == "constraint" and " pk_" in item]) == 25
    assert tables == ["alembic_version"]
    assert [kind for kind, _ in after_down if kind in {"grant", "function", "default acl"}] == []
    assert sorted(roles) == sorted(APP_ROLES)
    assert second == first
