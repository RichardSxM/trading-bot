"""SQLAlchemy metadata shared by every process and by Alembic.

Tables live in ``trading_core.recording.models`` (spec 0002) and register on this base.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, MetaData, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Multi column names use every column (``column_0_N``), so two indexes or keys that share a
# first column never collide. SQLAlchemy shortens a name past 63 characters with a hash.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base: ``NAMING_CONVENTION`` on every constraint, and the column types below.

    ``Decimal`` is unscaled ``numeric`` (exact, never a float), ``datetime`` is ``timestamptz``
    (UTC aware only), and a dict is JSONB.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012  # SQLAlchemy reads this class attribute
        Decimal: Numeric(asdecimal=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        str: Text(),
        uuid.UUID: Uuid(as_uuid=True),
        dict[str, object]: JSONB(),
    }
