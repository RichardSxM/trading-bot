"""The alerter's settings and the secrets it loads, which depend on the delivery mode (spec 0013 AC-20)."""

from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from trading_core.alerts.engine import EngineConfig
from trading_core.secrets import (
    HC_ALERTER_URL,
    POSTGRES_PASSWORD,
    PUSHOVER_APP_TOKEN,
    PUSHOVER_USER_KEY,
    REDIS_PASSWORD,
)
from trading_core.settings import AlertSettings


class AlerterSettings(AlertSettings):
    """``AlertSettings`` plus the alerter's own plumbing.

    ``alerts_docker_url`` is the read only Docker socket proxy (spec 0013, *Security model*).
    ``alerts_host_log_dir`` is the VPS's ``/var/log/kfs``, mounted read only (``compose.vps.yaml``);
    where it is absent (the laptop) the host lines and the disk check are off.
    """

    alerts_docker_url: str = "http://docker-proxy:2375"
    alerts_host_log_dir: Path = Path("/host/kfs-log")
    # The daily ECB rate (spec 0024 AC-15, AC-17): where to fetch it, the first date to backfill,
    # and how many calendar days old the newest rate may get before ``fx_rates_stale`` opens.
    fx_ecb_url: str = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
    fx_history_from: date = date(2026, 1, 1)
    fx_stale_days: int = Field(default=6, ge=1, le=14)

    @field_validator("fx_ecb_url")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.netloc or parts.query:
            raise ValueError("FX_ECB_URL must be an https URL without a query")
        return value

    @field_validator("fx_history_from")
    @classmethod
    def _not_future(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("FX_HISTORY_FROM must not be in the future")
        return value


def alerter_secrets(settings: AlerterSettings) -> tuple[str, ...]:
    """The database and Redis passwords always; Pushover and the ping URL only when really pushing.

    So a missing Pushover file stops the alerter at start in ``pushover`` mode only (AC-20).
    """
    base = (POSTGRES_PASSWORD, REDIS_PASSWORD)
    if settings.alerts_delivery == "pushover":
        return (*base, PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, HC_ALERTER_URL)
    return base


def engine_config(settings: AlerterSettings) -> EngineConfig:
    """The engine's timings from the ``ALERTS_*`` settings."""
    return EngineConfig(
        env=settings.trading_env,
        renotify_s=settings.alerts_renotify_s,
        critical_renotify_s=settings.alerts_critical_renotify_s,
        hourly_cap=settings.alerts_hourly_cap,
        entry_block_s=settings.alerts_entry_block_s,
    )
