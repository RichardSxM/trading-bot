"""Command slot (spec 0030 AC-14): ``control_commands`` is keyed by the command id and the trader slot.

Each running trader records the same command once, so the row gains ``slot`` (text, not null, server
default ``main``, ``main`` or ``portfolio``) and the primary key becomes ``(id, slot)``. Every old row
becomes ``main``'s. A release from before this change does not work on this schema: it records a command
with ``ON CONFLICT (id)``, and with the key on ``(id, slot)`` Postgres refuses that, so a trader rolled back
past this revision runs its commands but records none (and loses the duplicate check) until this downgrade
runs. No unique index on ``id`` alone can be kept, since two slots record the same id. The downgrade deletes
the ``portfolio`` rows, restores the key on ``id``, and drops the column.

Revision ID: 0015_command_slot
Revises: 0014_trend_kind
Create Date: 2026-11-07 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0015_command_slot"
down_revision: str | None = "0014_trend_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "control_commands"
PKEY: Final = "pk_control_commands"
SLOT_CHECK: Final = "ck_control_commands_slot_valid"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("slot", sa.Text(), nullable=False, server_default="main"))
    op.create_check_constraint(op.f(SLOT_CHECK), TABLE, "slot IN ('main', 'portfolio')")
    op.drop_constraint(op.f(PKEY), TABLE, type_="primary")
    op.create_primary_key(op.f(PKEY), TABLE, ["id", "slot"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM control_commands WHERE slot = 'portfolio'"))
    op.drop_constraint(op.f(PKEY), TABLE, type_="primary")
    op.create_primary_key(op.f(PKEY), TABLE, ["id"])
    op.drop_constraint(op.f(SLOT_CHECK), TABLE, type_="check")
    op.drop_column(TABLE, "slot")
