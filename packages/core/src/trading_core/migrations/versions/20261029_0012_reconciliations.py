"""Restart and outage safety (spec 0023): ``reconciliations``, ``reconciliation_mismatches``, and two commands.

Expand only, so the previous release still works on this schema. One ``reconciliations`` row per
reported check, one ``reconciliation_mismatches`` row per finding, both env scoped and kept
forever. ``control_commands.cmd`` gains ``recheck`` and ``drill_step``. The default privileges of
0002 and 0011 already give the trader INSERT and every reader SELECT on the new tables. The
downgrade restores the five value check; it fails while rows with the new commands exist, which
is correct.

Revision ID: 0012_reconciliations
Revises: 0011_gate_default_select
Create Date: 2026-10-29 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_reconciliations"
down_revision: str | None = "0011_gate_default_select"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMMANDS: Final = "control_commands"
CMD_CHECK: Final = "ck_control_commands_cmd_valid"
OLD_CMDS: Final = "'stop', 'resume', 'flatten', 'pause', 'unpause'"
NEW_CMDS: Final = f"{OLD_CMDS}, 'recheck', 'drill_step'"


def _env_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint("env IN ('demo', 'live')", name=op.f(f"ck_{table}_env_valid"))


def _env_fk(table: str, column: str, parent: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column, "env"], [f"{parent}.id", f"{parent}.env"], name=op.f(f"fk_{table}_{column}_env_{parent}")
    )


def upgrade() -> None:
    op.create_table(
        "reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_ns", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_ns", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("exchange_orders", sa.Integer(), nullable=True),
        sa.Column("exchange_positions", sa.Integer(), nullable=True),
        sa.Column("mismatch_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _env_check("reconciliations"),
        sa.CheckConstraint(
            "trigger IN ('start', 'reconnect', 'periodic')", name=op.f("ck_reconciliations_trigger_valid")
        ),
        sa.CheckConstraint("outcome IN ('clean', 'mismatch', 'failed')", name=op.f("ck_reconciliations_outcome_valid")),
        _env_fk("reconciliations", "run_id", "strategy_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliations")),
        sa.UniqueConstraint("id", "env", name=op.f("uq_reconciliations_id_env")),
    )
    op.create_index(op.f("ix_reconciliations_env_started_at"), "reconciliations", ["env", "started_at"])

    op.create_table(
        "reconciliation_mismatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=True),
        sa.Column("venue_order_id", sa.Text(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("exchange_side", sa.Text(), nullable=True),
        sa.Column("exchange_qty", sa.Numeric(), nullable=True),
        sa.Column("local_side", sa.Text(), nullable=True),
        sa.Column("local_qty", sa.Numeric(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("action_result", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _env_check("reconciliation_mismatches"),
        sa.CheckConstraint(
            "kind IN ('unknown_order', 'local_only_order', 'unknown_position', 'position_mismatch', "
            "'missing_stop', 'stop_size_mismatch', 'duplicate_entry')",
            name=op.f("ck_reconciliation_mismatches_kind_valid"),
        ),
        sa.CheckConstraint(
            "action IN ('cancelled', 'flattened', 'closed_at_market', 'stop_replaced', 'adopted', "
            "'marked_canceled', 'none')",
            name=op.f("ck_reconciliation_mismatches_action_valid"),
        ),
        sa.CheckConstraint(
            "action_result IN ('ok', 'failed', 'pending')",
            name=op.f("ck_reconciliation_mismatches_action_result_valid"),
        ),
        _env_fk("reconciliation_mismatches", "reconciliation_id", "reconciliations"),
        _env_fk("reconciliation_mismatches", "order_id", "orders"),
        _env_fk("reconciliation_mismatches", "position_id", "positions"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_reconciliation_mismatches_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_mismatches")),
    )
    op.create_index(
        op.f("ix_reconciliation_mismatches_env_reconciliation_id"),
        "reconciliation_mismatches",
        ["env", "reconciliation_id"],
    )

    op.drop_constraint(op.f(CMD_CHECK), COMMANDS, type_="check")
    op.create_check_constraint(op.f(CMD_CHECK), COMMANDS, f"cmd IN ({NEW_CMDS})")


def downgrade() -> None:
    op.drop_constraint(op.f(CMD_CHECK), COMMANDS, type_="check")
    op.create_check_constraint(op.f(CMD_CHECK), COMMANDS, f"cmd IN ({OLD_CMDS})")
    op.drop_index(op.f("ix_reconciliation_mismatches_env_reconciliation_id"), table_name="reconciliation_mismatches")
    op.drop_table("reconciliation_mismatches")
    op.drop_index(op.f("ix_reconciliations_env_started_at"), table_name="reconciliations")
    op.drop_table("reconciliations")
