"""Pause commands (spec 0005 AC-6): allow ``pause`` and ``unpause`` in ``control_commands.cmd``.

Expand only, so the previous release still works on this schema. The downgrade restores the
three value check; it fails while pause rows exist, which is correct.

Revision ID: 0003_pause_commands
Revises: 0002_data_model
Create Date: 2026-10-03 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0003_pause_commands"
down_revision: str | None = "0002_data_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "control_commands"
CHECK: Final = "ck_control_commands_cmd_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(CHECK), TABLE, type_="check")
    op.create_check_constraint(op.f(CHECK), TABLE, "cmd IN ('stop', 'resume', 'flatten', 'pause', 'unpause')")


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK), TABLE, type_="check")
    op.create_check_constraint(op.f(CHECK), TABLE, "cmd IN ('stop', 'resume', 'flatten')")
