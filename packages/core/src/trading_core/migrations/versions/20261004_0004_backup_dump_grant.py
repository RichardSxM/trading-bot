"""Backup dump grant (spec 0005 AC-19): let ``backup_app`` read ``alembic_version``.

The nightly ``pg_dump`` runs as ``backup_app``, which reads every app table but not the table
Alembic creates before the grants, so the dump failed on it. Expand only.

Revision ID: 0004_backup_dump_grant
Revises: 0003_pause_commands
Create Date: 2026-10-04 12:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_backup_dump_grant"
down_revision: str | None = "0003_pause_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO backup_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM backup_app")
