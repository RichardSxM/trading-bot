"""Make the dashboard login files (spec 0004 AC-19).

Run through ``scripts/dashboard-credentials.sh``, which starts the ``dashboard-credentials``
Compose service: no secrets mounted, the host secrets folder bound at ``/out``.

    python -m dashboard.credentials            # ask for a password, print everything
    python -m dashboard.credentials --write    # also write the three files into /out
    python -m dashboard.credentials --dev      # random password, printed once

Nothing secret is ever a process argument: the password comes from the terminal without echo.
"""

import argparse
import getpass
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

import pyotp
import qrcode
from argon2 import PasswordHasher

from trading_core.secrets import DASHBOARD_PASSWORD_HASH, DASHBOARD_SESSION_KEY, DASHBOARD_TOTP_SEED

MIN_PASSWORD_LENGTH: Final = 12
OUT_DIR: Final = Path("/out")
ISSUER: Final = "Kraken Futures Scalper"


class CredentialsError(Exception):
    """The command cannot finish: the password is too short or the two entries differ."""


@dataclass(frozen=True)
class Credentials:
    """The three values the dashboard login needs, plus the authenticator URI for the seed."""

    password_hash: str
    totp_seed: str
    session_key: str

    @property
    def otpauth_uri(self) -> str:
        """The ``otpauth://`` URI an authenticator app scans."""
        return pyotp.TOTP(self.totp_seed).provisioning_uri(name="dashboard", issuer_name=ISSUER)

    def files(self) -> dict[str, str]:
        """File name to content, as the dashboard reads them from ``/run/secrets``."""
        return {
            DASHBOARD_PASSWORD_HASH: self.password_hash,
            DASHBOARD_TOTP_SEED: self.totp_seed,
            DASHBOARD_SESSION_KEY: self.session_key,
        }


def make_credentials(password: str, hasher: PasswordHasher | None = None) -> Credentials:
    """Hash ``password`` with argon2id and draw a new 160 bit TOTP seed and session key."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CredentialsError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    return Credentials(
        password_hash=(hasher or PasswordHasher()).hash(password),
        # 32 base32 characters are 160 bits; pyotp returns uppercase without padding.
        totp_seed=pyotp.random_base32(32),
        session_key=secrets.token_urlsafe(48),
    )


# What a kept file means for the values just printed.
KEPT_WARNINGS: Final = {
    DASHBOARD_PASSWORD_HASH: "the password above does NOT log in; the existing hash stays in use",
    DASHBOARD_TOTP_SEED: "the QR code above is NOT in use; your authenticator keeps the existing seed",
    DASHBOARD_SESSION_KEY: "existing sessions stay valid",
}


def write_files(credentials: Credentials, out_dir: Path) -> tuple[list[str], list[str]]:
    """Write each missing file into ``out_dir`` with mode 0444; never overwrite one.

    The three files are independent, so an existing one (``dev-secrets.sh`` makes the session
    key) is kept and the others are still written. Returns (created, kept) names.
    """
    created: list[str] = []
    kept: list[str] = []
    for name, value in credentials.files().items():
        try:
            fd = os.open(out_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            kept.append(name)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(value)
        # A Compose file secret keeps the host owner, so the app user (uid 10001) must be able to read it.
        os.chmod(out_dir / name, 0o444)
        created.append(name)
    return created, kept


def ask_password() -> str:
    """Ask twice without echo; raises ``CredentialsError`` when the two differ."""
    first = getpass.getpass("dashboard password: ")
    if getpass.getpass("again: ") != first:
        raise CredentialsError("the two passwords differ")
    return first


def print_credentials(credentials: Credentials, out: TextIO) -> None:
    """Print every value, the authenticator URI, and a terminal QR code of it."""
    print(f"{DASHBOARD_PASSWORD_HASH}: {credentials.password_hash}", file=out)
    print(f"{DASHBOARD_TOTP_SEED}: {credentials.totp_seed}", file=out)
    print(f"{DASHBOARD_SESSION_KEY}: {credentials.session_key}", file=out)
    print(f"authenticator URI: {credentials.otpauth_uri}", file=out)
    print("Scan this with your authenticator app, and keep the seed in your password manager:", file=out)
    code = qrcode.QRCode(border=1)
    code.add_data(credentials.otpauth_uri)
    code.print_ascii(out=out, invert=True)


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    """Command line entry; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m dashboard.credentials", description="Make the dashboard login files."
    )
    parser.add_argument("--dev", action="store_true", help="random password, printed once (laptop only)")
    parser.add_argument("--write", action="store_true", help=f"also write the three files into {OUT_DIR}")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.dev:
            password = secrets.token_urlsafe(18)
            print(f"dev password (shown once): {password}", file=out)
        else:
            password = ask_password()
        credentials = make_credentials(password)
        print_credentials(credentials, out)
        if args.write:
            created, kept = write_files(credentials, args.out)
            for name in created:
                print(f"created  {name}", file=out)
            for name in kept:
                print(f"kept     {name} (already there: {KEPT_WARNINGS[name]})", file=out)
    except CredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
