"""The alerter's settings and the secrets it loads, which depend on the delivery mode (spec 0013 AC-20)."""

from pathlib import Path

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
