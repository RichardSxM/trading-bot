"""The alert command and result formats on the Redis streams (spec 0013, API surface).

The dashboard appends ``alerts_mute``, ``alerts_unmute``, and ``alerts_test`` to
``{env}:control:alerts``; the alerter appends each outcome to ``{env}:control:alerts:results``.
The fields follow ``{env}:control`` (``trading_core.control.messages``), plus ``mute_s`` on a mute
and ``until`` on an executed mute's result. The trader never reads these streams.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from trading_core.control.messages import (
    SCHEMA_VERSION,
    ControlFormatError,
    Fields,
    _check_version,
    _strings,
    _utc,
    _uuid,
    field,
    format_utc,
)
from trading_core.recording.enums import AlertCmd, ControlOutcome, ControlSource
from trading_core.settings import TradingEnv

# The mute lengths the dashboard offers: 1 h, 4 h, 12 h (AC-12).
MUTE_SECONDS: Final = frozenset({3600, 14_400, 43_200})


@dataclass(frozen=True)
class AlertCommand:
    """One command on ``{env}:control:alerts``; ``mute_s`` is set only on ``alerts_mute``."""

    id: uuid.UUID
    cmd: AlertCmd
    issued_at: datetime
    expires_at: datetime
    source: ControlSource = ControlSource.DASHBOARD
    mute_s: int | None = None


@dataclass(frozen=True)
class AlertResult:
    """One outcome on ``{env}:control:alerts:results`` for the command with the same ``id``.

    ``cmd`` repeats the command's, so a reader can find the newest mute or unmute result alone;
    None only for an entry too broken to name one. ``until`` is when an executed mute ends; None
    for anything else (an unmute ends the mute).
    """

    id: uuid.UUID
    outcome: ControlOutcome
    outcome_at: datetime
    cmd: AlertCmd | None = None
    reason: str | None = None
    until: datetime | None = None


def alerts_stream(env: TradingEnv) -> str:
    """The alert command stream, e.g. ``demo:control:alerts``."""
    return f"{env}:control:alerts"


def alerts_results_stream(env: TradingEnv) -> str:
    """The alert result stream, e.g. ``demo:control:alerts:results``."""
    return f"{env}:control:alerts:results"


def alert_command(cmd: AlertCmd, now: datetime, ttl_s: int, mute_s: int | None = None) -> AlertCommand:
    """A new ``cmd`` from the dashboard with a fresh UUID4, issued at ``now`` and expiring ``ttl_s`` later.

    Raises ``ValueError`` for a naive ``now``, a mute without one of ``MUTE_SECONDS``, or a
    ``mute_s`` on another command.
    """
    if now.tzinfo is None:
        raise ValueError("naive datetime; control timestamps must be timezone aware UTC")
    _check_mute(cmd, mute_s, ValueError)
    issued_at = now.astimezone(UTC)
    return AlertCommand(uuid.uuid4(), cmd, issued_at, issued_at + timedelta(seconds=ttl_s), mute_s=mute_s)


def command_to_fields(msg: AlertCommand) -> dict[str, str]:
    """The stream fields for ``msg``."""
    fields = {
        "id": str(msg.id),
        "cmd": msg.cmd.value,
        "issued_at": format_utc(msg.issued_at),
        "expires_at": format_utc(msg.expires_at),
        "schema_version": SCHEMA_VERSION,
        "source": msg.source.value,
    }
    if msg.mute_s is not None:
        fields["mute_s"] = str(msg.mute_s)
    return fields


def command_from_fields(fields: Fields) -> AlertCommand:
    """Parse a ``{env}:control:alerts`` entry; raises ``ControlFormatError`` for a bad one.

    A mute whose ``mute_s`` is not one of ``MUTE_SECONDS`` is a bad entry (answered ``failed
    invalid``), and so is a ``mute_s`` on another command.
    """
    values = _strings(fields, ("id", "cmd", "issued_at", "expires_at", "schema_version"))
    _check_version(values)
    try:
        cmd = AlertCmd(values["cmd"])
    except ValueError:
        raise ControlFormatError(f"unknown alert cmd {values['cmd']!r}") from None
    source_text = field(fields, "source")
    try:
        source = ControlSource.DASHBOARD if source_text is None else ControlSource(source_text)
    except ValueError:
        raise ControlFormatError(f"unknown control source {source_text!r}") from None
    mute_text = field(fields, "mute_s")
    try:
        mute_s = None if mute_text is None else int(mute_text)
    except ValueError:
        raise ControlFormatError(f"mute_s {mute_text!r} is not a number") from None
    _check_mute(cmd, mute_s, ControlFormatError)
    return AlertCommand(
        _uuid(values["id"]),
        cmd,
        _utc(values["issued_at"], "issued_at"),
        _utc(values["expires_at"], "expires_at"),
        source,
        mute_s,
    )


def result_to_fields(result: AlertResult) -> dict[str, str]:
    """The stream fields for ``result``; ``cmd``, ``reason``, and ``until`` only when set."""
    fields = {
        "id": str(result.id),
        "outcome": result.outcome.value,
        "outcome_at": format_utc(result.outcome_at),
        "schema_version": SCHEMA_VERSION,
    }
    if result.cmd is not None:
        fields["cmd"] = result.cmd.value
    if result.reason is not None:
        fields["reason"] = result.reason
    if result.until is not None:
        fields["until"] = format_utc(result.until)
    return fields


def result_from_fields(fields: Fields) -> AlertResult:
    """Parse a ``{env}:control:alerts:results`` entry; raises ``ControlFormatError`` for a bad one."""
    values = _strings(fields, ("id", "outcome", "outcome_at", "schema_version"))
    _check_version(values)
    cmd_text = field(fields, "cmd")
    try:
        cmd = None if cmd_text is None else AlertCmd(cmd_text)
        outcome = ControlOutcome(values["outcome"])
    except ValueError:
        raise ControlFormatError(f"unknown alert result {cmd_text!r} {values['outcome']!r}") from None
    until_text = field(fields, "until")
    return AlertResult(
        _uuid(values["id"]),
        outcome,
        _utc(values["outcome_at"], "outcome_at"),
        cmd,
        field(fields, "reason"),
        None if until_text is None else _utc(until_text, "until"),
    )


def _check_mute(cmd: AlertCmd, mute_s: int | None, error: type[ValueError]) -> None:
    if cmd is AlertCmd.MUTE and mute_s not in MUTE_SECONDS:
        raise error(f"mute_s must be one of {sorted(MUTE_SECONDS)}, got {mute_s!r}")
    if cmd is not AlertCmd.MUTE and mute_s is not None:
        raise error(f"{cmd.value} takes no mute_s")
