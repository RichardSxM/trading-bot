"""covers: spec 0013 AC-19 (rule coverage): every promised alert has a rule that catches it, and
every code a rule names is logged somewhere in the repo.

The repo scan reads source files only. Inside an app image the shell producers (`docker/`) are
absent, so their codes skip there and are checked in full in CI, which runs on the checkout.
"""

import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pytest

from trading_core.alerts.promises import PENDING, PROMISES
from trading_core.alerts.rules import BY_ID, RULES, first_match
from trading_core.recording.enums import RiskEventKind

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
# Codes only a shell script logs, and the script that logs them.
SHELL_CODES = {
    "backup_failed": "docker/backup/run.sh",
    "restic_repo_missing": "docker/backup/run.sh",
    "autoheal_gave_up": "deploy/host/kfs-autoheal",
    "rollback_failed": "deploy/host/kfs-deploy",
    "interrupted": "deploy/host/kfs-deploy",
}
SOURCE_DIRS = ("packages", "apps", "docker", "deploy", "scripts")
# The alert code itself would satisfy the scan trivially.
EXCLUDED = ("/tests/", "/trading_core/alerts/", "/apps/alerter/", "/migrations/")


def _root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "uv.lock").exists():
            return parent
    raise AssertionError("repo root not found")


@cache
def _sources() -> str:
    root = _root()
    texts = []
    for name in SOURCE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        # The host scripts (``deploy/host/kfs-*``) have no extension.
        for path in [*base.rglob("*.py"), *base.rglob("*.sh"), *base.rglob("kfs-*")]:
            if not path.is_file():
                continue
            if not any(part in path.as_posix() for part in EXCLUDED):
                texts.append(path.read_text(errors="replace"))
    return "\n".join(texts)


def _logged(code: str, sources: str) -> bool:
    quoted = re.search(rf"[\"']{re.escape(code)}[\"']", sources) is not None
    # ``log LEVEL code`` (backup), ``kfs_log LEVEL code`` (host), ``record outcome`` (deploys.jsonl).
    shell = re.search(rf"\b(?:kfs_)?log [A-Z]+ {re.escape(code)}\b|\brecord {re.escape(code)}\b", sources) is not None
    # Risk events are logged as ``"code": kind.value``.
    return quoted or shell or code in {k.value for k in RiskEventKind}


@pytest.mark.parametrize("promise", PROMISES, ids=[f"{p.spec}:{p.what}" for p in PROMISES])
def test_every_promise_has_its_rule(promise):
    if promise.rule in PENDING:
        pytest.skip(f"{promise.rule}: {PENDING[promise.rule]}")
    assert promise.rule in BY_ID, f"spec {promise.spec} promised {promise.what!r}; no rule {promise.rule!r}"
    sample = promise.sample(NOW)
    if sample is not None:
        rule = first_match(sample)
        assert rule is not None, f"{promise.what!r} is caught by no rule"
        assert rule.id == promise.rule, f"{promise.what!r} is caught by {rule.id}"


def test_a_pending_rule_leaves_the_list_when_it_lands():
    assert not set(PENDING) & set(BY_ID)


CODES = sorted({code for rule in RULES for code in rule.codes})


@pytest.mark.parametrize("code", CODES)
def test_every_rule_code_is_logged_somewhere(code):
    if code in SHELL_CODES and not (_root() / SHELL_CODES[code]).exists():
        pytest.skip(f"{SHELL_CODES[code]} is not in this image; CI checks it")
    assert _logged(code, _sources()), f"a rule names {code!r} but no file in the repo logs it"
