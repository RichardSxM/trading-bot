"""Alembic environment: URL from settings and the owner password file, metadata from trading_core.db, JSON logs.

A caller may hand in an open connection as ``config.attributes["connection"]`` (the
integration tests do, to migrate their own database); otherwise the URL comes from settings
plus the mounted ``postgres_password`` (spec 0003).
"""

import logging
import sys

from alembic import context
from sqlalchemy import Connection, create_engine, pool

import trading_core.recording.models  # noqa: F401  # registers every table on Base.metadata
from trading_core.db import Base
from trading_core.logs import configure_logging
from trading_core.secrets import POSTGRES_PASSWORD, SecretUnavailable, read_secret
from trading_core.settings import get_settings

target_metadata = Base.metadata

log = logging.getLogger(__name__)


def _owner_url() -> str:
    """Configure JSON logs, then build the owner URL; exit 1 when the password file is unusable."""
    settings = get_settings()
    configure_logging(process=settings.process_name, env=settings.trading_env, level=settings.log_level)
    try:
        password = read_secret(POSTGRES_PASSWORD, settings.secrets_dir)
    except SecretUnavailable as exc:
        log.critical(
            "secret unavailable, refusing to start",
            extra={"fields": {"code": "secret_unavailable", "secret": exc.name, "reason": exc.reason}},
        )
        sys.exit(1)
    return settings.database_url(password)


def run_migrations_offline() -> None:
    context.configure(url=_owner_url(), target_metadata=target_metadata, literal_binds=True)
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
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_on(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
