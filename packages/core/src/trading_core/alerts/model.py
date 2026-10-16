"""The values the alert engine works on (spec 0013): tiers, delivery states, inputs, and episodes.

Pure: no I/O. Every time is a timezone aware UTC ``datetime``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Tier(StrEnum):
    """How loud a push is: critical breaks through Do Not Disturb, info arrives silently."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Delivery(StrEnum):
    """The latest delivery outcome of an episode (the ``alerts.delivery`` column)."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    MUTED = "muted"
    HELD = "held"
    CAPPED = "capped"


class RuleKind(StrEnum):
    """How a rule opens and closes its episodes (spec 0013, *Rule table*)."""

    CONDITION = "condition"
    EVENT = "event"
    PAIR = "pair"
    INTERNAL = "internal"


class PushReason(StrEnum):
    """Why the engine asks for a push."""

    OPEN = "open"
    RENOTIFY = "renotify"
    RESOLVED = "resolved"
    TEST = "test"


@dataclass(frozen=True)
class Observation:
    """One log line or host line, already parsed: who wrote it, when, its ``code`` and fields.

    ``source`` is the ``alerts.source`` value (``log:<service>`` for a container line). ``text``
    holds a line that is not JSON (a NautilusTrader engine line), with ``code`` and ``level`` None.
    """

    source: str
    service: str
    at: datetime
    code: str | None
    level: str | None
    fields: Mapping[str, object] = field(default_factory=dict)
    text: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one condition check (heartbeat age, disk free, ...) at ``at``.

    ``subject`` names what was checked when a rule covers several things (a service); ``None``
    for a rule with a single subject.
    """

    rule: str
    ok: bool
    at: datetime
    subject: str | None = None
    fields: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Episode:
    """One alert episode, the in memory twin of an ``alerts`` row.

    ``(env, fingerprint, opened_at)`` identifies it; ``id`` is the row id once stored, ``None``
    before that. Episodes are values: the engine replaces them, never mutates them.
    """

    env: str
    rule: str
    fingerprint: str
    severity: Tier
    source: str
    title: str
    message: str
    details: Mapping[str, object]
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    occurrences: int = 1
    sends: int = 0
    last_sent_at: datetime | None = None
    delivery: Delivery = Delivery.PENDING
    id: int | None = None

    @property
    def is_open(self) -> bool:
        """True until the episode resolves."""
        return self.resolved_at is None


@dataclass(frozen=True)
class Push:
    """A push the engine wants sent: what to say and how loud.

    ``episode`` is the episode as it stood when the push was decided; the app records the
    delivery outcome on it.
    """

    episode: Episode
    reason: PushReason
    tier: Tier
    title: str
    message: str


@dataclass(frozen=True)
class Decision:
    """What one engine call changed: the episodes to store (latest state) and the pushes to send."""

    store: tuple[Episode, ...] = ()
    pushes: tuple[Push, ...] = ()

    def __add__(self, other: Decision) -> Decision:
        return Decision(self.store + other.store, self.pushes + other.pushes)
