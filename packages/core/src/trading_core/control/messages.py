"""The control command and result formats on the Redis streams (spec 0004, data model).

The dashboard (and a deploy, spec 0005) appends commands to ``{env}:control``; the trader
appends what happened to each one to ``{env}:control:results``. Every field is a string, as Redis stream
fields are. Timestamps are ISO 8601 UTC with a ``Z`` suffix.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource
from trading_core.settings import TradingEnv

SCHEMA_VERSION: Final = "1"

Fields = Mapping[str, str] | Mapping[bytes, bytes] | Mapping[str | bytes, str | bytes]


class ControlFormatError(ValueError):
    """A stream entry that lacks a field, has a bad value, or an unknown ``schema_version``."""


@dataclass(frozen=True)
class ControlMessage:
    """One command on ``{env}:control``; ``issued_at`` and ``expires_at`` are aware UTC datetimes.

    ``source`` says who sent it; an entry without the field came from the dashboard.
    """

    id: uuid.UUID
    cmd: ControlCmd
    issued_at: datetime
    expires_at: datetime
    source: ControlSource = ControlSource.DASHBOARD


@dataclass(frozen=True)
class ControlResult:
    """One outcome on ``{env}:control:results`` for the command with the same ``id``.

    ``reason`` explains a ``failed`` outcome (e.g. ``not_implemented``); it is None otherwise.
    """

    id: uuid.UUID
    outcome: ControlOutcome
    outcome_at: datetime
    reason: str | None = None


def control_stream(env: TradingEnv) -> str:
    """The command stream, e.g. ``demo:control`` (spec 0001)."""
    return f"{env}:control"


def results_stream(env: TradingEnv) -> str:
    """The result stream, e.g. ``demo:control:results``."""
    return f"{env}:control:results"


def format_utc(moment: datetime) -> str:
    """``moment`` as ISO 8601 UTC with a ``Z`` suffix; raises ``ValueError`` for a naive datetime."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; control timestamps must be timezone aware UTC")
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def command(
    cmd: ControlCmd, now: datetime, ttl_s: int, source: ControlSource = ControlSource.DASHBOARD
) -> ControlMessage:
    """A new ``cmd`` with a fresh UUID4, issued at ``now`` and expiring ``ttl_s`` later."""
    if now.tzinfo is None:
        raise ValueError("naive datetime; control timestamps must be timezone aware UTC")
    issued_at = now.astimezone(UTC)
    return ControlMessage(uuid.uuid4(), cmd, issued_at, issued_at + timedelta(seconds=ttl_s), source)


def stop_command(now: datetime, ttl_s: int) -> ControlMessage:
    """A new ``stop`` command with a fresh UUID4, issued at ``now`` and expiring ``ttl_s`` later."""
    return command(ControlCmd.STOP, now, ttl_s)


def pause_command(now: datetime, ttl_s: int, source: ControlSource) -> ControlMessage:
    """A new ``pause`` command from ``source`` (spec 0005 AC-6)."""
    return command(ControlCmd.PAUSE, now, ttl_s, source)


def unpause_command(now: datetime, ttl_s: int, source: ControlSource) -> ControlMessage:
    """A new ``unpause`` command from ``source`` (spec 0005 AC-6)."""
    return command(ControlCmd.UNPAUSE, now, ttl_s, source)


def to_fields(msg: ControlMessage) -> dict[str, str]:
    """The stream fields for ``msg``."""
    return {
        "id": str(msg.id),
        "cmd": msg.cmd.value,
        "issued_at": format_utc(msg.issued_at),
        "expires_at": format_utc(msg.expires_at),
        "schema_version": SCHEMA_VERSION,
        "source": msg.source.value,
    }


def result_to_fields(result: ControlResult) -> dict[str, str]:
    """The stream fields for ``result``; ``reason`` only when set."""
    fields = {
        "id": str(result.id),
        "outcome": result.outcome.value,
        "outcome_at": format_utc(result.outcome_at),
        "schema_version": SCHEMA_VERSION,
    }
    if result.reason is not None:
        fields["reason"] = result.reason
    return fields


def from_fields(fields: Fields) -> ControlMessage:
    """Parse a ``{env}:control`` entry; raises ``ControlFormatError`` for anything but version 1."""
    values = _strings(fields, ("id", "cmd", "issued_at", "expires_at", "schema_version"))
    _check_version(values)
    try:
        cmd = ControlCmd(values["cmd"])
    except ValueError:
        raise ControlFormatError(f"unknown control cmd {values['cmd']!r}") from None
    source_text = field(fields, "source")
    try:
        source = ControlSource.DASHBOARD if source_text is None else ControlSource(source_text)
    except ValueError:
        raise ControlFormatError(f"unknown control source {source_text!r}") from None
    return ControlMessage(
        _uuid(values["id"]),
        cmd,
        _utc(values["issued_at"], "issued_at"),
        _utc(values["expires_at"], "expires_at"),
        source,
    )


def result_from_fields(fields: Fields) -> ControlResult:
    """Parse a ``{env}:control:results`` entry; raises ``ControlFormatError`` for anything but version 1."""
    values = _strings(fields, ("id", "outcome", "outcome_at", "schema_version"))
    _check_version(values)
    try:
        outcome = ControlOutcome(values["outcome"])
    except ValueError:
        raise ControlFormatError(f"unknown control outcome {values['outcome']!r}") from None
    return ControlResult(
        _uuid(values["id"]), outcome, _utc(values["outcome_at"], "outcome_at"), field(fields, "reason")
    )


def field(fields: Fields, name: str) -> str | None:
    """One field of a raw entry as text (keys and values may be bytes), or None when absent or not UTF-8."""
    raw: object = None
    for key, value in fields.items():
        if (key.decode("utf-8", "replace") if isinstance(key, bytes) else key) == name:
            raw = value
            break
    if isinstance(raw, bytes):
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return None
    return raw if isinstance(raw, str) else None


def _strings(fields: Fields, names: tuple[str, ...]) -> dict[str, str]:
    values = {name: field(fields, name) for name in names}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ControlFormatError(f"control entry lacks {missing}")
    return {name: value for name, value in values.items() if value is not None}


def _check_version(values: Mapping[str, str]) -> None:
    if values["schema_version"] != SCHEMA_VERSION:
        raise ControlFormatError(f"unknown control schema_version {values['schema_version']!r}")


def _uuid(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(text)
    except ValueError:
        raise ControlFormatError(f"control id {text!r} is not a UUID") from None


def _utc(text: str, name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ControlFormatError(f"control {name} {text!r} is not ISO 8601") from None
    if moment.tzinfo is None:
        raise ControlFormatError(f"control {name} {text!r} has no timezone")
    return moment.astimezone(UTC)
