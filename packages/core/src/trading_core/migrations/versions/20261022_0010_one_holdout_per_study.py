"""One completed holdout per study (spec 0015 AC-5, review 2026-10-22).

``uq_study_runs_frozen_hash`` already allows one completed holdout per frozen version, but two grid
points of one study have two frozen hashes, so a study could hold two. This index makes the one time
holdout a database rule. Expand only; it fails on a database that already breaks the rule, which is
the point.

Revision ID: 0010_one_holdout_per_study
Revises: 0009_gate
Create Date: 2026-10-22 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_one_holdout_per_study"
down_revision: str | None = "0009_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_study_runs_study_id",
        "study_runs",
        ["study_id"],
        unique=True,
        postgresql_where=sa.text("role = 'holdout' AND status = 'completed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_study_runs_study_id", table_name="study_runs")
