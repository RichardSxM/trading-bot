"""covers: spec 0002 AC-13 (role setup refuses to run without every role password)."""

import json
import os
import subprocess
import sys

import pytest

from trading_core.db_roles import APP_ROLES, role_passwords
from trading_core.settings import RoleSettings


def test_role_passwords_cover_every_app_role():
    roles = RoleSettings(postgres_trader_password="t", postgres_dashboard_password="d", postgres_backup_password="b")

    assert role_passwords(roles) == {"trader_app": "t", "dashboard_app": "d", "backup_app": "b"}
    assert set(role_passwords(roles)) == set(APP_ROLES)


@pytest.mark.parametrize("dashboard_password", [None, ""])
def test_role_setup_exits_1_with_one_json_error_when_a_password_is_missing(dashboard_password):
    env = {
        "PATH": os.environ["PATH"],
        "TRADING_ENV": "demo",
        "PROCESS_NAME": "migrate",
        "POSTGRES_TRADER_PASSWORD": "t",
        "POSTGRES_BACKUP_PASSWORD": "b",
    }
    if dashboard_password is not None:
        env["POSTGRES_DASHBOARD_PASSWORD"] = dashboard_password

    result = subprocess.run(
        [sys.executable, "-m", "trading_core.db_roles"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert (entry["level"], entry["code"], entry["process"]) == ("ERROR", "role_passwords_missing", "migrate")
    assert "postgres_dashboard_password" in lines[0]
    assert "t" not in {str(error.get("input")) for error in entry["errors"]}
