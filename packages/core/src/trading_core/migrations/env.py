"""Alembic environment: URL from settings, metadata from trading_core.db, JSON logs.

A caller may hand in an open connection as ``config.attributes["connection"]`` (the
integration tests do, to migrate their own database); otherwise the URL comes from settings.
"""

from alembic import context
from sqlalchemy import Connection, create_engine, pool

import trading_core.recording.models  # noqa: F401  # registers every table on Base.metadata
from trading_core.db import Base
from trading_core.logs import configure_logging
from trading_core.settings import get_settings

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    settings = get_settings()
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_on(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if isinstance(connection, Connection):
        _run_on(connection)
        return
    settings = get_settings()
    configure_logging(process=settings.process_name, env=settings.trading_env, level=settings.log_level)
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_on(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
