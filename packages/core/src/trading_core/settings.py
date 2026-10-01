"""Non secret configuration shared by every process, loaded from environment variables.

Secrets never come from the environment: they are files read by ``trading_core.secrets``
(spec 0003). Settings only says where those files live.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # fields come from the environment
