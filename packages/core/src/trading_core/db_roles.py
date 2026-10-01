"""Create or update the app roles, before ``alembic upgrade head`` (spec 0002 Security model).

Runs as the owner role ``trading`` in the migrate service: ``python -m trading_core.db_roles``.
Roles are cluster wide and live outside Alembic, so a downgrade leaves them in place. Running
it again only resets the passwords and grants, so it is safe to repeat. A missing or empty
password variable stops it with exit 1 and one JSON error line.
"""

import logging
import os
import sys
from collections.abc import Mapping
from typing import Final

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from pydantic import ValidationError

from trading_core.logs import configure_logging
from trading_core.settings import RoleSettings, Settings, get_settings

log = logging.getLogger(__name__)

TRADER_ROLE: Final = "trader_app"
DASHBOARD_ROLE: Final = "dashboard_app"
BACKUP_ROLE: Final = "backup_app"
APP_ROLES: Final = (TRADER_ROLE, DASHBOARD_ROLE, BACKUP_ROLE)

_ATTRIBUTES: Final = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}")


def role_passwords(roles: RoleSettings) -> dict[str, str]:
    """Map each app role to its password variable."""
    return {
        TRADER_ROLE: roles.postgres_trader_password,
        DASHBOARD_ROLE: roles.postgres_dashboard_password,
        BACKUP_ROLE: roles.postgres_backup_password,
    }


def setup_roles(conn: psycopg.Connection[TupleRow], database: str, passwords: Mapping[str, str]) -> None:
    """Create each role or reset its attributes and password, then grant CONNECT and schema USAGE.

    DDL takes no bind parameters, so names and passwords are quoted with ``psycopg.sql``.
    """
    for role, password in passwords.items():
        name = sql.Identifier(role)
        attributes = _ATTRIBUTES.format(sql.Literal(password))
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} WITH {}").format(name, attributes))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} WITH {}").format(name, attributes))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), name))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(name))


def connect(settings: Settings, database: str | None = None) -> psycopg.Connection[TupleRow]:
    """Open a psycopg connection as the configured (owner) user, optionally to another database."""
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=database or settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )


def main() -> None:
    """Entry point of the migrate service's first step."""
    configure_logging(process=os.environ.get("PROCESS_NAME", "migrate"), env=os.environ.get("TRADING_ENV", ""))
    try:
        roles = RoleSettings()  # fields come from the environment
    except ValidationError as exc:
        log.error(
            "role passwords missing, refusing to set up roles",
            extra={
                "fields": {
                    "code": "role_passwords_missing",
                    "errors": exc.errors(include_url=False, include_input=False),
                }
            },
        )
        sys.exit(1)

    settings = get_settings()
    with connect(settings) as conn:
        setup_roles(conn, settings.postgres_db, role_passwords(roles))
    log.info("app roles ready", extra={"fields": {"roles": list(APP_ROLES)}})


if __name__ == "__main__":
    main()
