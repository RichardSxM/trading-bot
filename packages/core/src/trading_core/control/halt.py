"""The trader's halt state (spec 0010 AC-5): the ``{env}:trader:halt`` key.

A halt blocks new entries like a pause, and it also flattens. The trader writes the key when it
halts (a ``stop`` command or a loss limit) and deletes it only on a successful ``resume``, so a
restarted trader comes back halted. The key has no TTL and lives in Redis's AOF.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from trading_core.control.messages import format_utc
from trading_core.settings import TradingEnv


class HaltFormatError(ValueError):
    """A halt key value that is not the JSON object ``encode`` writes."""


class HaltReason(StrEnum):
    """Why the trader halted. ``unknown`` is used only in memory, for an unreadable key at start."""

    MANUAL_STOP = "manual_stop"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    TOTAL_LOSS_LIMIT = "total_loss_limit"
    RECONCILE_MISMATCH = "reconcile_mismatch"  # a reconciliation finding it cannot explain (spec 0023)
    UNKNOWN = "unknown"


class HaltSource(StrEnum):
    """Who halted the trader: a command's source, or ``trader`` for an automatic halt."""

    DASHBOARD = "dashboard"
    CLI = "cli"
    DEPLOY = "deploy"
    TRADER = "trader"


@dataclass(frozen=True)
class HaltState:
    """Why, by whom, and when (aware UTC) the trader halted; ``halt_id`` names the halt in events and tags."""

    reason: HaltReason
    source: HaltSource
    halted_at: datetime
    halt_id: uuid.UUID


def halt_key(env: TradingEnv) -> str:
    """The halt key, e.g. ``demo:trader:halt``."""
    return f"{env}:trader:halt"


def encode(state: HaltState) -> str:
    """The key value for ``state``: a JSON object of strings."""
    return json.dumps(
        {
            "reason": state.reason.value,
            "source": state.source.value,
            "halted_at": format_utc(state.halted_at),
            "halt_id": str(state.halt_id),
        },
        separators=(",", ":"),
    )


def decode(raw: str | bytes) -> HaltState:
    """Parse a halt key value; raises ``HaltFormatError`` for anything ``encode`` would not write."""
    try:
        value = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        raise HaltFormatError("halt key is not JSON") from None
    if not isinstance(value, dict):
        raise HaltFormatError("halt key is not a JSON object")
    try:
        reason = HaltReason(value["reason"])
        source = HaltSource(value["source"])
        halted_at = datetime.fromisoformat(value["halted_at"])
        halt_id = uuid.UUID(value["halt_id"])
    except KeyError, TypeError, ValueError:
        raise HaltFormatError("halt key lacks a valid reason, source, halted_at, or halt_id") from None
    if reason is HaltReason.UNKNOWN:
        raise HaltFormatError("halt key reason unknown is never written")
    if halted_at.tzinfo is None:
        raise HaltFormatError("halt key halted_at has no timezone")
    return HaltState(reason, source, halted_at.astimezone(UTC), halt_id)
