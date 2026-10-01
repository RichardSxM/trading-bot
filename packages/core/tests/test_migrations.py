"""covers: spec 0001 migrate service (Alembic baseline) and the shared naming convention."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, String, Table, UniqueConstraint
from sqlalchemy.schema import CreateTable

from trading_core.db import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def test_alembic_has_one_head_the_baseline_with_nothing_before_it():
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert scripts.get_heads() == ["0001_baseline"]
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
