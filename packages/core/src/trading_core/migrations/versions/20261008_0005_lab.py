"""Lab (spec 0008): ``run_fill_stats`` and the ``lab_app`` grants. Additive only.

``lab_app`` itself is created by ``trading_core.db_roles`` before this runs (``NOLOGIN`` on a
host without its password file) and is left in place by the downgrade. It may only read and
insert the tables a replay bundle writes, so a lab bug cannot change or delete a paper or live
row, and cannot touch the control or snapshot tables.

Revision ID: 0005_lab
Revises: 0004_backup_dump_grant
Create Date: 2026-10-08 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0005_lab"
down_revision: str | None = "0004_backup_dump_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LAB_ROLE: Final = "lab_app"
# Every table write_run_bundle writes, plus the two upserted before it (AC-11).
BUNDLE_TABLES: Final = (
    "strategies",
    "strategy_versions",
    "instruments",
    "strategy_runs",
    "run_results",
    "run_fill_stats",
    "decisions",
    "orders",
    "order_events",
    "fills",
    "positions",
    "funding_payments",
    "risk_events",
)


def upgrade() -> None:
    op.create_table(
        "run_fill_stats",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("liquidity", sa.Text(), nullable=False),
        sa.Column("orders_placed", sa.Integer(), nullable=False),
        sa.Column("orders_filled", sa.Integer(), nullable=False),
        sa.Column("fill_rate", sa.Numeric(), nullable=False),
        sa.Column("fill_count", sa.Integer(), nullable=False),
        sa.Column("markout_1s_bps", sa.Numeric(), nullable=True),
        sa.Column("markout_5s_bps", sa.Numeric(), nullable=True),
        sa.Column("markout_30s_bps", sa.Numeric(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_run_fill_stats_env_valid")),
        sa.CheckConstraint("liquidity IN ('maker', 'taker')", name=op.f("ck_run_fill_stats_liquidity_valid")),
        sa.CheckConstraint("orders_placed >= 1", name=op.f("ck_run_fill_stats_orders_placed_positive")),
        sa.CheckConstraint(
            "orders_filled BETWEEN 0 AND orders_placed", name=op.f("ck_run_fill_stats_orders_filled_range")
        ),
        sa.CheckConstraint("fill_rate BETWEEN 0 AND 1", name=op.f("ck_run_fill_stats_fill_rate_range")),
        sa.CheckConstraint("fill_count >= 0", name=op.f("ck_run_fill_stats_fill_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_run_fill_stats_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("run_id", "liquidity", name=op.f("pk_run_fill_stats")),
    )
    # The default privileges of 0002 already give the other app roles SELECT on a new table;
    # granted here too so the intent is explicit.
    op.execute("GRANT SELECT ON run_fill_stats TO trader_app, dashboard_app, backup_app")
    op.execute(f"GRANT SELECT, INSERT ON {', '.join(BUNDLE_TABLES)} TO {LAB_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, INSERT ON {', '.join(BUNDLE_TABLES)} FROM {LAB_ROLE}")
    op.drop_table("run_fill_stats")
