"""covers: spec 0002 AC-1 (18 tables, plus spec 0008's run_fill_stats), AC-9 (no float column, numeric money,
timestamptz, ns bigint)."""

from sqlalchemy import BigInteger, DateTime, Numeric
from sqlalchemy.dialects import postgresql

import trading_core.recording.models  # noqa: F401  # registers the tables
from trading_core.db import Base

EXPECTED_TABLES = {
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
    "run_fill_stats",
}


def _columns():
    for table in Base.metadata.sorted_tables:
        yield from table.columns


def test_the_model_has_exactly_the_spec_tables():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_column_is_a_float_type():
    dialect = postgresql.dialect()
    compiled = {str(c): c.type.compile(dialect=dialect).upper() for c in _columns()}

    assert [name for name, sql_type in compiled.items() if sql_type.startswith(("REAL", "DOUBLE", "FLOAT"))] == []
    assert [str(c) for c in _columns() if isinstance(c.type, Numeric) and not c.type.asdecimal] == []


def test_numeric_columns_have_no_fixed_precision_or_scale():
    numerics = [c for c in _columns() if isinstance(c.type, Numeric)]

    assert numerics
    assert [str(c) for c in numerics if c.type.precision is not None or c.type.scale is not None] == []


def test_every_datetime_is_timestamptz_and_every_ns_is_bigint():
    assert [str(c) for c in _columns() if isinstance(c.type, DateTime) and not c.type.timezone] == []
    assert [str(c) for c in _columns() if c.name.endswith("_ns") and not isinstance(c.type, BigInteger)] == []


def test_every_ns_event_time_has_an_at_sibling():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name.endswith("_ns") and column.name != "last_event_ns":
                assert column.name.removesuffix("_ns") + "_at" in table.columns, str(column)


def test_env_scoped_tables_link_parents_through_env():
    for table in Base.metadata.sorted_tables:
        if "env" not in table.columns:
            continue
        for fk in table.foreign_key_constraints:
            referred = fk.referred_table
            if "env" in referred.columns:
                assert "env" in fk.column_keys, f"{table.name} -> {referred.name} skips env"
