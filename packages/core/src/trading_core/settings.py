"""Non secret configuration shared by every process, loaded from environment variables.

Secrets (Kraken keys, real database credentials) are feature 4 and do not live here.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TradingEnv = Literal["demo", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Required: every process refuses to start when this is unset or unknown.
    trading_env: TradingEnv
    process_name: str = "unknown"
    log_level: str = "INFO"

    redis_host: str = "redis"
    redis_port: int = 6379

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "trading"
    postgres_user: str = "trading"
    postgres_password: str = ""

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

    @property
    def database_url(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        return f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"


class RoleSettings(BaseSettings):
    """Passwords for the app roles, read only by the migrate role setup (``trading_core.db_roles``).

    Kept apart from ``Settings`` so no other process needs them. Every one is required and
    must be non empty, so role setup refuses to run with a missing password.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    postgres_trader_password: str = Field(min_length=1)
    postgres_dashboard_password: str = Field(min_length=1)
    postgres_backup_password: str = Field(min_length=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # fields come from the environment
