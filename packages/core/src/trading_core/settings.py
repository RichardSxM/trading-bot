"""Non secret configuration shared by every process, loaded from environment variables.

Secrets never come from the environment: they are files read by ``trading_core.secrets``
(spec 0003). Settings only says where those files live.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_core.secrets import DEFAULT_SECRETS_DIR

TradingEnv = Literal["demo", "live"]


class Settings(BaseSettings):
    # No validate_by_name: it would also read SECRETS_DIR (the host folder) into secrets_dir.
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Required: every process refuses to start when this is unset or unknown.
    trading_env: TradingEnv
    process_name: str = "unknown"
    log_level: str = "INFO"

    # Where the mounted secret files live. Only tests point it elsewhere; the name keeps it
    # apart from SECRETS_DIR, which is the host folder Compose substitutes and no service sees.
    secrets_dir: Path = Field(default=DEFAULT_SECRETS_DIR, validation_alias="SECRETS_DIR_IN_CONTAINER")

    redis_host: str = "redis"
    redis_port: int = 6379
    # Each service's own Redis ACL user (spec 0003); None for processes without Redis.
    redis_username: str | None = None

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "trading"
    postgres_user: str = "trading"

    # The commit the image was built from (Docker build arg GIT_COMMIT); recorded on every
    # strategy version, and a live run refuses ``unknown``.
    git_commit: str = "unknown"

    clock_servers: str = "0.pool.ntp.org,1.pool.ntp.org,2.pool.ntp.org"
    max_clock_offset_ms: float = Field(default=250, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    @property
    def clock_server_list(self) -> list[str]:
        return [s.strip() for s in self.clock_servers.split(",") if s.strip()]

    def database_url(self, password: SecretStr, database: str | None = None) -> str:
        """SQLAlchemy URL for ``postgres_user`` with the given password, optionally to another database.

        The quoted password form is already registered for log redaction by the secret loader.
        """
        user = quote(self.postgres_user, safe="")
        secret = quote(password.get_secret_value(), safe="")
        dbname = database or self.postgres_db
        return f"postgresql+psycopg://{user}:{secret}@{self.postgres_host}:{self.postgres_port}/{dbname}"


class PaperLoopSettings(Settings):
    """The placeholder strategy, risk caps, sandbox, and recording settings of the paper loop (spec 0006).

    Money, fractions, and durations are ``Decimal``. The trader's settings subclass this, so a
    bad value refuses the trader's start.
    """

    placeholder_interval_min: int = Field(default=15, gt=0)
    placeholder_hold_min: int = Field(default=10, ge=1)
    placeholder_lookback_min: int = Field(default=5, ge=1)
    placeholder_stop_pct: Decimal = Field(default=Decimal("0.3"), gt=0, lt=100)
    placeholder_notional_usd: Decimal = Field(default=Decimal(100), gt=0)

    risk_max_order_notional_usd: Decimal = Field(default=Decimal(250), gt=0)
    risk_max_leverage: Decimal = Field(default=Decimal(2), gt=0)
    risk_stale_quote_s: Decimal = Field(default=Decimal(10), gt=0)
    stop_accept_timeout_s: Decimal = Field(default=Decimal(5), gt=0)

    sandbox_starting_balance_usd: Decimal = Field(default=Decimal(1000), gt=0)
    sandbox_maker_fee: Decimal = Field(default=Decimal("0.0002"), ge=0, lt=1)
    sandbox_taker_fee: Decimal = Field(default=Decimal("0.0005"), ge=0, lt=1)

    recording_max_backlog_s: Decimal = Field(default=Decimal(30), gt=0)
    recording_queue_max: int = Field(default=10_000, gt=0)
    recording_drain_s: Decimal = Field(default=Decimal(5), gt=0)
    spool_dir: Path = Path("/data/spool")

    @model_validator(mode="after")
    def _placeholder_timing(self) -> Self:
        if 60 % self.placeholder_interval_min:
            raise ValueError("PLACEHOLDER_INTERVAL_MIN must divide 60")
        if self.placeholder_hold_min >= self.placeholder_interval_min:
            raise ValueError("PLACEHOLDER_HOLD_MIN must be below PLACEHOLDER_INTERVAL_MIN")
        return self

    def strategy_config(self) -> dict[str, str | int]:
        """The ``PLACEHOLDER_*`` and ``RISK_*`` settings as a dict, Decimals as strings (the strategy version)."""
        return {
            name: value if isinstance(value, int) else str(value)
            for name, value in self.model_dump().items()
            if name.startswith(("placeholder_", "risk_"))
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # fields come from the environment
