"""Alert episodes in Overview's Activity feed, the mute badge, and the test answer (spec 0013 AC-16).

Pure functions.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from dashboard.data.alerts import AlertLine
from dashboard.live.state import ActivityRow, Entry
from trading_core.control.alerts import AlertResult, result_from_fields
from trading_core.control.messages import ControlFormatError
from trading_core.recording.enums import AlertCmd, ControlOutcome

# Postgres did not answer: the commands still show, with this row on top.
ALERTS_UNAVAILABLE: Final = ActivityRow("alerts", None, "unavailable: Postgres did not answer", "warn", invalid=True)


def alert_row(line: AlertLine) -> ActivityRow:
    """The Activity row of one alert episode."""
    return ActivityRow("alert", line.opened_at, "", "none", final=True, alert=line)


def merge_activity(commands: Sequence[ActivityRow], alerts: Sequence[AlertLine] | None) -> list[ActivityRow]:
    """Commands and alert episodes in one list, newest first; ``alerts`` None means Postgres failed.

    Both inputs are newest first. A command row without a time (one that does not parse) keeps
    its place among the commands.
    """
    pending = list(alerts or ())
    merged: list[ActivityRow] = [] if alerts is not None else [ALERTS_UNAVAILABLE]
    for row in commands:
        if row.issued_at is not None:
            while pending and pending[0].opened_at > row.issued_at:
                merged.append(alert_row(pending.pop(0)))
        merged.append(row)
    merged += [alert_row(line) for line in pending]
    return merged


@dataclass(frozen=True)
class MuteView:
    """The mute badge: ``until`` when warnings are muted now, None when they are not."""

    until: datetime | None


MUTE_OFF: Final = MuteView(None)
_MUTE_CMDS: Final = frozenset({AlertCmd.MUTE, AlertCmd.UNMUTE})


def _results(entries: Sequence[Entry] | None) -> list[AlertResult]:
    parsed = []
    for _, fields in entries or ():
        if fields is None:
            continue
        try:
            parsed.append(result_from_fields(fields))
        except ControlFormatError:
            continue
    return parsed


def mute_view(entries: Sequence[Entry] | None, now: datetime) -> MuteView:
    """From the newest executed mute or unmute on ``{env}:control:alerts:results`` (newest first).

    A mute shows until its ``until``; an unmute, a past ``until``, or no such result shows nothing.
    """
    for result in _results(entries):
        if result.cmd in _MUTE_CMDS and result.outcome is ControlOutcome.EXECUTED:
            if result.cmd is AlertCmd.MUTE and result.until is not None and result.until > now:
                return MuteView(result.until)
            return MUTE_OFF
    return MUTE_OFF


def answer_of(entries: Sequence[Entry] | None, command_id: uuid.UUID) -> AlertResult | None:
    """The final result of the command ``command_id`` among ``entries``, or None while there is none."""
    return next((r for r in _results(entries) if r.id == command_id and r.outcome is not ControlOutcome.RECEIVED), None)
