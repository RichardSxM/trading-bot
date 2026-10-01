"""Data model (spec 0002): 18 tables, app role grants, and downsample_snapshots().

The roles themselves are created by ``trading_core.db_roles`` before this runs and are left
in place by the downgrade.

Revision ID: 0002_data_model
Revises: 0001_baseline
Create Date: 2026-10-01 11:09:23.753850+00:00
"""

# ruff: noqa: E501  # generated CHECK value lists

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_data_model"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLES: Final = "trader_app, dashboard_app, backup_app"
TABLES: Final = (
    "instruments",
    "strategies",
    "strategy_versions",
    "promotions",
    "promotion_evidence",
    "fx_rates",
    "strategy_runs",
    "run_results",
    "decisions",
    "orders",
    "order_events",
    "fills",
    "positions",
    "position_snapshots",
    "account_snapshots",
    "funding_payments",
    "risk_events",
    "control_commands",
)
# The only tables trader_app may UPDATE; fills and funding_payments only to set a NULL position_id.
TRADER_UPDATE_TABLES: Final = ("orders", "positions", "strategy_runs", "control_commands", "fills", "funding_payments")

# Keeps the newest raw row per series per UTC hour for rows older than 90 days, marks it
# hourly, deletes the rest. Runs as the owner (SECURITY DEFINER), so backup_app needs no DELETE.
DOWNSAMPLE_FUNCTION: Final = """
CREATE FUNCTION downsample_snapshots() RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    cutoff timestamptz := now() - interval '90 days';
    deleted_positions integer;
    deleted_accounts integer;
BEGIN
    WITH ranked AS (
        SELECT id, row_number() OVER (
            PARTITION BY position_id, date_trunc('hour', captured_at, 'UTC')
            ORDER BY captured_ns DESC
        ) AS rank_in_hour
        FROM position_snapshots
        WHERE granularity = 'raw' AND captured_at < cutoff
    ), removed AS (
        DELETE FROM position_snapshots AS s
        USING ranked AS r
        WHERE s.id = r.id AND r.rank_in_hour > 1
        RETURNING 1
    )
    SELECT count(*) INTO deleted_positions FROM removed;

    UPDATE position_snapshots SET granularity = 'hourly'
    WHERE granularity = 'raw' AND captured_at < cutoff;

    WITH ranked AS (
        SELECT id, row_number() OVER (
            PARTITION BY env, run_id, account_ref, currency, date_trunc('hour', captured_at, 'UTC')
            ORDER BY captured_ns DESC
        ) AS rank_in_hour
        FROM account_snapshots
        WHERE granularity = 'raw' AND captured_at < cutoff
    ), removed AS (
        DELETE FROM account_snapshots AS s
        USING ranked AS r
        WHERE s.id = r.id AND r.rank_in_hour > 1
        RETURNING 1
    )
    SELECT count(*) INTO deleted_accounts FROM removed;

    UPDATE account_snapshots SET granularity = 'hourly'
    WHERE granularity = 'raw' AND captured_at < cutoff;

    RETURN deleted_positions + deleted_accounts;
END;
$$
"""


def _grant_privileges() -> None:
    tables = ", ".join(TABLES)
    op.execute(f"GRANT SELECT ON {tables} TO {APP_ROLES}")
    op.execute(f"GRANT INSERT ON {tables} TO trader_app")
    op.execute(f"GRANT UPDATE ON {', '.join(TRADER_UPDATE_TABLES)} TO trader_app")
    # Future tables: SELECT for every app role, INSERT for the trader; UPDATE stays per table.
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {APP_ROLES}")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT INSERT ON TABLES TO trader_app")
    op.execute(DOWNSAMPLE_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION downsample_snapshots() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION downsample_snapshots() TO backup_app")


def _revoke_privileges() -> None:
    op.execute("DROP FUNCTION downsample_snapshots()")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE INSERT ON TABLES FROM trader_app")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {APP_ROLES}")


def upgrade() -> None:
    op.create_table(
        "control_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("cmd", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cmd IN ('stop', 'resume', 'flatten')", name=op.f("ck_control_commands_cmd_valid")),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_control_commands_env_valid")),
        sa.CheckConstraint(
            "outcome IN ('received', 'executed', 'expired', 'failed', 'duplicate')",
            name=op.f("ck_control_commands_outcome_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_commands")),
    )
    op.create_table(
        "fx_rates",
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_rate_positive")),
        sa.PrimaryKeyConstraint("rate_date", "base_currency", "quote_currency", name=op.f("pk_fx_rates")),
    )
    op.create_table(
        "instruments",
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        sa.Column("settle_currency", sa.Text(), nullable=False),
        sa.Column("tick_size", sa.Numeric(), nullable=False),
        sa.Column("lot_size", sa.Numeric(), nullable=False),
        sa.Column("multiplier", sa.Numeric(), nullable=False),
        sa.Column("is_inverse", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('perpetual')", name=op.f("ck_instruments_kind_valid")),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_instruments")),
    )
    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('scalping', 'hour_level')", name=op.f("ck_strategies_kind_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategies")),
        sa.UniqueConstraint("name", name=op.f("uq_strategies_name")),
    )
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("git_commit", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.id"], name=op.f("fk_strategy_versions_strategy_id_strategies")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_versions")),
        sa.UniqueConstraint("version_hash", name=op.f("uq_strategy_versions_version_hash")),
    )
    op.create_index(op.f("ix_strategy_versions_strategy_id"), "strategy_versions", ["strategy_id"], unique=False)
    op.create_table(
        "promotions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage", sa.Text(), nullable=False),
        sa.Column("to_stage", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("decided_by IN ('gate', 'manual')", name=op.f("ck_promotions_decided_by_valid")),
        sa.CheckConstraint("from_stage IN ('candidate', 'paper', 'live')", name=op.f("ck_promotions_from_stage_valid")),
        sa.CheckConstraint("to_stage IN ('candidate', 'paper', 'live')", name=op.f("ck_promotions_to_stage_valid")),
        sa.CheckConstraint("verdict IN ('pass', 'fail')", name=op.f("ck_promotions_verdict_valid")),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_promotions_strategy_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promotions")),
    )
    op.create_index(op.f("ix_promotions_strategy_version_id"), "promotions", ["strategy_version_id"], unique=False)
    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("trader_id", sa.Text(), nullable=False),
        sa.Column("run_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'running') = (ended_at IS NULL)", name=op.f("ck_strategy_runs_ended_at_iff_finished")
        ),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_strategy_runs_env_valid")),
        sa.CheckConstraint("mode <> 'live' OR env = 'live'", name=op.f("ck_strategy_runs_live_mode_in_live_env")),
        sa.CheckConstraint("mode IN ('backtest', 'replay', 'paper', 'live')", name=op.f("ck_strategy_runs_mode_valid")),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'aborted')", name=op.f("ck_strategy_runs_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_runs_strategy_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_runs")),
        sa.UniqueConstraint("id", "env", name=op.f("uq_strategy_runs_id_env")),
    )
    op.create_index(
        "ix_strategy_runs_env_trader_id_status", "strategy_runs", ["env", "trader_id", "status"], unique=False
    )
    op.create_index(
        "ix_strategy_runs_strategy_version_id_mode", "strategy_runs", ["strategy_version_id", "mode"], unique=False
    )
    op.create_table(
        "account_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("account_ref", sa.Text(), nullable=False),
        sa.Column("balance_total", sa.Numeric(), nullable=False),
        sa.Column("balance_free", sa.Numeric(), nullable=False),
        sa.Column("margin_used", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("granularity", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_account_snapshots_env_valid")),
        sa.CheckConstraint("granularity IN ('raw', 'hourly')", name=op.f("ck_account_snapshots_granularity_valid")),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_account_snapshots_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_snapshots")),
        sa.UniqueConstraint(
            "env",
            "run_id",
            "account_ref",
            "currency",
            "captured_ns",
            name="uq_account_snapshots_series_captured_ns",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_account_snapshots_env_account_ref_captured_at",
        "account_snapshots",
        ["env", "account_ref", "captured_at"],
        unique=False,
    )
    op.create_index(op.f("ix_account_snapshots_run_id"), "account_snapshots", ["run_id"], unique=False)
    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_verdict", sa.Text(), nullable=False),
        sa.Column("risk_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_decisions_env_valid")),
        sa.CheckConstraint("intent IN ('enter', 'exit', 'adjust')", name=op.f("ck_decisions_intent_valid")),
        sa.CheckConstraint("risk_verdict IN ('approved', 'rejected')", name=op.f("ck_decisions_risk_verdict_valid")),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_decisions_side_valid")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], name=op.f("fk_decisions_instrument_id_instruments")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_decisions_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
        sa.UniqueConstraint("id", "env", name=op.f("uq_decisions_id_env")),
    )
    op.create_index(op.f("ix_decisions_instrument_id"), "decisions", ["instrument_id"], unique=False)
    op.create_index("ix_decisions_run_id_decided_at", "decisions", ["run_id", "decided_at"], unique=False)
    op.create_table(
        "positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("position_ref", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("peak_qty", sa.Numeric(), nullable=False),
        sa.Column("avg_entry_px", sa.Numeric(), nullable=False),
        sa.Column("avg_exit_px", sa.Numeric(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=False),
        sa.Column("fees_total", sa.Numeric(), nullable=False),
        sa.Column("funding_total", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_ns", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_positions_env_valid")),
        sa.CheckConstraint("side IN ('long', 'short', 'flat')", name=op.f("ck_positions_side_valid")),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_positions_status_valid")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], name=op.f("fk_positions_instrument_id_instruments")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_positions_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_positions")),
        sa.UniqueConstraint("id", "env", name=op.f("uq_positions_id_env")),
        sa.UniqueConstraint(
            "run_id", "position_ref", "opened_ns", name=op.f("uq_positions_run_id_position_ref_opened_ns")
        ),
    )
    op.create_index("ix_positions_env_status", "positions", ["env", "status"], unique=False)
    op.create_index(op.f("ix_positions_instrument_id"), "positions", ["instrument_id"], unique=False)
    op.create_table(
        "promotion_evidence",
        sa.Column("promotion_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["promotion_id"], ["promotions.id"], name=op.f("fk_promotion_evidence_promotion_id_promotions")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["strategy_runs.id"], name=op.f("fk_promotion_evidence_run_id_strategy_runs")
        ),
        sa.PrimaryKeyConstraint("promotion_id", "run_id", name=op.f("pk_promotion_evidence")),
    )
    op.create_index(op.f("ix_promotion_evidence_run_id"), "promotion_evidence", ["run_id"], unique=False)
    op.create_table(
        "run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("sample", sa.Text(), nullable=False),
        sa.Column("return_after_costs", sa.Numeric(), nullable=False),
        sa.Column("max_drawdown", sa.Numeric(), nullable=False),
        sa.Column("risk_adjusted_ratio", sa.Numeric(), nullable=True),
        sa.Column("ratio_kind", sa.Text(), nullable=True),
        sa.Column("win_rate", sa.Numeric(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("fees_total", sa.Numeric(), nullable=False),
        sa.Column("funding_total", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_run_results_env_valid")),
        sa.CheckConstraint(
            "sample IN ('in_sample', 'out_of_sample', 'full')", name=op.f("ck_run_results_sample_valid")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_run_results_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("run_id", "sample", name=op.f("pk_run_results")),
    )
    op.create_table(
        "funding_payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_funding_payments_env_valid")),
        sa.CheckConstraint(
            "source <> 'kraken_log' OR external_id IS NOT NULL",
            name=op.f("ck_funding_payments_kraken_log_has_external_id"),
        ),
        sa.CheckConstraint("source IN ('kraken_log', 'computed')", name=op.f("ck_funding_payments_source_valid")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], name=op.f("fk_funding_payments_instrument_id_instruments")
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "env"],
            ["positions.id", "positions.env"],
            name=op.f("fk_funding_payments_position_id_env_positions"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_funding_payments_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funding_payments")),
    )
    op.create_index("ix_funding_payments_env_paid_at", "funding_payments", ["env", "paid_at"], unique=False)
    op.create_index(op.f("ix_funding_payments_instrument_id"), "funding_payments", ["instrument_id"], unique=False)
    op.create_index(op.f("ix_funding_payments_position_id"), "funding_payments", ["position_id"], unique=False)
    op.create_index(op.f("ix_funding_payments_run_id"), "funding_payments", ["run_id"], unique=False)
    op.create_index(
        "uq_funding_payments_env_external_id",
        "funding_payments",
        ["env", "external_id"],
        unique=True,
        postgresql_where=sa.text("source = 'kraken_log'"),
    )
    op.create_index(
        "uq_funding_payments_run_id_instrument_id_paid_ns",
        "funding_payments",
        ["run_id", "instrument_id", "paid_ns"],
        unique=True,
        postgresql_where=sa.text("source = 'computed'"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("venue_order_id", sa.Text(), nullable=True),
        sa.Column("parent_client_order_id", sa.Text(), nullable=True),
        sa.Column("order_list_id", sa.Text(), nullable=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("time_in_force", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column("trigger_price", sa.Numeric(), nullable=True),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("post_only", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filled_qty", sa.Numeric(), nullable=False),
        sa.Column("avg_px", sa.Numeric(), nullable=True),
        sa.Column("last_event_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_orders_env_valid")),
        sa.CheckConstraint(
            "order_type IN ('market', 'limit', 'stop_market', 'stop_limit', 'market_to_limit', 'market_if_touched', 'limit_if_touched', 'trailing_stop_market', 'trailing_stop_limit')",
            name=op.f("ck_orders_order_type_valid"),
        ),
        sa.CheckConstraint(
            "origin <> 'strategy' OR decision_id IS NOT NULL", name=op.f("ck_orders_strategy_order_has_decision")
        ),
        sa.CheckConstraint(
            "origin IN ('strategy', 'kill_switch', 'reconciliation', 'external')", name=op.f("ck_orders_origin_valid")
        ),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_orders_side_valid")),
        sa.CheckConstraint(
            "status IN ('initialized', 'denied', 'emulated', 'released', 'submitted', 'accepted', 'rejected', 'canceled', 'expired', 'triggered', 'pending_update', 'pending_cancel', 'partially_filled', 'filled', 'voided', 'unknown')",
            name=op.f("ck_orders_status_valid"),
        ),
        sa.CheckConstraint(
            "time_in_force IN ('gtc', 'ioc', 'fok', 'gtd', 'day', 'at_the_open', 'at_the_close')",
            name=op.f("ck_orders_time_in_force_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "env"], ["decisions.id", "decisions.env"], name=op.f("fk_orders_decision_id_env_decisions")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], name=op.f("fk_orders_instrument_id_instruments")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_orders_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("id", "env", name=op.f("uq_orders_id_env")),
        sa.UniqueConstraint("run_id", "client_order_id", name=op.f("uq_orders_run_id_client_order_id")),
    )
    op.create_index(op.f("ix_orders_decision_id"), "orders", ["decision_id"], unique=False)
    op.create_index("ix_orders_env_client_order_id", "orders", ["env", "client_order_id"], unique=False)
    op.create_index(
        "ix_orders_env_status",
        "orders",
        ["env", "status"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('canceled', 'denied', 'expired', 'filled', 'rejected')"),
    )
    op.create_index(op.f("ix_orders_instrument_id"), "orders", ["instrument_id"], unique=False)
    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("mark_px", sa.Numeric(), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(), nullable=False),
        sa.Column("liquidation_px", sa.Numeric(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("granularity", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_position_snapshots_env_valid")),
        sa.CheckConstraint("granularity IN ('raw', 'hourly')", name=op.f("ck_position_snapshots_granularity_valid")),
        sa.ForeignKeyConstraint(
            ["position_id", "env"],
            ["positions.id", "positions.env"],
            name=op.f("fk_position_snapshots_position_id_env_positions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_snapshots")),
        sa.UniqueConstraint("position_id", "captured_ns", name=op.f("uq_position_snapshots_position_id_captured_ns")),
    )
    op.create_index(
        "ix_position_snapshots_position_id_captured_at",
        "position_snapshots",
        ["position_id", "captured_at"],
        unique=False,
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("trade_id", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("fee", sa.Numeric(), nullable=False),
        sa.Column("fee_currency", sa.Text(), nullable=False),
        sa.Column("liquidity", sa.Text(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_fills_env_valid")),
        sa.CheckConstraint("liquidity IN ('maker', 'taker', 'no_liquidity')", name=op.f("ck_fills_liquidity_valid")),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_fills_side_valid")),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], name=op.f("fk_fills_instrument_id_instruments")
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "env"], ["orders.id", "orders.env"], name=op.f("fk_fills_order_id_env_orders")
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "env"], ["positions.id", "positions.env"], name=op.f("fk_fills_position_id_env_positions")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"], ["strategy_runs.id", "strategy_runs.env"], name=op.f("fk_fills_run_id_env_strategy_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fills")),
        sa.UniqueConstraint("run_id", "trade_id", name=op.f("uq_fills_run_id_trade_id")),
    )
    op.create_index("ix_fills_env_filled_at", "fills", ["env", "filled_at"], unique=False)
    op.create_index(op.f("ix_fills_instrument_id"), "fills", ["instrument_id"], unique=False)
    op.create_index(op.f("ix_fills_order_id"), "fills", ["order_id"], unique=False)
    op.create_index(op.f("ix_fills_position_id"), "fills", ["position_id"], unique=False)
    op.create_table(
        "order_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_order_events_env_valid")),
        sa.CheckConstraint(
            "kind IN ('order_initialized', 'order_denied', 'order_emulated', 'order_released', 'order_submitted', 'order_accepted', 'order_rejected', 'order_canceled', 'order_expired', 'order_triggered', 'order_pending_update', 'order_pending_cancel', 'order_modify_rejected', 'order_cancel_rejected', 'order_updated', 'order_filled')",
            name=op.f("ck_order_events_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "env"], ["orders.id", "orders.env"], name=op.f("fk_order_events_order_id_env_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_events")),
        sa.UniqueConstraint("order_id", "event_id", name=op.f("uq_order_events_order_id_event_id")),
    )
    op.create_table(
        "risk_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_ns", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("env IN ('demo', 'live')", name=op.f("ck_risk_events_env_valid")),
        sa.CheckConstraint(
            "kind IN ('order_rejected', 'limit_hit', 'halt', 'resume', 'stale_feed', 'reconcile_mismatch', 'record_conflict', 'orphan_fill')",
            name=op.f("ck_risk_events_kind_valid"),
        ),
        sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name=op.f("ck_risk_events_severity_valid")),
        sa.ForeignKeyConstraint(
            ["decision_id", "env"],
            ["decisions.id", "decisions.env"],
            name=op.f("fk_risk_events_decision_id_env_decisions"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "env"], ["orders.id", "orders.env"], name=op.f("fk_risk_events_order_id_env_orders")
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "env"],
            ["strategy_runs.id", "strategy_runs.env"],
            name=op.f("fk_risk_events_run_id_env_strategy_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_events")),
        sa.UniqueConstraint("env", "event_key", name=op.f("uq_risk_events_env_event_key")),
    )
    op.create_index(op.f("ix_risk_events_decision_id"), "risk_events", ["decision_id"], unique=False)
    op.create_index("ix_risk_events_env_occurred_at", "risk_events", ["env", "occurred_at"], unique=False)
    op.create_index(op.f("ix_risk_events_order_id"), "risk_events", ["order_id"], unique=False)
    op.create_index(op.f("ix_risk_events_run_id"), "risk_events", ["run_id"], unique=False)
    _grant_privileges()


def downgrade() -> None:
    _revoke_privileges()
    op.drop_index(op.f("ix_risk_events_run_id"), table_name="risk_events")
    op.drop_index(op.f("ix_risk_events_order_id"), table_name="risk_events")
    op.drop_index("ix_risk_events_env_occurred_at", table_name="risk_events")
    op.drop_index(op.f("ix_risk_events_decision_id"), table_name="risk_events")
    op.drop_table("risk_events")
    op.drop_table("order_events")
    op.drop_index(op.f("ix_fills_position_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_order_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_instrument_id"), table_name="fills")
    op.drop_index("ix_fills_env_filled_at", table_name="fills")
    op.drop_table("fills")
    op.drop_index("ix_position_snapshots_position_id_captured_at", table_name="position_snapshots")
    op.drop_table("position_snapshots")
    op.drop_index(op.f("ix_orders_instrument_id"), table_name="orders")
    op.drop_index(
        "ix_orders_env_status",
        table_name="orders",
        postgresql_where=sa.text("status NOT IN ('canceled', 'denied', 'expired', 'filled', 'rejected')"),
    )
    op.drop_index("ix_orders_env_client_order_id", table_name="orders")
    op.drop_index(op.f("ix_orders_decision_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(
        "uq_funding_payments_run_id_instrument_id_paid_ns",
        table_name="funding_payments",
        postgresql_where=sa.text("source = 'computed'"),
    )
    op.drop_index(
        "uq_funding_payments_env_external_id",
        table_name="funding_payments",
        postgresql_where=sa.text("source = 'kraken_log'"),
    )
    op.drop_index(op.f("ix_funding_payments_run_id"), table_name="funding_payments")
    op.drop_index(op.f("ix_funding_payments_position_id"), table_name="funding_payments")
    op.drop_index(op.f("ix_funding_payments_instrument_id"), table_name="funding_payments")
    op.drop_index("ix_funding_payments_env_paid_at", table_name="funding_payments")
    op.drop_table("funding_payments")
    op.drop_table("run_results")
    op.drop_index(op.f("ix_promotion_evidence_run_id"), table_name="promotion_evidence")
    op.drop_table("promotion_evidence")
    op.drop_index(op.f("ix_positions_instrument_id"), table_name="positions")
    op.drop_index("ix_positions_env_status", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_decisions_run_id_decided_at", table_name="decisions")
    op.drop_index(op.f("ix_decisions_instrument_id"), table_name="decisions")
    op.drop_table("decisions")
    op.drop_index(op.f("ix_account_snapshots_run_id"), table_name="account_snapshots")
    op.drop_index("ix_account_snapshots_env_account_ref_captured_at", table_name="account_snapshots")
    op.drop_table("account_snapshots")
    op.drop_index("ix_strategy_runs_strategy_version_id_mode", table_name="strategy_runs")
    op.drop_index("ix_strategy_runs_env_trader_id_status", table_name="strategy_runs")
    op.drop_table("strategy_runs")
    op.drop_index(op.f("ix_promotions_strategy_version_id"), table_name="promotions")
    op.drop_table("promotions")
    op.drop_index(op.f("ix_strategy_versions_strategy_id"), table_name="strategy_versions")
    op.drop_table("strategy_versions")
    op.drop_table("strategies")
    op.drop_table("instruments")
    op.drop_table("fx_rates")
    op.drop_table("control_commands")
