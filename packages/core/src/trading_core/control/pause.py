"""The trader's pause state (spec 0005 AC-6): the ``{env}:trader:pause`` key and the trading state it maps to.

A pause blocks new entries; exits and protective stops still go through. The trader writes the
key when it executes ``pause`` and deletes it on ``unpause``, so a restarted trader comes back
paused. The key has no TTL and lives in Redis's AOF.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from trading_core.control.messages import format_utc
from trading_core.recording.enums import ControlSource
from trading_core.settings import TradingEnv

TradingStateName = Literal["ACTIVE", "REDUCING", "HALTED"]


class PauseFormatError(ValueError):
    """A pause key value that is not the JSON object ``encode`` writes."""


@dataclass(frozen=True)
class PauseState:
    """Who paused the trader, when (aware UTC), and through which command."""

    source: ControlSource
    paused_at: datetime
    command_id: uuid.UUID


def pause_key(env: TradingEnv) -> str:
    """The pause key, e.g. ``demo:trader:pause``."""
    return f"{env}:trader:pause"


def encode(state: PauseState) -> str:
    """The key value for ``state``: a JSON object of strings."""
    return json.dumps(
        {"source": state.source.value, "paused_at": format_utc(state.paused_at), "command_id": str(state.command_id)},
        separators=(",", ":"),
    )


def decode(raw: str | bytes) -> PauseState:
    """Parse a pause key value; raises ``PauseFormatError`` for anything ``encode`` would not write."""
    try:
        value = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        raise PauseFormatError("pause key is not JSON") from None
    if not isinstance(value, dict):
        raise PauseFormatError("pause key is not a JSON object")
    try:
        source = ControlSource(value["source"])
        paused_at = datetime.fromisoformat(value["paused_at"])
        command_id = uuid.UUID(value["command_id"])
    except KeyError, TypeError, ValueError:
        raise PauseFormatError("pause key lacks a valid source, paused_at, or command_id") from None
    if paused_at.tzinfo is None:
        raise PauseFormatError("pause key paused_at has no timezone")
    return PauseState(source, paused_at.astimezone(UTC), command_id)


def target_trading_state(paused: bool) -> TradingStateName:
    """The risk engine state for a trader that is (or is not) paused: ``REDUCING`` or ``ACTIVE``.

    Feature 9 extends this with its stop state (``HALTED``).
    """
    return "REDUCING" if paused else "ACTIVE"
