"""What the top bar shows for live values (spec 0004 AC-9, AC-12), as pure functions of their inputs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from trading_core.control.messages import ControlFormatError, Fields, field, from_fields, result_from_fields
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
    prefix = f"{msg.cmd.value} sent · "
    if outcome is None:
        if now <= msg.expires_at:
            return CommandView(prefix + "awaiting trader", "pending")
        # Not final: a late result from the trader still replaces it.
        shown_since, view = msg.expires_at, CommandView(prefix + "not handled, expired", "bad")
    else:
        label, level, final = _OUTCOMES[outcome.outcome]
        if not final:
            return CommandView(prefix + label, level)
        shown_since, view = outcome.outcome_at, CommandView(prefix + label, level, final=True)
    if (now - shown_since).total_seconds() > hide_after_s:
        return CommandView(NO_COMMAND.text, NO_COMMAND.level, final=view.final)
    return view
