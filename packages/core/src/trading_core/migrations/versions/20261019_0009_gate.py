"""Strategy scoring and promotion gate (spec 0015): studies, daily equity, measures, folds, and ``gate_app``.

Additive only. ``strategy_versions`` gains ``code_hash`` and ``frozen_hash`` (NULL on older rows).
``promotions`` gains the ``insufficient`` verdict, the frozen hash, the study, the frozen thresholds
hash, reasons, metrics, and the gate's commit, plus two CHECKs: no manual promotion to live, and a
gate promotion always names a frozen hash.

``gate_app`` itself is created by ``trading_core.db_roles`` before this runs (``NOLOGIN`` without its
password file) and is left in place by the downgrade. It reads everything, inserts only what a bundle
import and an evaluation write, and never updates or deletes (AC-22). The new tables are append only
for every role, so the trader's default INSERT on new tables is revoked.

Revision ID: 0009_gate
Revises: 0008_alerter_instruments
Create Date: 2026-10-19 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_gate"
down_revision: str | None = "0008_alerter_instruments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GATE_ROLE: Final = "gate_app"
LAB_ROLE: Final = "lab_app"
# Every table a lab bundle writes (migration 0005's list), which gate import replays.
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
# The bundle's new tables, which the lab writes as well as the gate.
STUDY_TABLES: Final = ("studies", "study_runs", "run_daily_equity", "run_measures")
NEW_TABLES: Final = (*STUDY_TABLES, "promotion_folds")
EQUITY_BASES: Final = "'marked', 'realized'"


def _env_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["run_id", "env"],
        ["strategy_runs.id", "strategy_runs.env"],
        name=op.f(f"fk_{table}_run_id_env_strategy_runs"),
    )


def _env_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint("env IN ('demo', 'live')", name=op.f(f"ck_{table}_env_valid"))


def upgrade() -> None:
    op.add_column("strategy_versions", sa.Column("code_hash", sa.Text(), nullable=True))
    op.add_column("strategy_versions", sa.Column("frozen_hash", sa.Text(), nullable=True))
    op.create_index(op.f("ix_strategy_versions_frozen_hash"), "strategy_versions", ["frozen_hash"], unique=False)

    op.create_table(
        "studies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lab", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("thresholds_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("lab IN ('candle', 'replay')", name=op.f("ck_studies_lab_valid")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_studies_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studies")),
        sa.UniqueConstraint("name", name=op.f("uq_studies_name")),
    )
    op.create_index(op.f("ix_studies_strategy_id"), "studies", ["strategy_id"], unique=False)

    op.create_table(
        "study_runs",
        sa.Column("study_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("grid_point", sa.Integer(), nullable=True),
        sa.Column("frozen_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('development', 'holdout', 'benchmark')", name=op.f("ck_study_runs_role_valid")),
        sa.CheckConstraint("status IN ('completed', 'failed', 'aborted')", name=op.f("ck_study_runs_status_valid")),
        sa.CheckConstraint(
            "(role = 'benchmark') = (grid_point IS NULL)", name=op.f("ck_study_runs_grid_point_iff_not_benchmark")
        ),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], name=op.f("fk_study_runs_study_id_studies")),
        sa.ForeignKeyConstraint(["run_id"], ["strategy_runs.id"], name=op.f("fk_study_runs_run_id_strategy_runs")),
        sa.PrimaryKeyConstraint("study_id", "run_id", name=op.f("pk_study_runs")),
        sa.UniqueConstraint("run_id", name=op.f("uq_study_runs_run_id")),
    )
    op.create_index(
        "uq_study_runs_frozen_hash",
        "study_runs",
        ["frozen_hash"],
        unique=True,
        postgresql_where=sa.text("role = 'holdout' AND status = 'completed'"),
    )

    op.create_table(
        "run_daily_equity",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("equity_close", sa.Numeric(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("trades_closed", sa.Integer(), nullable=False),
        sa.Column("fees", sa.Numeric(), nullable=False),
        sa.Column("funding", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        _env_check("run_daily_equity"),
        sa.CheckConstraint(f"basis IN ({EQUITY_BASES})", name=op.f("ck_run_daily_equity_basis_valid")),
        sa.CheckConstraint("trades_closed >= 0", name=op.f("ck_run_daily_equity_trades_closed_nonnegative")),
        _env_fk("run_daily_equity"),
        sa.PrimaryKeyConstraint("run_id", "day", name=op.f("pk_run_daily_equity")),
    )

    op.create_table(
        "run_measures",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("drawdown_basis", sa.Text(), nullable=False),
        sa.Column("marked_max_drawdown", sa.Numeric(), nullable=True),
        sa.Column("longest_drawdown_hours", sa.Integer(), nullable=True),
        sa.Column("exposure", sa.Numeric(), nullable=True),
        sa.Column("long_trades", sa.Integer(), nullable=False),
        sa.Column("long_net_pnl", sa.Numeric(), nullable=False),
        sa.Column("short_trades", sa.Integer(), nullable=False),
        sa.Column("short_net_pnl", sa.Numeric(), nullable=False),
        sa.Column("profit_factor", sa.Numeric(), nullable=True),
        sa.Column("expectancy", sa.Numeric(), nullable=True),
        sa.Column("top_trades_profit_share", sa.Numeric(), nullable=True),
        sa.Column("avg_gross_edge", sa.Numeric(), nullable=True),
        sa.Column("avg_round_trip_cost", sa.Numeric(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        _env_check("run_measures"),
        sa.CheckConstraint(f"drawdown_basis IN ({EQUITY_BASES})", name=op.f("ck_run_measures_drawdown_basis_valid")),
        sa.CheckConstraint("long_trades >= 0 AND short_trades >= 0", name=op.f("ck_run_measures_trades_nonnegative")),
        _env_fk("run_measures"),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_measures")),
    )

    op.drop_constraint(op.f("ck_promotions_verdict_valid"), "promotions", type_="check")
    op.create_check_constraint(
        op.f("ck_promotions_verdict_valid"), "promotions", "verdict IN ('pass', 'fail', 'insufficient')"
    )
    op.add_column("promotions", sa.Column("frozen_hash", sa.Text(), nullable=True))
    op.add_column("promotions", sa.Column("study_id", sa.Uuid(), nullable=True))
    op.add_column("promotions", sa.Column("thresholds_hash", sa.Text(), nullable=True))
    op.add_column(
        "promotions",
        sa.Column(
            "reasons", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "promotions",
        sa.Column(
            "metrics", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column("promotions", sa.Column("gate_commit", sa.Text(), nullable=True))
    op.create_foreign_key(op.f("fk_promotions_study_id_studies"), "promotions", "studies", ["study_id"], ["id"])
    op.create_check_constraint(
        op.f("ck_promotions_no_manual_live"), "promotions", "decided_by = 'gate' OR to_stage <> 'live'"
    )
    op.create_check_constraint(
        op.f("ck_promotions_gate_has_frozen_hash"), "promotions", "decided_by = 'manual' OR frozen_hash IS NOT NULL"
    )
    op.create_index(
        op.f("ix_promotions_frozen_hash_to_stage_decided_at"),
        "promotions",
        ["frozen_hash", "to_stage", "decided_at"],
        unique=False,
    )

    op.create_table(
        "promotion_folds",
        sa.Column("promotion_id", sa.Uuid(), nullable=False),
        sa.Column("fold", sa.Integer(), nullable=False),
        sa.Column("is_start", sa.Date(), nullable=False),
        sa.Column("is_end", sa.Date(), nullable=False),
        sa.Column("oos_start", sa.Date(), nullable=False),
        sa.Column("oos_end", sa.Date(), nullable=False),
        sa.Column("chosen_grid_point", sa.Integer(), nullable=False),
        sa.Column("is_sharpe", sa.Numeric(), nullable=True),
        sa.Column("oos_sharpe", sa.Numeric(), nullable=True),
        sa.Column("oos_return", sa.Numeric(), nullable=False),
        sa.Column("oos_trades", sa.Integer(), nullable=False),
        sa.Column("oos_net_pnl", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["promotion_id"], ["promotions.id"], name=op.f("fk_promotion_folds_promotion_id_promotions")
        ),
        sa.PrimaryKeyConstraint("promotion_id", "fold", name=op.f("pk_promotion_folds")),
    )

    new = ", ".join(NEW_TABLES)
    # Default privileges of 0002 gave the trader INSERT on these; spec 0015 gives it SELECT only.
    op.execute(f"REVOKE INSERT ON {new} FROM trader_app")
    op.execute(f"GRANT SELECT ON {new} TO trader_app, dashboard_app, backup_app")
    op.execute(f"GRANT SELECT, INSERT ON {', '.join(STUDY_TABLES)} TO {LAB_ROLE}")
    op.execute(f"GRANT SELECT ON promotions, promotion_folds, promotion_evidence TO {LAB_ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {GATE_ROLE}")
    gate_inserts = (*BUNDLE_TABLES, *NEW_TABLES, "promotions", "promotion_evidence")
    op.execute(f"GRANT INSERT ON {', '.join(gate_inserts)} TO {GATE_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {GATE_ROLE}")
    op.execute(f"REVOKE SELECT ON promotions, promotion_evidence FROM {LAB_ROLE}")
    op.drop_table("promotion_folds")
    op.drop_index(op.f("ix_promotions_frozen_hash_to_stage_decided_at"), table_name="promotions")
    op.drop_constraint(op.f("ck_promotions_gate_has_frozen_hash"), "promotions", type_="check")
    op.drop_constraint(op.f("ck_promotions_no_manual_live"), "promotions", type_="check")
    op.drop_constraint(op.f("fk_promotions_study_id_studies"), "promotions", type_="foreignkey")
    for column in ("gate_commit", "metrics", "reasons", "thresholds_hash", "study_id", "frozen_hash"):
        op.drop_column("promotions", column)
    # An insufficient row cannot survive the old CHECK; drop them first (they never existed before 0009).
    op.execute(
        "DELETE FROM promotion_evidence"
        " WHERE promotion_id IN (SELECT id FROM promotions WHERE verdict = 'insufficient')"
    )
    op.execute("DELETE FROM promotions WHERE verdict = 'insufficient'")
    op.drop_constraint(op.f("ck_promotions_verdict_valid"), "promotions", type_="check")
    op.create_check_constraint(op.f("ck_promotions_verdict_valid"), "promotions", "verdict IN ('pass', 'fail')")
    op.drop_table("run_measures")
    op.drop_table("run_daily_equity")
    op.drop_index("uq_study_runs_frozen_hash", table_name="study_runs")
    op.drop_table("study_runs")
    op.drop_index(op.f("ix_studies_strategy_id"), table_name="studies")
    op.drop_table("studies")
    op.drop_index(op.f("ix_strategy_versions_frozen_hash"), table_name="strategy_versions")
    op.drop_column("strategy_versions", "frozen_hash")
    op.drop_column("strategy_versions", "code_hash")
