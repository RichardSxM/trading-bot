"""covers: spec 0002 AC-13 and spec 0003 AC-2 (role setup refuses to run without every password file)."""

import json
import os
import subprocess
import sys

import pytest
from pydantic import SecretStr

from trading_core.db_roles import APP_ROLES, MIGRATE_SECRETS, role_passwords

PASSWORDS = {name: f"{name}-0123456789abcdef" for name in MIGRATE_SECRETS}


def test_role_passwords_cover_every_app_role():
    secrets = {name: SecretStr(value) for name, value in PASSWORDS.items()}

    assert role_passwords(secrets) == {
        "trader_app": PASSWORDS["postgres_trader_password"],
        "dashboard_app": PASSWORDS["postgres_dashboard_password"],
        "backup_app": PASSWORDS["postgres_backup_password"],
    }
    assert set(role_passwords(secrets)) == set(APP_ROLES)


def test_migrate_reads_the_owner_and_three_role_files():
    assert MIGRATE_SECRETS == (
        "postgres_password",
        "postgres_trader_password",
        "postgres_dashboard_password",
        "postgres_backup_password",
    )


@pytest.mark.parametrize(("content", "reason"), [(None, "missing"), ("", "empty")])
def test_role_setup_exits_1_with_one_json_error_when_a_password_file_is_unusable(tmp_path, content, reason):
    for name, value in PASSWORDS.items():
        if name != "postgres_dashboard_password":
            (tmp_path / name).write_text(value)
    if content is not None:
        (tmp_path / "postgres_dashboard_password").write_text(content)
    env = {
        "PATH": os.environ["PATH"],
        "TRADING_ENV": "demo",
        "PROCESS_NAME": "migrate",
        "SECRETS_DIR_IN_CONTAINER": str(tmp_path),
    }

    result = subprocess.run(
        [sys.executable, "-m", "trading_core.db_roles"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert [line["msg"] for line in lines] == [
        "secret loaded",
        "secret loaded",
        "secret unavailable, refusing to start",
    ]
    assert (lines[-1]["level"], lines[-1]["code"], lines[-1]["process"]) == (
        "CRITICAL",
        "secret_unavailable",
        "migrate",
    )
    assert (lines[-1]["secret"], lines[-1]["reason"]) == ("postgres_dashboard_password", reason)
    for value in PASSWORDS.values():
        assert value not in result.stdout
