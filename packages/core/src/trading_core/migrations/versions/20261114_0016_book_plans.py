"""Book sizing history (spec 0033 AC-5): ``book_plans`` and ``book_plan_coins``.

Expand only, so the previous release still works on this schema. One ``book_plans`` row per rebalance of a
book strategy (env scoped, unique per run and rebalance time) and one ``book_plan_coins`` row per planned
coin; both immutable and kept forever. The default privileges of 0002 already give ``trader_app`` INSERT
and ``trader_app``, ``dashboard_app``, ``backup_app`` SELECT on new tables; no UPDATE is granted.
``alerter_app`` gets SELECT on ``book_plans`` and ``strategy_runs`` for the ``book_plan_stale`` rule (AC-7:
the newest row's age and the run's start). The downgrade revokes that and drops both tables.

Revision ID: 0016_book_plans
Revises: 0015_command_slot
Create Date: 2026-11-14 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_book_plans"
down_revision: str | None = "0015_command_slot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "book_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("plan_day", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rebalanced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rebalanced_ns", sa.BigInteger(), nullable=False),
        sa.Column("equity", sa.Numeric(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("gross", sa.Numeric(), nullable=False),
        sa.Column("unit_vol", sa.Numeric(), nullable=True),
        sa.Column("book_gross", sa.Numeric(), nullable=True),
        sa.Column("book_vol_target", sa.Numeric(), nullable=True),
        sa.Column("rebalance_band", sa.Numeric(), nullable=True),
        sa.Column("gross_cap", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_book_plans_env_valid")),
        sa.CheckConstraint("reason IN ('start', 'resume', 'daily', 'weekly')", name=op.f("ck_book_plans_reason_valid")),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_book_plans_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_plans")),
        sa.UniqueConstraint("env", "run_id", "rebalanced_ns", name=op.f("uq_book_plans_env_run_id_rebalanced_ns")),
    )
    op.create_index(op.f("ix_book_plans_run_id_rebalanced_ns"), "book_plans", ["run_id", "rebalanced_ns"])

    op.create_table(
        "book_plan_coins",
        sa.Column("book_plan_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("signal", sa.Integer(), nullable=False),
        sa.Column("vol", sa.Numeric(), nullable=True),
        sa.Column("weight", sa.Numeric(), nullable=False),
        sa.Column("target_notional", sa.Numeric(), nullable=False),
        sa.Column("close_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "signal IN (-1, 0, 1) AND (eligible OR signal = 0)", name=op.f("ck_book_plan_coins_signal_valid")
        ),
        sa.ForeignKeyConstraint(
            ["book_plan_id"], ["book_plans.id"], name=op.f("fk_book_plan_coins_book_plan_id_book_plans")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_book_plan_coins_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("book_plan_id", "instrument_id", name=op.f("pk_book_plan_coins")),
    )
    op.execute("GRANT SELECT ON book_plans, strategy_runs TO alerter_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON book_plans, strategy_runs FROM alerter_app")
    op.drop_table("book_plan_coins")
    op.drop_index(op.f("ix_book_plans_run_id_rebalanced_ns"), table_name="book_plans")
    op.drop_table("book_plans")
