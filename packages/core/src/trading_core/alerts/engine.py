"""The alert episode state machine (spec 0013 AC-1, AC-9, AC-14): open, repeat, re-notify, resolve, retry.

The engine holds the open episodes and the failed ones awaiting a retry. Each call takes the
time from its input and returns a ``Decision``: the episodes to store (their latest state) and
the pushes to send. The app sends them and reports each outcome back with ``record``. Pure: no
I/O, no clock.
"""

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from trading_core.alerts import messages
from trading_core.alerts.model import CheckResult, Decision, Delivery, Episode, Push, PushReason, RuleKind, Tier
from trading_core.alerts.rules import BY_ID, TEST, TRADER_DOWN, Rule

# Episodes recently resolved stay findable so a push still in flight can record its outcome.
_RECENT_MAX: Final = 1000

EpisodeKey = tuple[str, datetime]


class AlertEngineError(Exception):
    """A call the engine cannot honour: an unknown rule, a rule of the wrong kind, or a naive time."""


@dataclass(frozen=True)
class EngineConfig:
    """The engine's timings (``ALERTS_*`` settings) and the env every episode belongs to."""

    env: str
    renotify_s: int = 3600
    critical_renotify_s: int = 14_400
    quiet_close_s: int = 900
    retry_limit: int = 20
    retry_max_age_s: int = 86_400


def key_of(episode: Episode) -> EpisodeKey:
    """The identity of an episode: its fingerprint and when it opened."""
    return (episode.fingerprint, episode.opened_at)


class AlertEngine:
    """Turns check results into episodes and pushes, one open episode per fingerprint."""

    def __init__(self, config: EngineConfig, rules: Mapping[str, Rule] = BY_ID) -> None:
        self._config = config
        self._rules = rules
        self._open: dict[str, Episode] = {}
        self._failed: dict[EpisodeKey, Episode] = {}
        self._recent: OrderedDict[EpisodeKey, Episode] = OrderedDict()
        self._inflight: set[EpisodeKey] = set()

    # State ----------------------------------------------------------------------------------

    def load(self, episodes: Iterable[Episode]) -> None:
        """Adopt stored episodes after a restart: open ones join the dedupe index, failed ones the retries.

        An episode already known (by fingerprint while open) is kept as is, so a late load never
        reopens or doubles anything (AC-9).
        """
        for episode in episodes:
            if episode.env != self._config.env:
                continue
            if episode.is_open and episode.fingerprint not in self._open:
                self._open[episode.fingerprint] = episode
            if episode.delivery is Delivery.FAILED:
                self._failed.setdefault(key_of(episode), episode)

    @property
    def open_episodes(self) -> tuple[Episode, ...]:
        """The open episodes, oldest first."""
        return tuple(sorted(self._open.values(), key=lambda e: e.opened_at))

    @property
    def push_failing(self) -> bool:
        """True while any episode's push has failed and is still waiting for a retry (AC-14)."""
        return bool(self._failed)

    # Inputs ---------------------------------------------------------------------------------

    def check(self, result: CheckResult) -> Decision:
        """Apply one condition check: a failure opens (or keeps) the episode, a pass resolves it."""
        rule = self._rule(result.rule, RuleKind.CONDITION)
        now = _utc(result.at)
        fingerprint = rule.fingerprint(result.subject)
        current = self._open.get(fingerprint)
        if result.ok:
            return Decision() if current is None else self._resolve(current, now, note=True)
        if current is not None:
            # A condition still failing is the same episode; only the time moves.
            self._open[fingerprint] = replace(current, last_seen_at=now)
            return Decision()
        text_title, text_body = _texts(rule, result.fields)
        episode = Episode(
            env=self._config.env,
            rule=rule.id,
            fingerprint=fingerprint,
            severity=rule.tier,
            source=rule.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details=messages.redact_details(result.fields),
            opened_at=now,
            last_seen_at=now,
        )
        self._open[fingerprint] = episode
        return Decision(store=(episode,)) + self._release(episode, now)

    def tick(self, now: datetime) -> Decision:
        """Time based steps: the ``min_open_s`` release, re-notify, and the quiet close of event episodes."""
        now = _utc(now)
        decision = Decision()
        for episode in list(self._open.values()):
            if key_of(episode) in self._inflight:
                continue
            rule = self._rules.get(episode.rule)
            quiet = now - episode.last_seen_at >= timedelta(seconds=self._config.quiet_close_s)
            if rule is not None and rule.kind is RuleKind.EVENT and quiet:
                decision += self._resolve(episode, now, note=False)
                continue
            decision += self._release(episode, now)
            decision += self._renotify(episode, now, rule)
        return decision

    def test_episodes(self, command_id: str, tiers: Iterable[Tier], now: datetime) -> Decision:
        """One marked test episode and push per tier (AC-16, AC-17). Stored resolved, so never "still open"."""
        now = _utc(now)
        decision = Decision()
        for tier in tiers:
            text_title, text_body = messages.marked_test(tier, command_id)
            episode = Episode(
                env=self._config.env,
                rule=TEST.id,
                fingerprint=f"{TEST.id}:{command_id}:{tier.value}",
                severity=tier,
                source=TEST.source,
                title=messages.title(self._config.env, text_title),
                message=messages.message(text_body),
                details={"command_id": command_id},
                opened_at=now,
                last_seen_at=now,
                resolved_at=now,
            )
            self._remember(episode)
            self._inflight.add(key_of(episode))
            push = Push(episode, PushReason.TEST, tier, episode.title, episode.message)
            decision += Decision(store=(episode,), pushes=(push,))
        return decision

    def retry(self, now: datetime) -> Decision:
        """Pushes for failed episodes: critical first, then oldest, at most ``retry_limit`` (AC-14).

        An episode failing for longer than ``retry_max_age_s`` (since it opened) stops retrying and
        stays ``failed``.
        """
        now = _utc(now)
        cutoff = now - timedelta(seconds=self._config.retry_max_age_s)
        for key, episode in list(self._failed.items()):
            if episode.opened_at < cutoff:
                del self._failed[key]
        due = sorted(
            (e for k, e in self._failed.items() if k not in self._inflight),
            key=lambda e: (e.severity is not Tier.CRITICAL, e.opened_at),
        )[: self._config.retry_limit]
        pushes = []
        for episode in due:
            self._inflight.add(key_of(episode))
            pushes.append(Push(episode, PushReason.OPEN, episode.severity, episode.title, episode.message))
        return Decision(pushes=tuple(pushes))

    def record(self, push: Push, delivery: Delivery, at: datetime) -> Episode | None:
        """Record a push's outcome on its episode; returns the episode to store, or None for a note.

        ``sent`` and ``dry_run`` count as a send. A resolved note's outcome changes no row.
        """
        at = _utc(at)
        key = key_of(push.episode)
        self._inflight.discard(key)
        if push.reason is PushReason.RESOLVED:
            return None
        current = self._find(key) or push.episode
        if delivery in (Delivery.SENT, Delivery.DRY_RUN):
            updated = replace(current, delivery=delivery, sends=current.sends + 1, last_sent_at=at)
            self._failed.pop(key, None)
        else:
            updated = replace(current, delivery=delivery)
            if delivery is Delivery.FAILED:
                self._failed[key] = updated
        self._put(updated)
        return updated

    # Steps ----------------------------------------------------------------------------------

    def _release(self, episode: Episode, now: datetime) -> Decision:
        """The first push once the episode has been open ``min_open_s``."""
        rule = self._rules.get(episode.rule)
        min_open = timedelta(seconds=rule.min_open_s if rule else 0)
        if episode.delivery is not Delivery.PENDING or episode.sends or now - episode.opened_at < min_open:
            return Decision()
        self._inflight.add(key_of(episode))
        return Decision(pushes=(Push(episode, PushReason.OPEN, episode.severity, episode.title, episode.message),))

    def _renotify(self, episode: Episode, now: datetime, rule: Rule | None) -> Decision:
        if episode.last_sent_at is None or episode.delivery is Delivery.FAILED:
            return Decision()
        if episode.severity is Tier.WARNING:
            interval = self._config.renotify_s
        elif episode.severity is Tier.CRITICAL and rule is not None and rule.kind is RuleKind.CONDITION:
            interval = self._config.critical_renotify_s
        else:
            return Decision()
        if now - episode.last_sent_at < timedelta(seconds=interval):
            return Decision()
        self._inflight.add(key_of(episode))
        body = messages.renotify(episode)
        return Decision(pushes=(Push(episode, PushReason.RENOTIFY, episode.severity, episode.title, body),))

    def _resolve(self, episode: Episode, now: datetime, *, note: bool) -> Decision:
        resolved = replace(episode, resolved_at=now, last_seen_at=max(episode.last_seen_at, now))
        del self._open[episode.fingerprint]
        self._remember(resolved)
        if key_of(resolved) in self._failed:
            self._failed[key_of(resolved)] = resolved
        if not note or episode.sends == 0:
            return Decision(store=(resolved,))
        note_title, note_body = messages.resolved(resolved, now)
        push = Push(resolved, PushReason.RESOLVED, Tier.INFO, note_title, note_body)
        return Decision(store=(resolved,), pushes=(push,))

    # Helpers --------------------------------------------------------------------------------

    def _rule(self, rule_id: str, kind: RuleKind) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise AlertEngineError(f"unknown alert rule {rule_id!r}")
        if rule.kind is not kind:
            raise AlertEngineError(f"rule {rule_id!r} is a {rule.kind.value} rule, not {kind.value}")
        return rule

    def _find(self, key: EpisodeKey) -> Episode | None:
        current = self._open.get(key[0])
        if current is not None and key_of(current) == key:
            return current
        return self._recent.get(key) or self._failed.get(key)

    def _put(self, episode: Episode) -> None:
        current = self._open.get(episode.fingerprint)
        if episode.is_open and current is not None and key_of(current) == key_of(episode):
            self._open[episode.fingerprint] = episode
        else:
            self._remember(episode)

    def _remember(self, episode: Episode) -> None:
        self._recent[key_of(episode)] = episode
        self._recent.move_to_end(key_of(episode))
        while len(self._recent) > _RECENT_MAX:
            self._recent.popitem(last=False)


def _texts(rule: Rule, fields: Mapping[str, object]) -> tuple[str, str]:
    if rule.id == TRADER_DOWN.id:
        return messages.trader_down(fields)
    return rule.id.replace("_", " ").capitalize(), ""


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise AlertEngineError(f"alert times must be UTC aware, got {at!r}")
    return at
