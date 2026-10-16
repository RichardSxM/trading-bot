"""Alerter instruments grant (spec 0013 AC-8): let ``alerter_app`` read ``instruments``.

A live fill push shows quantity and price with the instrument's lot and tick precision and names
its base currency, all from ``instruments``. Expand only.

Revision ID: 0008_alerter_instruments
Revises: 0007_backup_sequence_grant
Create Date: 2026-10-16 16:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_alerter_instruments"
down_revision: str | None = "0007_backup_sequence_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON instruments TO alerter_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON instruments FROM alerter_app")
