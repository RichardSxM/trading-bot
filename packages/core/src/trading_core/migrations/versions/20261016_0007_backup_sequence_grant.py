"""Backup sequence grant (spec 0005 AC-19, spec 0013): let ``backup_app`` read ``alerts_id_seq``.

``alerts.id`` is the first identity column, so its sequence is the first in the schema. The
nightly ``pg_dump`` as ``backup_app`` reads every sequence's state and failed on it. The default
privileges now cover later sequences too. Expand only.

Revision ID: 0007_backup_sequence_grant
Revises: 0006_alerts
Create Date: 2026-10-16 14:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_backup_sequence_grant"
down_revision: str | None = "0006_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_app")


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON SEQUENCES FROM backup_app")
    op.execute("REVOKE SELECT ON ALL SEQUENCES IN SCHEMA public FROM backup_app")
