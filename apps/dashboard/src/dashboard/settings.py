"""The dashboard's settings, secret files, and boot guard (spec 0004)."""

import base64
import binascii
from collections.abc import Mapping
from typing import ClassVar, Final, Self

from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError
from pydantic import Field, SecretStr, model_validator

from trading_core.env_guard import EnvGuardRefused
from trading_core.secrets import (
    DASHBOARD_PASSWORD_HASH,
    DASHBOARD_SESSION_KEY,
    DASHBOARD_TOTP_SEED,
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
)
from trading_core.settings import Settings

DASHBOARD_SECRETS: Final = (
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
    DASHBOARD_PASSWORD_HASH,
    DASHBOARD_TOTP_SEED,
    DASHBOARD_SESSION_KEY,
)

# An authenticator seed below 160 bits is weaker than RFC 4226 recommends.
MIN_SEED_BYTES: Final = 20


class DashboardSettings(Settings):
    # False only for a laptop browser that refuses Secure cookies on http://127.0.0.1.
    dashboard_cookie_secure: bool = True
    dashboard_stale_amber_s: float = Field(default=3, gt=0)
    dashboard_stale_red_s: float = Field(default=10, gt=0)
    dashboard_poll_ms: int = Field(default=500, gt=0)
    dashboard_session_idle_h: float = Field(default=24, gt=0)
    dashboard_session_max_d: float = Field(default=7, gt=0)
    control_command_ttl_s: int = Field(default=300, gt=0)
    dashboard_command_hide_s: int = Field(default=900, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.dashboard_stale_red_s <= self.dashboard_stale_amber_s:
            raise ValueError("DASHBOARD_STALE_RED_S must exceed DASHBOARD_STALE_AMBER_S")
        if self.trading_env == "live" and not self.dashboard_cookie_secure:
            raise ValueError("DASHBOARD_COOKIE_SECURE=false is allowed only in demo")
        return self


class CredentialsInvalid(EnvGuardRefused):
    """A dashboard credential file loaded but holds the wrong kind of value."""

    code: ClassVar[str] = "dashboard_credentials_invalid"
    log_message: ClassVar[str] = "dashboard credentials invalid, refusing to start"


def normalize_seed(seed: str) -> str:
    """The TOTP seed as authenticators use it: uppercase base32 without padding."""
    return seed.upper().replace("=", "")


def seed_bytes(seed: str) -> bytes:
    """Decode a normalized base32 seed; raises ``ValueError`` when it is not base32."""
    padded = seed + "=" * (-len(seed) % 8)
    try:
        return base64.b32decode(padded, casefold=False)
    except binascii.Error as exc:
        raise ValueError("not base32") from exc


def credentials_guard(settings: DashboardSettings, secrets: Mapping[str, SecretStr]) -> None:
    """Refuse to start unless the hash is argon2id and the seed is base32 of at least 160 bits."""
    try:
        is_argon2id = extract_parameters(secrets[DASHBOARD_PASSWORD_HASH].get_secret_value()).type is Type.ID
    except InvalidHashError:
        is_argon2id = False
    if not is_argon2id:
        raise CredentialsInvalid(f"{DASHBOARD_PASSWORD_HASH} is not an argon2id hash")
    try:
        decoded = seed_bytes(normalize_seed(secrets[DASHBOARD_TOTP_SEED].get_secret_value()))
    except ValueError:
        raise CredentialsInvalid(f"{DASHBOARD_TOTP_SEED} is not base32") from None
    if len(decoded) < MIN_SEED_BYTES:
        raise CredentialsInvalid(f"{DASHBOARD_TOTP_SEED} holds fewer than 160 bits")
