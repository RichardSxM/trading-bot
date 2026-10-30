"""covers: spec 0003 AC-6 (no secret reaches any output, NautilusTrader's included), AC-7, AC-8.

Runs canary_main.py as a subprocess with freshly generated canary secrets and captures every
byte it writes to stdout and stderr. Run in the dev image:
``docker compose run --rm --no-deps -T trader pytest -q -m canary``.
"""

import base64
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.canary

HARNESS = Path(__file__).resolve().parent / "canary_main.py"
NAMES = ("postgres_password", "redis_password", "kraken_api_key", "kraken_api_secret")


def _canary() -> str:
    # Base64 like a real Kraken secret, so '+', '/' and '=' exercise the URL quoted form too.
    while True:
        value = base64.b64encode(os.urandom(48)).decode()
        if "+" in value or "/" in value:
            return value


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    folder = tmp_path_factory.mktemp("canary-secrets")
    canaries = {name: _canary() for name in NAMES}
    for name, value in canaries.items():
        (folder / name).write_text(value)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(folder),
        "TRADING_ENV": "live",
        # A live node runs only in drill mode (spec 0023 AC-22).
        "LIVE_MODE": "drill",
        "PROCESS_NAME": "trader",
        "LOG_LEVEL": "DEBUG",
        "SECRETS_DIR_IN_CONTAINER": str(folder),
        "KRAKEN_KEY_ROTATED_ON": datetime.now(UTC).date().isoformat(),
        "RISK_TOTAL_LOSS_SINCE": datetime.now(UTC).date().isoformat(),
        "GIT_COMMIT": "canary",
        "HEARTBEAT_FILE": str(folder / "alive"),
    }
    result = subprocess.run(
        [sys.executable, str(HARNESS)],
        env=env,
        capture_output=True,
        timeout=180,
        check=False,
    )
    return canaries, result


def test_the_key_check_fails_closed(run):
    _, result = run

    assert result.returncode == 1, result.stdout.decode(errors="replace")[-3000:]
    assert b'"code": "kraken_auth_failed"' in result.stdout


def test_every_secret_was_loaded(run):
    _, result = run

    for name in NAMES:
        assert f'"secret": "{name}"'.encode() in result.stdout


def test_the_values_logged_on_purpose_came_out_redacted(run):
    _, result = run

    for name in NAMES:
        # Message, field, and exception text: three lines per secret, plus the URL line.
        assert result.stdout.count(f"[REDACTED:{name}]".encode()) >= 3, f"{name} probe missing"
    assert b":[REDACTED:postgres_password]@" in result.stdout


def test_no_canary_value_appears_in_any_output(run):
    canaries, result = run
    output = result.stdout + result.stderr

    for name, value in canaries.items():
        for form in {value, quote(value, safe="")}:
            assert form.encode() not in output, f"{name} leaked"
