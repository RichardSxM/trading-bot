"""Secrets from files, never from the environment (spec 0003).

Compose mounts each secret read only at ``/run/secrets/<name>`` into only the services that
need it. ``read_secret`` reads one, refuses a broken one with a typed reason, logs its
fingerprint, and registers the value so the JSON log formatter can redact it anywhere.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable
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
KRAKEN_API_KEY: Final = "kraken_api_key"
KRAKEN_API_SECRET: Final = "kraken_api_secret"

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


KeyCheckState = Literal["confirmed", "waiting", "failed"]


def key_check_state(account_confirmed: bool, elapsed_s: float, timeout_s: float) -> KeyCheckState:
    """Where the live startup key check stands (spec 0003 AC-8).

    ``confirmed`` once an authenticated Kraken account is in the cache, ``failed`` when
    ``timeout_s`` passed without one, else ``waiting``. A confirmation always wins, even late.
    """
    if account_confirmed:
        return "confirmed"
    return "failed" if elapsed_s >= timeout_s else "waiting"


async def watch_key_check(
    is_confirmed: Callable[[], bool],
    on_failed: Callable[[], None],
    timeout_s: float,
    poll_s: float = 1.0,
    now: Callable[[], float] = time.monotonic,
) -> KeyCheckState:
    """Poll ``is_confirmed`` until the key check decides (spec 0003 AC-8).

    Runs as a task on the node's event loop rather than as an actor: when the Kraken client
    cannot connect, Nautilus never starts the trader, so an actor would never run. On
    failure it logs ``kraken_auth_failed`` and calls ``on_failed`` (which stops the node).
    """
    started = now()
    while True:
        elapsed_s = now() - started
        state = key_check_state(is_confirmed(), elapsed_s, timeout_s)
        if state == "confirmed":
            log.info("kraken account confirmed", extra={"fields": {"elapsed_s": round(elapsed_s, 1)}})
            return state
        if state == "failed":
            log.critical(
                "no authenticated Kraken account, stopping",
                extra={"fields": {"code": "kraken_auth_failed", "timeout_s": timeout_s}},
            )
            on_failed()
            return state
        await asyncio.sleep(poll_s)


def _register(name: str, value: str) -> None:
    # Raw, URL quoted (as in a database URL), and JSON escaped (as inside a log line).
    for form in (value, quote(value, safe=""), json.dumps(value)[1:-1]):
        _registry[form] = name
