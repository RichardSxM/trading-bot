"""Alembic environment: URL from settings, metadata from trading_core.db, JSON logs."""

from alembic import context
from sqlalchemy import create_engine, pool

from trading_core.db import Base
from trading_core.logs import configure_logging
from trading_core.settings import get_settings

settings = get_settings()
configure_logging(process=settings.process_name, env=settings.trading_env, level=settings.log_level)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
