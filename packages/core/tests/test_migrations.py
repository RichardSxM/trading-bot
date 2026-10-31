"""covers: spec 0001 migrate service (Alembic baseline), spec 0002 AC-1 (one head), and the naming convention."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, String, Table, UniqueConstraint
from sqlalchemy.schema import CreateTable

from trading_core.db import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def test_alembic_has_one_head_the_data_model_on_top_of_the_baseline():
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert scripts.get_heads() == ["0013_alerter_fx_rates"]
    assert scripts.get_revision("0013_alerter_fx_rates").down_revision == "0012_reconciliations"
    assert scripts.get_revision("0012_reconciliations").down_revision == "0011_gate_default_select"
    assert scripts.get_revision("0011_gate_default_select").down_revision == "0010_one_holdout_per_study"
    assert scripts.get_revision("0010_one_holdout_per_study").down_revision == "0009_gate"
    assert scripts.get_revision("0009_gate").down_revision == "0008_alerter_instruments"
    assert scripts.get_revision("0008_alerter_instruments").down_revision == "0007_backup_sequence_grant"
    assert scripts.get_revision("0007_backup_sequence_grant").down_revision == "0006_alerts"
    assert scripts.get_revision("0006_alerts").down_revision == "0005_lab"
    assert scripts.get_revision("0005_lab").down_revision == "0004_backup_dump_grant"
    assert scripts.get_revision("0004_backup_dump_grant").down_revision == "0003_pause_commands"
    assert scripts.get_revision("0003_pause_commands").down_revision == "0002_data_model"
    assert scripts.get_revision("0002_data_model").down_revision == "0001_baseline"
    assert scripts.get_revision("0001_baseline").down_revision is None


def test_constraint_names_follow_the_naming_convention():
    table = Table(
        "probe",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String, index=True),
        UniqueConstraint("code"),
    )
    try:
        ddl = str(CreateTable(table))

        assert "CONSTRAINT pk_probe PRIMARY KEY" in ddl
        assert "CONSTRAINT uq_probe_code UNIQUE" in ddl
        assert {index.name for index in table.indexes} == {"ix_probe_code"}
    finally:
        Base.metadata.remove(table)


def test_multi_column_names_use_every_column():
    table = Table(
        "probe",
        Base.metadata,
        Column("a", Integer, primary_key=True),
        Column("b", String),
        UniqueConstraint("a", "b"),
    )
    try:
        assert "CONSTRAINT uq_probe_a_b UNIQUE" in str(CreateTable(table))
    finally:
        Base.metadata.remove(table)
