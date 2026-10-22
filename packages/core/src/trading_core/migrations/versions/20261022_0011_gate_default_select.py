"""Gate default read grant (spec 0015 AC-22, review 2026-10-22): ``gate_app`` reads later tables too.

0009 granted ``gate_app`` SELECT on every table that existed then, but set no default privilege, so
a table a later migration adds would be unreadable by the gate and fail only when ``gate`` runs.
Expand only.

Revision ID: 0011_gate_default_select
Revises: 0010_one_holdout_per_study
Create Date: 2026-10-22 16:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_gate_default_select"
down_revision: str | None = "0010_one_holdout_per_study"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gate_app")


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM gate_app")
