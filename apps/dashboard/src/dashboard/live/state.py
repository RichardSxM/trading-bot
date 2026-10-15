"""What the top bar, its alert bar, and Overview's Activity show for live values, as pure functions.

Spec 0004 AC-9, AC-10, AC-12, AC-20, and AC-21; spec 0005 AC-13 (the pause in the top bar);
spec 0006 AC-18 (why entries are blocked).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
from trading_core.strategy.blocks import PLAIN_WORDS, BlockReason

Level = Literal["ok", "warn", "bad", "lost"]
CommandLevel = Literal["none", "pending", "ok", "warn", "bad", "lost"]
CommandStatus = Literal[
    "none", "lost", "unknown", "awaiting", "unhandled", "received", "executed", "duplicate", "failed", "expired"
]
"""Which state the command is in, independent of its words: the alert bar keys on it."""
AlertKind = Literal[
    "stop_unhandled", "stop_failed", "stop_late", "stop_unknown", "trader_silent", "stop_unacknowledged", "no_data"
]
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
# The dashboard cannot read Redis. "no data", not "link lost": the browser's link may be fine (AC-10).
NO_DATA: Final = AgeView("no data", "lost")


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
    results for it. ``invalid`` marks an entry that does not parse (logged once). ``since`` is
    the moment the shown state began (issued, answered, or expired) and ``age`` how long ago
    that was, as ``12s ago`` (AC-12: a value is never shown without its age). ``cmd`` is the
    command's name, None when nothing parses.
    """

    text: str
    level: CommandLevel
    final: bool = False
    invalid: bool = False
    status: CommandStatus = "none"
    since: datetime | None = None
    age: str | None = None
    expires_at: datetime | None = None
    cmd: str | None = None


NO_COMMAND: Final = CommandView("none", "none")
COMMAND_NO_DATA: Final = CommandView("no data", "lost", status="lost")
UNKNOWN_RESULT: Final = CommandView("unknown result", "warn", invalid=True, status="unknown")

_OUTCOMES: Final[dict[ControlOutcome, tuple[str, CommandLevel, bool, CommandStatus]]] = {
    ControlOutcome.RECEIVED: ("received by trader", "pending", False, "received"),
    ControlOutcome.EXECUTED: ("confirmed", "ok", True, "executed"),
    ControlOutcome.DUPLICATE: ("duplicate", "none", True, "duplicate"),
    ControlOutcome.FAILED: ("failed", "bad", True, "failed"),
    ControlOutcome.EXPIRED: ("expired", "bad", True, "expired"),
}


# A ``failed`` result's reason in plain words (spec 0010 AC-18); an unknown reason shows its code.
FAILURE_WORDS: Final = {
    "not_flat": "not flat yet",
    "total_loss_limit": "total loss limit",
    "loss_unknown": "loss not known yet",
    "halted_during_resume": "a loss limit halted it again",
    "flatten_timeout": "flatten timeout",
    "halt_key_write_failed": "halt not saved, a restart would run",
    "halt_key_delete_failed": "halt not cleared",
    "pause_key_write_failed": "pause not saved",
    "pause_key_delete_failed": "pause not cleared",
    "not_implemented": "not implemented",
    "error": "error in the trader",
}


def failure_words(reason: str | None) -> str | None:
    """The plain words for a ``failed`` reason, the code itself when unknown, None without one."""
    if not reason:
        return None
    return FAILURE_WORDS.get(reason, reason)


def ago(seconds: float) -> str:
    """A short elapsed time: ``12s ago`` under a minute, ``3m ago`` under an hour, else ``2h ago``.

    A negative span (clock skew) reads ``0s ago``.
    """
    whole = max(int(seconds), 0)
    if whole < 60:
        return f"{whole}s ago"
    if whole < 3600:
        return f"{whole // 60}m ago"
    return f"{whole // 3600}h ago"


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
    An entry that does not parse, or an outcome we do not know, is ``unknown result``. Every shown
    state carries its age: since the command was issued, answered, or expired.
    """
    if command is None:
        return NO_COMMAND
    try:
        msg = from_fields(command)
        outcome = None if result is None else result_from_fields(result)
    except ControlFormatError:
        return UNKNOWN_RESULT
    state = _outcome(msg, outcome, now)
    if state.hide_from is not None and (now - state.hide_from).total_seconds() > hide_after_s:
        return CommandView(NO_COMMAND.text, NO_COMMAND.level, final=state.final)
    return CommandView(
        f"{msg.cmd.value} sent · {state.label}",
        state.level,
        final=state.final,
        status=state.status,
        since=state.since,
        age=ago((now - state.since).total_seconds()),
        expires_at=msg.expires_at,
        cmd=msg.cmd.value,
    )


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
    state = _outcome(msg, outcome, now)
    return ActivityRow(msg.cmd.value, msg.issued_at, state.label, state.level, final=state.final)


@dataclass(frozen=True)
class ActivityGroup:
    """A run of consecutive Activity rows that read the same (AC-20): one line with a count.

    ``row`` is the newest of the run and ``oldest`` the ``issued_at`` of its oldest row; for a
    single row ``count`` is 1 and ``oldest`` equals ``row.issued_at``.
    """

    row: ActivityRow
    count: int
    oldest: datetime | None


def collapse(rows: Sequence[ActivityRow]) -> list[ActivityGroup]:
    """Merge newest first ``rows`` into runs of the same command, outcome words, and level.

    Order is kept and nothing is dropped: the counts add up to ``len(rows)``.
    """
    groups: list[ActivityGroup] = []
    for row in rows:
        last = groups[-1] if groups else None
        if last is not None and _same(last.row, row):
            groups[-1] = ActivityGroup(last.row, last.count + 1, row.issued_at)
        else:
            groups.append(ActivityGroup(row, 1, row.issued_at))
    return groups


def _same(a: ActivityRow, b: ActivityRow) -> bool:
    return (a.cmd, a.text, a.level, a.invalid) == (b.cmd, b.text, b.level, b.invalid)


@dataclass(frozen=True)
class AlertView:
    """The one alert the alert bar shows (AC-21): what went wrong, how bad, and since when.

    ``at`` is the moment it began (the stop's expiry, answer, or sending), None when no single
    moment applies; ``until`` is when an unanswered stop expires. The words live in
    ``partials/topbar_alert.html``, so the times go through ``ts``.
    """

    kind: AlertKind
    level: Literal["warn", "bad"]
    at: datetime | None = None
    until: datetime | None = None


_STOP_ALERTS: Final[dict[CommandStatus, tuple[AlertKind, Literal["warn", "bad"]]]] = {
    "unhandled": ("stop_unhandled", "bad"),
    "failed": ("stop_failed", "bad"),
    "expired": ("stop_late", "bad"),
    "unknown": ("stop_unknown", "warn"),
}


def alert_state(age: AgeView, command: CommandView, now: datetime, ack_warn_s: float) -> AlertView | None:
    """The most severe thing wrong right now, or None when all is well (AC-21).

    A stop that did not take effect comes first, then a silent trader, then a stop that a running
    trader has left unanswered for more than ``ack_warn_s``, then a dashboard that cannot read
    Redis. A stop's alert follows the top bar, so it hides when the command does. Only a stop
    (or an entry too broken to name) raises a stop alert; a pause or unpause shows its outcome in
    the top bar alone.
    """
    is_stop = command.cmd in (None, "stop")
    stop = _STOP_ALERTS.get(command.status) if is_stop else None
    if stop is not None:
        return AlertView(stop[0], stop[1], command.since)
    if age.level == "bad":
        return AlertView("trader_silent", "bad")
    if (
        is_stop
        and command.status == "awaiting"
        and command.since is not None
        and (now - command.since).total_seconds() > ack_warn_s
    ):
        return AlertView("stop_unacknowledged", "warn", command.since, command.expires_at)
    if age.level == "lost":
        return AlertView("no_data", "warn")
    return None


@dataclass(frozen=True)
class _State:
    label: str
    level: CommandLevel
    final: bool
    status: CommandStatus
    since: datetime
    hide_from: datetime | None
    """When the hide clock starts; None: never hidden."""


def _outcome(msg: ControlMessage, outcome: ControlResult | None, now: datetime) -> _State:
    """The words, level, finality, and timing for a command and its newest result."""
    if outcome is None:
        if now <= msg.expires_at:
            return _State("awaiting trader", "pending", False, "awaiting", msg.issued_at, None)
        # Not final: a late result from the trader still replaces it.
        return _State("not handled, expired", "bad", False, "unhandled", msg.expires_at, msg.expires_at)
    label, level, final, status = _OUTCOMES[outcome.outcome]
    words = failure_words(outcome.reason) if outcome.outcome is ControlOutcome.FAILED else None
    if words is not None:
        label = f"{label} · {words}"
    return _State(label, level, final, status, outcome.outcome_at, outcome.outcome_at if final else None)


@dataclass(frozen=True)
class PauseView:
    """The pause cue in the top bar (spec 0005 AC-13): empty text while the trader runs."""

    text: str
    level: Level


RUNNING: Final = PauseView("", "ok")
PAUSE_NO_DATA: Final = PauseView("no data", "lost")
_PAUSED_BY: Final = {"dashboard": "paused · by you", "deploy": "paused · by deploy", "cli": "paused · by command line"}


# A halt's reason in plain words (spec 0010 AC-17).
HALT_WORDS: Final = {
    "manual_stop": "manual stop",
    "daily_loss_limit": "daily loss limit",
    "total_loss_limit": "total loss limit",
    "unknown": "unknown reason",
}


def short_age(seconds: float) -> str:
    """``ago`` without the ``ago``: ``12s``, ``3m``, ``2h``."""
    return ago(seconds).removesuffix(" ago")


def halt_text(data: Mapping[str, object], now: datetime) -> str:
    """``HALTED · <reason words> · <age>`` from trader_state's halt fields; the age is left out when unreadable."""
    reason = data.get("halt_reason")
    parts = ["HALTED", HALT_WORDS.get(reason, reason) if isinstance(reason, str) else "unknown reason"]
    halted_at = data.get("halted_at")
    if isinstance(halted_at, str):
        try:
            since = datetime.fromisoformat(halted_at)
        except ValueError:
            since = None
        if since is not None and since.tzinfo is not None:
            parts.append(short_age((now - since).total_seconds()))
    return " · ".join(parts)


def pause_view(raw: str | bytes | None, now: datetime | None = None) -> PauseView:
    """The pause cue for the raw ``{env}:snapshot:trader_state`` value (None when the key is missing).

    A halt wins over a pause (spec 0010 AC-17): ``HALTED · daily loss limit · 12m``, aged from ``now``.
    """
    if raw is None:
        return PAUSE_NO_DATA
    try:
        snapshot = decode(raw)
    except SnapshotFormatError:
        return PauseView("bad snapshot", "bad")
    if snapshot.data.get("halted") is True:
        return PauseView(halt_text(snapshot.data, now or datetime.now(UTC)), "bad")
    if snapshot.data.get("paused") is not True:
        return RUNNING
    source = snapshot.data.get("pause_source")
    return PauseView(_PAUSED_BY.get(source, "paused") if isinstance(source, str) else "paused", "warn")


@dataclass(frozen=True)
class BlockedReason:
    """One reason entries are blocked: its code and the plain words the dashboard shows."""

    code: str
    words: str


@dataclass(frozen=True)
class BlockedView:
    """Why the trader takes no new entries (spec 0006 AC-18); no reasons means nothing to show."""

    reasons: tuple[BlockedReason, ...]


NOT_BLOCKED: Final = BlockedView(())


def _words(code: str) -> str:
    try:
        return PLAIN_WORDS[BlockReason(code)]
    except ValueError:
        return code  # a reason this dashboard does not know yet still shows, as its code


def blocked_view(raw: str | bytes | None) -> BlockedView:
    """The block reasons in the raw ``{env}:snapshot:trader_state`` value, in its order.

    A missing or unreadable snapshot shows nothing here: the pause cue and the heartbeat age
    already say the trader's state is unknown.
    """
    if raw is None:
        return NOT_BLOCKED
    try:
        snapshot = decode(raw)
    except SnapshotFormatError:
        return NOT_BLOCKED
    codes = snapshot.data.get("entries_blocked")
    if not isinstance(codes, list):
        return NOT_BLOCKED
    return BlockedView(tuple(BlockedReason(code, _words(code)) for code in codes if isinstance(code, str)))


@dataclass(frozen=True)
class LossesView:
    """The loss limits on System (spec 0010 AC-17): each figure against its limit; None shows as ``unknown``.

    ``known`` is False when trader_state is missing or unreadable (the whole box says ``no data``).
    ``cooldown_left`` is the time left (``8m``), None while no cooldown runs.
    """

    known: bool
    loss_today: Decimal | None = None
    daily_limit: Decimal | None = None
    loss_total: Decimal | None = None
    total_limit: Decimal | None = None
    trades_today: int | None = None
    max_trades: int | None = None
    cooldown_left: str | None = None


LOSSES_NO_DATA: Final = LossesView(known=False)


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        return None
    return decimal if decimal.is_finite() else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def losses_view(raw: str | bytes | None, now: datetime) -> LossesView:
    """The loss figures in the raw ``{env}:snapshot:trader_state`` value; a field it lacks reads ``unknown``."""
    if raw is None:
        return LOSSES_NO_DATA
    try:
        data = decode(raw).data
    except SnapshotFormatError:
        return LOSSES_NO_DATA
    cooldown_left = None
    until = data.get("cooldown_until")
    if isinstance(until, str):
        try:
            end = datetime.fromisoformat(until)
        except ValueError:
            end = None
        if end is not None and end.tzinfo is not None and end > now:
            cooldown_left = short_age((end - now).total_seconds())
    return LossesView(
        known=True,
        loss_today=_decimal(data.get("loss_today_usd")),
        daily_limit=_decimal(data.get("daily_loss_limit_usd")),
        loss_total=_decimal(data.get("loss_total_usd")),
        total_limit=_decimal(data.get("total_loss_limit_usd")),
        trades_today=_count(data.get("trades_today")),
        max_trades=_count(data.get("max_trades_per_day")),
        cooldown_left=cooldown_left,
    )
