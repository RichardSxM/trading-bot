"""Secrets from files, never from the environment (spec 0003).

Compose mounts each secret read only at ``/run/secrets/<name>`` into only the services that
need it. ``read_secret`` reads one, refuses a broken one with a typed reason, logs its
fingerprint, and registers the value so the JSON log formatter can redact it anywhere.
"""

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Final, Literal
from urllib.parse import quote

from pydantic import SecretStr

log = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR: Final = Path("/run/secrets")

# Short values would shred unrelated log text when redacted, so none is accepted.
MIN_SECRET_LENGTH: Final = 16

# Rotate the live Kraken key at least this often (spec 0003, rotation).
KEY_MAX_AGE_DAYS: Final = 90

# Names inside the container (spec 0003, Secret inventory). Compose mounts each service's own
# role password under the generic names, so code never knows which role it runs as.
POSTGRES_PASSWORD: Final = "postgres_password"
REDIS_PASSWORD: Final = "redis_password"
# The lab role's password as the migrate service mounts it (spec 0008); laptop only. The lab
# service itself mounts it under POSTGRES_PASSWORD.
POSTGRES_LAB_PASSWORD: Final = "postgres_lab_password"
# The gate role's password as the migrate service mounts it (spec 0015); optional on the laptop.
# The gate service itself mounts it under POSTGRES_PASSWORD.
POSTGRES_GATE_PASSWORD: Final = "postgres_gate_password"
KRAKEN_API_KEY: Final = "kraken_api_key"
KRAKEN_API_SECRET: Final = "kraken_api_secret"
# The dashboard login (spec 0004), mounted into the dashboard only.
DASHBOARD_PASSWORD_HASH: Final = "dashboard_password_hash"
DASHBOARD_TOTP_SEED: Final = "dashboard_totp_seed"
DASHBOARD_SESSION_KEY: Final = "dashboard_session_key"
# Phone alerts (spec 0013): Pushover's app token and user key, and the healthchecks.io ping URLs.
# The alerter loads the Pushover pair and its own ping URL only when it really pushes.
PUSHOVER_APP_TOKEN: Final = "pushover_app_token"
PUSHOVER_USER_KEY: Final = "pushover_user_key"
HC_ALERTER_URL: Final = "hc_alerter_url"
HC_BACKUP_URL: Final = "hc_backup_url"
# The migrate service's name for the alerter role's password; the alerter mounts it as POSTGRES_PASSWORD.
POSTGRES_ALERTER_PASSWORD: Final = "postgres_alerter_password"

Reason = Literal["missing", "empty", "not_a_file", "unreadable", "too_short", "whitespace"]

# Every form a loaded value may take in a log line, mapped to the secret's name.
_registry: dict[str, str] = {}


class SecretUnavailable(Exception):
    """A required secret file is missing or unusable. The message never holds the value."""

    def __init__(self, name: str, reason: Reason) -> None:
        super().__init__(f"secret {name!r} is unavailable: {reason}")
        self.name = name
        self.reason: Reason = reason


def fingerprint(value: str) -> str:
    """First 8 hex characters of the value's sha256: enough to tell versions apart, useless to an attacker."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def read_secret(name: str, secrets_dir: Path = DEFAULT_SECRETS_DIR) -> SecretStr:
    """Read ``secrets_dir/name``, trimmed of surrounding whitespace.

    Raises ``SecretUnavailable`` when the file is missing, not a regular file, unreadable,
    empty, shorter than ``MIN_SECRET_LENGTH``, or contains inner whitespace. On success it
    logs ``secret loaded`` with the fingerprint and registers the value for redaction.
    """
    path = secrets_dir / name
    if not path.exists():
        raise SecretUnavailable(name, "missing")
    if not path.is_file():
        raise SecretUnavailable(name, "not_a_file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        raise SecretUnavailable(name, "unreadable") from None
    if not value:
        raise SecretUnavailable(name, "empty")
    if any(char.isspace() for char in value):
        raise SecretUnavailable(name, "whitespace")
    if len(value) < MIN_SECRET_LENGTH:
        raise SecretUnavailable(name, "too_short")

    _register(name, value)
    log.info("secret loaded", extra={"fields": {"secret": name, "fp": fingerprint(value)}})
    return SecretStr(value)


def load_required(names: Iterable[str], secrets_dir: Path = DEFAULT_SECRETS_DIR) -> dict[str, SecretStr]:
    """Read every named secret; the first unusable one raises ``SecretUnavailable``."""
    return {name: read_secret(name, secrets_dir) for name in names}


def redact(text: str) -> str:
    """Replace every registered secret form in ``text`` with ``[REDACTED:<name>]``."""
    for form in sorted(_registry, key=len, reverse=True):
        if form in text:
            text = text.replace(form, f"[REDACTED:{_registry[form]}]")
    return text


def reset_registry() -> None:
    """Forget every registered value. For tests only: the registry is process wide."""
    _registry.clear()


def key_rotation_age(rotated_on: date, today: date) -> int:
    """Days since the key was rotated. Raises ``ValueError`` for a date in the future."""
    if rotated_on > today:
        raise ValueError(f"key rotation date {rotated_on.isoformat()} is in the future")
    return (today - rotated_on).days


def _register(name: str, value: str) -> None:
    # Raw, URL quoted (as in a database URL), and JSON escaped (as inside a log line).
    for form in (value, quote(value, safe=""), json.dumps(value)[1:-1]):
        _registry[form] = name
