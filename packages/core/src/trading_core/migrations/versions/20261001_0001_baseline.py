"""Empty baseline. Tables arrive with feature 3 (data model).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-01
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
