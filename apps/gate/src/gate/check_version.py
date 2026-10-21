"""``gate check-version`` (spec 0015 AC-26): does the version a new trader image would start hold its paper pass?

``kfs-deploy`` pipes ``python -m trader.version`` from the new image into this command before it drains
the running trader, so a missing promotion refuses the deploy instead of failing its health gate. The
version is recorded the same way the trader records it at start (``ensure_strategy_version`` with the
image's commit), so the id printed here is the one ``gate promote --manual --version`` takes, and that row
stays even when the deploy is refused.
"""

import json
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import Stage, StrategyKind
from trading_core.recording.errors import PromotionRequiredError
from trading_core.recording.rows import JsonObject
from trading_core.versioning import FrozenHashes

PASS: Final = "pass"
MISSING: Final = "missing"


class CheckVersionRefused(Exception):
    """The input or the env rules out a check (exit 1): bad JSON, a missing field, or not demo."""


@dataclass(frozen=True)
class VersionCheck:
    """What ``gate check-version`` prints: the version, its frozen hash, and its paper verdict."""

    version_id: UUID
    frozen_hash: str
    verdict: str

    @property
    def passed(self) -> bool:
        """True when the latest ``candidate → paper`` promotion of the frozen hash is a pass."""
        return self.verdict == PASS

    def to_json(self) -> JsonObject:
        """The printed JSON (``stage_needed`` is always paper until feature 19 brings live)."""
        return {
            "version_id": str(self.version_id),
            "frozen_hash": self.frozen_hash,
            "stage_needed": Stage.PAPER.value,
            "verdict": self.verdict,
        }


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CheckVersionRefused(f"trader.version output has no {key}")
    return value


def parse_version(text: str) -> tuple[str, StrategyKind, JsonObject, str, FrozenHashes]:
    """The strategy, kind, config, commit, and hashes from ``trader.version`` JSON, or ``CheckVersionRefused``."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CheckVersionRefused(f"trader.version output is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CheckVersionRefused("trader.version output is not a JSON object")
    git_commit = _text(payload, "git_commit")
    if git_commit == "unknown":
        raise CheckVersionRefused("trader.version git_commit is unknown (build the image with GIT_COMMIT)")
    frozen = FrozenHashes(code_hash=_text(payload, "code_hash"), frozen_hash=_text(payload, "frozen_hash"))
    try:
        kind = StrategyKind(_text(payload, "kind"))
    except ValueError as exc:
        raise CheckVersionRefused(f"trader.version kind is not a strategy kind: {payload['kind']!r}") from exc
    config = payload.get("config")
    if not isinstance(config, dict):
        raise CheckVersionRefused("trader.version output has no config object")
    return _text(payload, "strategy"), kind, config, git_commit, frozen


def check_version(session: Session, text: str, trading_env: str) -> VersionCheck:
    """Record the version ``text`` describes and read its paper verdict; the caller commits.

    Demo only: the trader paper trades today, and the live deploy check arrives with feature 19.
    """
    if trading_env != "demo":
        raise CheckVersionRefused(f"TRADING_ENV is {trading_env}; the live deploy check arrives with feature 19")
    strategy, kind, config, git_commit, frozen = parse_version(text)
    version_id = repository.ensure_strategy_version(session, strategy, kind, git_commit, config, frozen)
    try:
        repository.require_paper_promotion(session, version_id)
    except PromotionRequiredError:
        latest = repository.latest_promotion(session, frozen.frozen_hash, Stage.PAPER)
        verdict = MISSING if latest is None else str(latest.verdict)
        return VersionCheck(version_id, frozen.frozen_hash, verdict)
    return VersionCheck(version_id, frozen.frozen_hash, PASS)
