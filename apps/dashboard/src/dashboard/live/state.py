"""What the top bar and Overview's Activity show for live values (spec 0004 AC-9, AC-12, AC-20), as pure functions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from trading_core.control.messages import (
    ControlFormatError,
    ControlMessage,
    ControlResult,
    Fields,
    field,
    from_fields,
    result_from_fields,
)
from trading_core.recording.enums import ControlOutcome
from trading_core.snapshot.envelope import SnapshotFormatError, decode

Level = Literal["ok", "warn", "bad", "lost"]
CommandLevel = Literal["none", "pending", "ok", "warn", "bad", "lost"]
Entry = tuple[str | bytes | None, Mapping[str | bytes, str | bytes] | None]
"""One stream entry as redis-py's ``XREVRANGE`` types it: the stream id and its fields, either may be None."""

NS_PER_S: Final = 1_000_000_000


@dataclass(frozen=True)
class AgeView:
    """The data age label and its color level; ``skewed`` marks a heartbeat from the future."""

    text: str
    level: Level
    skewed: bool = False


NO_HEARTBEAT: Final = AgeView("no heartbeat", "bad")
BAD_SNAPSHOT: Final = AgeView("bad snapshot", "bad")
LINK_LOST: Final = AgeView("link lost", "lost")


def age_state(now_ns: int, ts_ns: int, amber_s: float, red_s: float) -> AgeView:
    """Neutral up to ``amber_s``, amber above it, red above ``red_s``; a negative age shows ``0s``."""
    age_ns = now_ns - ts_ns
    if age_ns < 0:
        return AgeView("0s", "ok", skewed=True)
    age_s = age_ns / NS_PER_S
    level: Level = "bad" if age_s > red_s else "warn" if age_s > amber_s else "ok"
    return AgeView(f"{int(age_s)}s", level)


def heartbeat_view(raw: str | bytes | None, now_ns: int, amber_s: float, red_s: float) -> AgeView:
    """The age view for the raw ``{env}:snapshot:heartbeat`` value (None when the key is missing)."""
    if raw is None:
        return NO_HEARTBEAT
    try:
        snapshot = decode(raw)
    except SnapshotFormatError:
        return BAD_SNAPSHOT
    return age_state(now_ns, snapshot.ts_ns, amber_s, red_s)


@dataclass(frozen=True)
class CommandView:
    """The last command's state in the top bar.

    ``final`` marks an outcome that no later result can change, so the poller stops reading
    results for it. ``invalid`` marks an entry that does not parse (logged once).
    """

    text: str
    level: CommandLevel
    final: bool = False
    invalid: bool = False


NO_COMMAND: Final = CommandView("none", "none")
COMMAND_LINK_LOST: Final = CommandView("link lost", "lost")
UNKNOWN_RESULT: Final = CommandView("unknown result", "warn", invalid=True)

_OUTCOMES: Final[dict[ControlOutcome, tuple[str, CommandLevel, bool]]] = {
    ControlOutcome.RECEIVED: ("received by trader", "pending", False),
    ControlOutcome.EXECUTED: ("confirmed", "ok", True),
    ControlOutcome.DUPLICATE: ("duplicate", "none", True),
    ControlOutcome.FAILED: ("failed", "bad", True),
    ControlOutcome.EXPIRED: ("expired", "bad", True),
}


def find_result(entries: Sequence[Entry] | None, command_id: str) -> Fields | None:
    """The newest entry for ``command_id`` in a newest first ``XREVRANGE`` reply, else None."""
    for _, fields in entries or ():
        if fields is not None and field(fields, "id") == command_id:
            return fields
    return None


def command_state(command: Fields | None, result: Fields | None, now: datetime, hide_after_s: float) -> CommandView:
    """The state of the latest command given its newest result (AC-12).

    No result yet is ``awaiting trader``, or ``not handled, expired`` once ``expires_at`` has
    passed. A final state is hidden ``hide_after_s`` after its ``outcome_at`` (or ``expires_at``).
    An entry that does not parse, or an outcome we do not know, is ``unknown result``.
    """
    if command is None:
        return NO_COMMAND
    try:
        msg = from_fields(command)
        outcome = None if result is None else result_from_fields(result)
    except ControlFormatError:
        return UNKNOWN_RESULT
    label, level, final, shown_since = _outcome(msg, outcome, now)
    if shown_since is not None and (now - shown_since).total_seconds() > hide_after_s:
        return CommandView(NO_COMMAND.text, NO_COMMAND.level, final=final)
    return CommandView(f"{msg.cmd.value} sent · {label}", level, final=final)


@dataclass(frozen=True)
class ActivityRow:
    """One command in Overview's Activity feed (AC-20); unlike the top bar, a final outcome stays.

    ``issued_at`` is None only for an entry that does not parse. ``final`` and ``invalid`` mean
    what they mean on ``CommandView``.
    """

    cmd: str
    issued_at: datetime | None
    text: str
    level: CommandLevel
    final: bool = False
    invalid: bool = False


UNKNOWN_ENTRY: Final = ActivityRow("unknown entry", None, "", "warn", invalid=True)


def activity_row(command: Fields, result: Fields | None, now: datetime) -> ActivityRow:
    """The Activity row for one command and its newest result, in the top bar's outcome words."""
    try:
        msg = from_fields(command)
    except ControlFormatError:
        return UNKNOWN_ENTRY
    try:
        outcome = None if result is None else result_from_fields(result)
    except ControlFormatError:
        return ActivityRow(msg.cmd.value, msg.issued_at, UNKNOWN_RESULT.text, "warn", invalid=True)
    label, level, final, _ = _outcome(msg, outcome, now)
    return ActivityRow(msg.cmd.value, msg.issued_at, label, level, final=final)


def _outcome(
    msg: ControlMessage, outcome: ControlResult | None, now: datetime
) -> tuple[str, CommandLevel, bool, datetime | None]:
    """Label, level, finality, and the moment the hide clock starts (None: never hidden)."""
    if outcome is None:
        if now <= msg.expires_at:
            return "awaiting trader", "pending", False, None
        # Not final: a late result from the trader still replaces it.
        return "not handled, expired", "bad", False, msg.expires_at
    label, level, final = _OUTCOMES[outcome.outcome]
    return label, level, final, outcome.outcome_at if final else None
