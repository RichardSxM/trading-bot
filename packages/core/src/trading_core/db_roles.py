"""Create or update the app roles, before ``alembic upgrade head`` (spec 0002 Security model).

Runs as the owner role ``trading`` in the migrate service: ``python -m trading_core.db_roles``.
Roles are cluster wide and live outside Alembic, so a downgrade leaves them in place. Running
it again only resets the passwords and grants, so it is safe to repeat. The owner password and
each required role password are secret files (spec 0003); an unusable one stops it with exit 1
and one JSON error line. The lab role's password (spec 0008) is optional: the laptop has it,
the VPS does not, and without it the role exists but cannot log in (``NOLOGIN``).
"""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from pydantic import SecretStr, ValidationError

from trading_core.logs import configure_logging
from trading_core.secrets import POSTGRES_LAB_PASSWORD, POSTGRES_PASSWORD, SecretUnavailable, load_required, read_secret
from trading_core.settings import Settings

log = logging.getLogger(__name__)

TRADER_ROLE: Final = "trader_app"
DASHBOARD_ROLE: Final = "dashboard_app"
BACKUP_ROLE: Final = "backup_app"
LAB_ROLE: Final = "lab_app"
# The alerter (spec 0013): writes only the ``alerts`` table, reads fills and positions.
ALERTER_ROLE: Final = "alerter_app"
APP_ROLES: Final = (TRADER_ROLE, DASHBOARD_ROLE, BACKUP_ROLE, LAB_ROLE, ALERTER_ROLE)

# Each app role's password file as the migrate service mounts it.
ROLE_SECRETS: Final = {
    TRADER_ROLE: "postgres_trader_password",
    DASHBOARD_ROLE: "postgres_dashboard_password",
    BACKUP_ROLE: "postgres_backup_password",
    ALERTER_ROLE: "postgres_alerter_password",
}
# Roles whose password file may be absent; such a role is created or reset NOLOGIN.
OPTIONAL_ROLE_SECRETS: Final = {LAB_ROLE: POSTGRES_LAB_PASSWORD}
# Everything role setup and Alembic need: the owner password, then the role passwords.
MIGRATE_SECRETS: Final = (POSTGRES_PASSWORD, *ROLE_SECRETS.values())

_ATTRIBUTES: Final = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}")
_NO_LOGIN: Final = sql.SQL("NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD NULL")


def load_optional(secrets_dir: Path) -> dict[str, SecretStr]:
    """Read each optional role password whose file exists; a missing file is skipped.

    A file that exists but is unusable (empty, too short, unreadable) still raises
    ``SecretUnavailable``: only absence means "this host has no such role login".
    """
    loaded: dict[str, SecretStr] = {}
    for name in OPTIONAL_ROLE_SECRETS.values():
        try:
            loaded[name] = read_secret(name, secrets_dir)
        except SecretUnavailable as exc:
            if exc.reason != "missing":
                raise
    return loaded


def role_passwords(secrets: Mapping[str, SecretStr]) -> dict[str, str | None]:
    """Map each app role to its password from the loaded secrets; ``None`` for an absent optional one."""
    passwords: dict[str, str | None] = {role: secrets[name].get_secret_value() for role, name in ROLE_SECRETS.items()}
    for role, name in OPTIONAL_ROLE_SECRETS.items():
        secret = secrets.get(name)
        passwords[role] = secret.get_secret_value() if secret is not None else None
    return passwords


def setup_roles(conn: psycopg.Connection[TupleRow], database: str, passwords: Mapping[str, str | None]) -> None:
    """Create each role or reset its attributes and password, then grant CONNECT and schema USAGE.

    A role whose password is ``None`` is made ``NOLOGIN`` with no password. DDL takes no bind
    parameters, so names and passwords are quoted with ``psycopg.sql``.
    """
    for role, password in passwords.items():
        name = sql.Identifier(role)
        attributes = _NO_LOGIN if password is None else _ATTRIBUTES.format(sql.Literal(password))
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} WITH {}").format(name, attributes))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} WITH {}").format(name, attributes))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), name))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(name))


def connect(settings: Settings, password: SecretStr, database: str | None = None) -> psycopg.Connection[TupleRow]:
    """Open a psycopg connection as the configured (owner) user, optionally to another database."""
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=database or settings.postgres_db,
        user=settings.postgres_user,
        password=password.get_secret_value(),
    )


def main() -> None:
    """Entry point of the migrate service's first step."""
    configure_logging(process=os.environ.get("PROCESS_NAME", "migrate"), env=os.environ.get("TRADING_ENV", ""))
    try:
        settings = Settings()  # fields come from the environment
    except ValidationError as exc:
        log.critical(
            "invalid configuration, refusing to start",
            extra={"fields": {"errors": exc.errors(include_url=False, include_input=False)}},
        )
        sys.exit(1)
    try:
        secrets = load_required(MIGRATE_SECRETS, settings.secrets_dir)
        secrets |= load_optional(settings.secrets_dir)
    except SecretUnavailable as exc:
        log.critical(
            "secret unavailable, refusing to start",
            extra={"fields": {"code": "secret_unavailable", "secret": exc.name, "reason": exc.reason}},
        )
        sys.exit(1)

    with connect(settings, secrets[POSTGRES_PASSWORD]) as conn:
        passwords = role_passwords(secrets)
        setup_roles(conn, settings.postgres_db, passwords)
    no_login = [role for role, password in passwords.items() if password is None]
    log.info("app roles ready", extra={"fields": {"roles": list(APP_ROLES), "no_login": no_login}})


if __name__ == "__main__":
    main()
