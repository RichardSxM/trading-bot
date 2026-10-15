"""Integration fixtures: a throwaway Postgres database per test session (spec 0002).

Run in the migrate service, which is on the Compose network as the owner role ``trading``:
``docker compose run --rm -T migrate pytest -q -m integration tests/integration``. The session creates
``trading_test_<hex>``, runs role setup and ``alembic upgrade head`` into it, and drops it at
the end. Roles are cluster wide and shared with the dev stack; setup is idempotent. Passwords
come from the migrate service's mounted secret files (spec 0003).
"""

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid7

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, pool
from sqlalchemy.orm import Session

from trading_core.db_roles import MIGRATE_SECRETS, load_optional, role_passwords, setup_roles
from trading_core.recording import repository
from trading_core.recording.enums import (
    Env,
    Intent,
    Liquidity,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RiskVerdict,
    RunMode,
    StrategyKind,
    TimeInForce,
)
from trading_core.recording.rows import DecisionRow, FillRow, InstrumentRow, OrderRow, PositionRow
from trading_core.secrets import DEFAULT_SECRETS_DIR, POSTGRES_PASSWORD, load_required
from trading_core.settings import Settings, get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
INSTRUMENT = "PF_XBTUSD.KRAKEN"

# Captured at import: the autouse clean_env fixture removes these before every test.
_ENV = {name: value for name, value in os.environ.items() if name.startswith("POSTGRES_")}

_HERE = Path(__file__).resolve().parent

# The migrate service mounts the owner password and the three role passwords here.
_SECRETS_DIR = Path(os.environ.get("SECRETS_DIR_IN_CONTAINER", str(DEFAULT_SECRETS_DIR)))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Mark every test in this folder ``integration`` before ``-m`` selects."""
    for item in items:
        if _HERE in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.integration)


def owner_settings(database: str | None = None) -> Settings:
    """Settings for the owner role from the captured environment, optionally for another database."""
    return Settings(
        trading_env="demo",
        postgres_host=_ENV.get("POSTGRES_HOST", "postgres"),
        postgres_port=int(_ENV.get("POSTGRES_PORT", "5432")),
        postgres_db=database or _ENV.get("POSTGRES_DB", "trading"),
        postgres_user=_ENV.get("POSTGRES_USER", "trading"),
    )


def migrate_secrets() -> dict[str, SecretStr]:
    """The owner password, the three role passwords, and the lab password when mounted.

    Roles are cluster wide, so leaving out a mounted lab password would reset the dev stack's
    lab_app to NOLOGIN.
    """
    return load_required(MIGRATE_SECRETS, _SECRETS_DIR) | load_optional(_SECRETS_DIR)


def owner_url(database: str | None = None) -> str:
    """SQLAlchemy URL for the owner role, optionally to another database."""
    return owner_settings(database).database_url(migrate_secrets()[POSTGRES_PASSWORD])


def role_url(database: str, role: str) -> str:
    """SQLAlchemy URL for one app role on ``database``."""
    settings = owner_settings(database).model_copy(update={"postgres_user": role})
    password = role_passwords(migrate_secrets())[role]
    if password is None:
        pytest.skip(f"{role} has no mounted password here, so it cannot log in")
    return settings.database_url(SecretStr(password))


def _admin() -> psycopg.Connection:
    settings = owner_settings()
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=migrate_secrets()[POSTGRES_PASSWORD].get_secret_value(),
        autocommit=True,
    )


def migrate(engine: Engine, revision: str = "head", *, down: bool = False) -> None:
    """Run Alembic on ``engine``'s database through the passed connection hook in env.py."""
    config = Config(str(ALEMBIC_INI))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if down:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def create_database() -> str:
    """Create ``trading_test_<hex>`` with the app roles set up, and return its name."""
    name = f"trading_test_{secrets.token_hex(4)}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(owner_url(name).replace("+psycopg", "")) as conn:
        setup_roles(conn, name, role_passwords(migrate_secrets()))
    return name


def drop_database(name: str) -> None:
    """Drop a test database, closing any connection still open on it."""
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    """Name of a fresh database with roles set up and every migration applied."""
    name = create_database()
    try:
        engine = create_engine(owner_url(name), poolclass=pool.NullPool)
        migrate(engine)
        engine.dispose()
        yield name
    finally:
        drop_database(name)


@pytest.fixture(scope="session")
def engine(database: str) -> Iterator[Engine]:
    """Owner engine on the test database."""
    owner = create_engine(owner_url(database), poolclass=pool.NullPool)
    yield owner
    owner.dispose()


@pytest.fixture
def trading_env(monkeypatch) -> Iterator[None]:
    """Run the write API as a demo process."""
    monkeypatch.setenv("TRADING_ENV", "demo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def session(engine: Engine, trading_env: None) -> Iterator[Session]:
    """A session on the test database, rolled back at the end unless the test committed."""
    with Session(engine) as db:
        yield db
        db.rollback()


@dataclass
class World:
    """Ids of a demo paper run on BTC, plus row builders that default to it."""

    version_id: UUID
    run_id: UUID
    clock: int = 1_800_000_000_000_000_000

    def tick(self) -> int:
        """A later nanosecond time on every call."""
        self.clock += 1_000_000_000
        return self.clock

    def decision(self, **overrides: object) -> DecisionRow:
        values: dict[str, object] = {
            "env": Env.DEMO,
            "run_id": self.run_id,
            "instrument_id": INSTRUMENT,
            "intent": Intent.ENTER,
            "side": OrderSide.BUY,
            "reason": "test entry",
            "inputs": {"mid": Decimal("60000.5")},
            "risk_verdict": RiskVerdict.APPROVED,
            "decided_ns": self.tick(),
        }
        return DecisionRow(**(values | overrides))  # type: ignore[arg-type]

    def order(self, **overrides: object) -> OrderRow:
        values: dict[str, object] = {
            "env": Env.DEMO,
            "run_id": self.run_id,
            "origin": OrderOrigin.STRATEGY,
            "client_order_id": f"O-{uuid7().hex[-12:]}",
            "instrument_id": INSTRUMENT,
            "side": OrderSide.BUY,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.GTC,
            "quantity": Decimal("0.001"),
            "price": Decimal("60000.5"),
            "status": OrderStatus.ACCEPTED,
            "last_event_ns": self.tick(),
        }
        return OrderRow(**(values | overrides))  # type: ignore[arg-type]

    def fill(self, client_order_id: str, **overrides: object) -> FillRow:
        values: dict[str, object] = {
            "env": Env.DEMO,
            "run_id": self.run_id,
            "client_order_id": client_order_id,
            "instrument_id": INSTRUMENT,
            "trade_id": f"T-{uuid7().hex[-12:]}",
            "side": OrderSide.BUY,
            "quantity": Decimal("0.001"),
            "price": Decimal("60000.5"),
            "fee": Decimal("0.03000025"),
            "fee_currency": "USD",
            "liquidity": Liquidity.MAKER,
            "filled_ns": self.tick(),
        }
        return FillRow(**(values | overrides))  # type: ignore[arg-type]

    def position(self, **overrides: object) -> PositionRow:
        values: dict[str, object] = {
            "env": Env.DEMO,
            "run_id": self.run_id,
            "instrument_id": INSTRUMENT,
            "position_ref": f"P-{uuid7().hex[-12:]}",
            "side": PositionSide.LONG,
            "quantity": Decimal("0.001"),
            "peak_qty": Decimal("0.001"),
            "avg_entry_px": Decimal("60000.5"),
            "realized_pnl": Decimal(0),
            "fees_total": Decimal("0.03000025"),
            "funding_total": Decimal(0),
            "currency": "USD",
            "opened_ns": self.tick(),
            "last_event_ns": self.clock,
        }
        return PositionRow(**(values | overrides))  # type: ignore[arg-type]


def instrument_row() -> InstrumentRow:
    """The BTC perpetual as the instrument provider describes it."""
    return InstrumentRow(
        instrument_id=INSTRUMENT,
        venue="KRAKEN",
        symbol="PF_XBTUSD",
        base_currency="BTC",
        quote_currency="USD",
        settle_currency="USD",
        tick_size=Decimal("0.5"),
        lot_size=Decimal("0.0001"),
        multiplier=Decimal(1),
        is_inverse=False,
    )


@pytest.fixture
def world(session: Session) -> World:
    """A fresh demo paper run (its own trader id) on a strategy version with a real looking commit."""
    repository.upsert_instrument(session, instrument_row())
    version_id = repository.ensure_strategy_version(
        session, "tracer", StrategyKind.SCALPING, "0123456789abcdef", {"size": Decimal("0.001"), "window": 20}
    )
    run_id = repository.start_run(
        session,
        env=Env.DEMO,
        mode=RunMode.PAPER,
        version_id=version_id,
        trader_id=f"TRADER-{uuid7().hex[-8:]}",
        run_config={},
        started_ns=1_800_000_000_000_000_000,
    )
    return World(version_id=version_id, run_id=run_id)
