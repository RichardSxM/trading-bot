"""Alerts (spec 0013): the ``alerts`` table and the ``alerter_app`` grants. Additive only.

``alerter_app`` itself is created by ``trading_core.db_roles`` before this runs and is left in
place by the downgrade. It may read and write ``alerts`` (no DELETE) and read ``fills`` and
``positions`` for the fill pushes and the daily note. The default privileges of 0002 would let
``trader_app`` insert alerts; that grant is taken back, since only the alerter writes them.

Revision ID: 0006_alerts
Revises: 0005_lab
Create Date: 2026-10-16 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_alerts"
down_revision: str | None = "0005_lab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALERTER_ROLE: Final = "alerter_app"


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sends", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery", sa.Text(), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_alerts_env_valid")),
        sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name=op.f("ck_alerts_severity_valid")),
        sa.CheckConstraint(
            "delivery IN ('pending', 'sent', 'failed', 'dry_run', 'muted', 'held', 'capped')",
            name=op.f("ck_alerts_delivery_valid"),
        ),
        sa.CheckConstraint("occurrences >= 1", name=op.f("ck_alerts_occurrences_positive")),
        sa.CheckConstraint("sends >= 0", name=op.f("ck_alerts_sends_nonnegative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    op.create_index(
        "uq_alerts_open",
        "alerts",
        ["env", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_alerts_env_opened_at", "alerts", ["env", "opened_at"])
    # The default privileges of 0002 already give the other app roles SELECT on a new table;
    # granted here too so the intent is explicit.
    op.execute("GRANT SELECT ON alerts TO dashboard_app, backup_app")
    op.execute("REVOKE INSERT ON alerts FROM trader_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON alerts TO {ALERTER_ROLE}")
    op.execute(f"GRANT SELECT ON fills, positions TO {ALERTER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON fills, positions FROM {ALERTER_ROLE}")
    op.drop_index("ix_alerts_env_opened_at", table_name="alerts")
    op.drop_index("uq_alerts_open", table_name="alerts")
    op.drop_table("alerts")
