"""The alert episode state machine (spec 0013 AC-1, AC-3, AC-6, AC-9 to AC-11, AC-14): open, repeat,
re-notify, resolve, retry, the hourly storm cap, and the hold windows with their summary push.

The engine holds the open episodes and the failed ones awaiting a retry. Each call takes the
time from its input and returns a ``Decision``: the episodes to store (their latest state) and
the pushes to send. The app sends them and reports each outcome back with ``record``. Pure: no
I/O, no clock.
"""

from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final

from trading_core.alerts import daily, messages
from trading_core.alerts.model import (
    CheckResult,
    Decision,
    Delivery,
    Episode,
    Hold,
    HoldKind,
    Observation,
    Push,
    PushReason,
    RuleKind,
    Tier,
)
from trading_core.alerts.rules import (
    ALERT_STORM,
    BY_ID,
    DAILY,
    DASHBOARD,
    DEPLOY,
    ENTRY_BLOCK,
    FILL,
    HOST,
    LOGIN_BURST_COUNT,
    LOGIN_BURST_WINDOW_S,
    LOGIN_FAILED_BURST,
    SUMMARY,
    TEST,
    TRADER_DOWN,
    Rule,
    first_match,
    partner_of,
    text_field,
)

# Episodes recently resolved stay findable so a push still in flight can record its outcome.
_RECENT_MAX: Final = 1000
# The ``host.jsonl`` message that opens a deploy window (AC-11).
DEPLOY_STARTED: Final = "deploy started"
# Fills push only from this env (AC-8).
LIVE: Final = "live"
_HELD: Final = frozenset({Delivery.HELD, Delivery.MUTED})
# A retried test or daily push keeps its reason, so it still passes the holds and the cap.
_RETRY_REASONS: Final = MappingProxyType({TEST.id: PushReason.TEST, DAILY.id: PushReason.DAILY})

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
    pair_quiet_s: int = 86_400
    retry_limit: int = 20
    retry_max_age_s: int = 86_400
    hourly_cap: int = 30
    entry_block_s: int = 3600
    # An entry block reason back within this long of its last episode keeps that episode's start.
    entry_block_rejoin_s: int = 300
    deploy_window_s: int = 900


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
        self._logins: deque[datetime] = deque()
        # The storm cap (AC-10): the UTC hour counted, warning and info pushes sent in it, the
        # ones admitted and not yet reported (by ``id`` of the push), and the hour's storm episode.
        self._hour: datetime | None = None
        self._hour_sent = 0
        self._counted: set[int] = set()
        self._storm: Episode | None = None
        # The hold windows (AC-11, AC-12), and each summary push's covered episodes until it is sent.
        self._holds: dict[HoldKind, Hold] = {}
        self._summaries: dict[EpisodeKey, tuple[EpisodeKey, ...]] = {}

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

    @property
    def holds(self) -> tuple[Hold, ...]:
        """The hold windows now open."""
        return tuple(self._holds.values())

    # Inputs ---------------------------------------------------------------------------------

    def check(self, result: CheckResult) -> Decision:
        """Apply one condition check: a failure opens (or keeps) the episode, a pass resolves it.

        A new episode opens at the result's ``since`` when it has one (never later than ``at``).
        An ``entry_block`` episode also records in ``details["since"]`` when its reason first
        blocked: the start of an unpushed episode for the same reason that resolved less than
        ``entry_block_rejoin_s`` ago, so a trader restart does not restart the hour (AC-6).
        """
        rule = self._rule(result.rule, RuleKind.CONDITION)
        now = _utc(result.at)
        fingerprint = rule.fingerprint(result.subject)
        current = self._open.get(fingerprint)
        if result.ok:
            return Decision() if current is None else self._gate(self._resolve(current, now, note=True), now)
        if current is not None:
            # A condition still failing is the same episode; only the time moves.
            self._open[fingerprint] = replace(current, last_seen_at=now)
            return Decision()
        opened_at = now if result.since is None else min(_utc(result.since), now)
        details = dict(result.fields)
        if rule.id == ENTRY_BLOCK.id:
            details["since"] = min([opened_at, *self._rejoin(fingerprint, now)]).isoformat()
        text_title, text_body = messages.check(rule.id, result.subject, result.fields)
        episode = Episode(
            env=self._config.env,
            rule=rule.id,
            fingerprint=fingerprint,
            severity=rule.tier,
            source=rule.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details=messages.redact_details(details),
            opened_at=opened_at,
            last_seen_at=now,
        )
        self._open[fingerprint] = episode
        return Decision(store=(episode,)) + self._gate(self._release(episode, now), now)

    def observe(self, obs: Observation) -> Decision:
        """Apply one log line (AC-3, AC-7): the first matching rule opens or bumps its episode.

        A pair rule's partner code resolves that rule's open episode from the same source. A
        repeat of an open fingerprint, whatever rule matched it, bumps ``occurrences`` and
        ``last_seen_at`` and pushes nothing (AC-9). A fill line counts only in ``live``
        (AC-8). A deploy's ``host.jsonl`` start line and its
        ``deploys.jsonl`` outcome line also open and close the deploy window (AC-11).
        """
        now = _utc(obs.at)
        window = self._deploy_window(obs, now)
        if obs.source == HOST and text_field(obs, "msg") == DEPLOY_STARTED:
            return window
        return window + self._line(obs, now)

    def _line(self, obs: Observation, now: datetime) -> Decision:
        pair = partner_of(obs, self._rules.values())
        if pair is not None:
            return self._gate(self._resolve_where(now, lambda e: e.rule == pair.id and e.source == obs.source), now)
        rule = first_match(obs, self._rules.values())
        if rule is None and self._login_burst(obs, now):
            rule = self._rules.get(LOGIN_FAILED_BURST.id)
            obs = replace(obs, fields={**obs.fields, "count": len(self._logins)})
        if rule is None or (rule.id == FILL.id and self._config.env != LIVE):
            return Decision()
        fingerprint = rule.key(obs) if rule.key is not None else rule.fingerprint()
        current = self._open.get(fingerprint)
        if current is not None:
            bumped = replace(current, occurrences=current.occurrences + 1, last_seen_at=max(current.last_seen_at, now))
            self._open[fingerprint] = bumped
            return Decision(store=(bumped,))
        text_title, text_body = messages.log_line(rule.id, obs.service, obs.code, obs.fields, obs.text)
        details = dict(obs.fields) if obs.text is None else {**obs.fields, "text": obs.text}
        episode = Episode(
            env=self._config.env,
            rule=rule.id,
            fingerprint=fingerprint,
            severity=rule.tier,
            source=obs.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details=messages.redact_details(details),
            opened_at=now,
            last_seen_at=now,
        )
        self._open[fingerprint] = episode
        return Decision(store=(episode,)) + self._gate(self._release(episode, now), now)

    def service_started(self, service: str, started_at: datetime) -> Decision:
        """A service's container (re)started at ``started_at``: its older pair episodes resolve."""
        started_at = _utc(started_at)
        source = f"log:{service}"

        def stale(episode: Episode) -> bool:
            rule = self._rules.get(episode.rule)
            kind = rule.kind if rule is not None else None
            return kind is RuleKind.PAIR and episode.source == source and episode.opened_at < started_at

        return self._gate(self._resolve_where(started_at, stale), started_at)

    def open_hold(self, hold: Hold) -> Decision:
        """Open a hold window; one of the same kind is replaced, and what it held stays held."""
        self._holds[hold.kind] = replace(hold, started_at=_utc(hold.started_at), until=_utc(hold.until))
        return Decision()

    def close_hold(self, kind: HoldKind, now: datetime) -> Decision:
        """End a hold window (AC-11, AC-12): see ``_close``."""
        now = _utc(now)
        return self._gate(self._close(kind, now), now)

    def tick(self, now: datetime) -> Decision:
        """Time based steps: hold windows that ran out, the ``min_open_s`` release, re-notify, and
        the quiet close of event episodes."""
        now = _utc(now)
        decision = Decision()
        for hold in list(self._holds.values()):
            if now >= hold.until:
                decision += self._close(hold.kind, now)
        for episode in list(self._open.values()):
            if key_of(episode) in self._inflight:
                continue
            rule = self._rules.get(episode.rule)
            quiet_s = (now - episode.last_seen_at).total_seconds()
            if rule is not None and rule.kind is RuleKind.EVENT and quiet_s >= self._config.quiet_close_s:
                decision += self._resolve(episode, now, note=False)
                continue
            if rule is not None and rule.kind is RuleKind.PAIR and quiet_s >= self._config.pair_quiet_s:
                decision += self._resolve(episode, now, note=True)
                continue
            decision += self._release(episode, now)
            decision += self._renotify(episode, now, rule)
        return self._gate(decision, now)

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

    def daily_note(self, day: date, figures: daily.DailyFigures, now: datetime) -> Decision:
        """The daily note's episode and silent push (AC-18). Stored resolved, never held or capped."""
        now = _utc(now)
        text_title, text_body = daily.text(day, figures)
        episode = Episode(
            env=self._config.env,
            rule=DAILY.id,
            fingerprint=daily.fingerprint(day),
            severity=DAILY.tier,
            source=DAILY.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details={},
            opened_at=now,
            last_seen_at=now,
            resolved_at=now,
        )
        self._remember(episode)
        self._inflight.add(key_of(episode))
        push = Push(episode, PushReason.DAILY, episode.severity, episode.title, episode.message)
        return Decision(store=(episode,), pushes=(push,))

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
            reason = _RETRY_REASONS.get(episode.rule, PushReason.OPEN)
            pushes.append(Push(episode, reason, episode.severity, episode.title, episode.message))
        return self._gate(Decision(pushes=tuple(pushes)), now)

    def record(self, push: Push, delivery: Delivery, at: datetime) -> tuple[Episode, ...]:
        """Record a push's outcome on its episode; returns the episodes to store (none for a note).

        ``sent`` and ``dry_run`` count as a send. A resolved note's outcome changes no row. A sent
        summary also counts as a send on every episode it covers.
        """
        at = _utc(at)
        key = key_of(push.episode)
        self._inflight.discard(key)
        if id(push) in self._counted:
            self._counted.discard(id(push))
            if delivery is Delivery.SENT:
                self._roll(at)
                self._hour_sent += 1
        if push.reason is PushReason.RESOLVED:
            return ()
        updated = self._outcome(key, push.episode, delivery, at)
        if delivery not in (Delivery.SENT, Delivery.DRY_RUN) or key not in self._summaries:
            return (updated,)
        covered = [self._find(k) for k in self._summaries.pop(key)]
        return (updated, *(self._outcome(key_of(e), e, delivery, at) for e in covered if e is not None))

    def _outcome(self, key: EpisodeKey, episode: Episode, delivery: Delivery, at: datetime) -> Episode:
        current = self._find(key) or episode
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
        """The first push once the episode has been open ``min_open_s`` (counted from its ``since``)."""
        rule = self._rules.get(episode.rule)
        min_open = timedelta(seconds=self._min_open(rule))
        if episode.delivery is not Delivery.PENDING or episode.sends or now - _since(episode) < min_open:
            return Decision()
        self._inflight.add(key_of(episode))
        return Decision(pushes=(Push(episode, PushReason.OPEN, episode.severity, episode.title, episode.message),))

    def _min_open(self, rule: Rule | None) -> int:
        if rule is None:
            return 0
        return self._config.entry_block_s if rule.id == ENTRY_BLOCK.id else rule.min_open_s

    def _renotify(self, episode: Episode, now: datetime, rule: Rule | None) -> Decision:
        if episode.last_sent_at is None or episode.delivery is Delivery.FAILED or episode.delivery in _HELD:
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

    def _resolve_where(self, now: datetime, which: Callable[[Episode], bool]) -> Decision:
        decision = Decision()
        for episode in [e for e in self._open.values() if which(e)]:
            decision += self._resolve(episode, max(now, episode.opened_at), note=True)
        return decision

    def _rejoin(self, fingerprint: str, now: datetime) -> list[datetime]:
        """The starts of unpushed episodes of ``fingerprint`` that resolved within the rejoin time."""
        cutoff = now - timedelta(seconds=self._config.entry_block_rejoin_s)
        return [
            _since(e)
            for e in self._recent.values()
            if e.fingerprint == fingerprint and e.resolved_at is not None and e.resolved_at >= cutoff and not e.sends
        ]

    # The hold windows (AC-11, AC-12) -------------------------------------------------------

    def _deploy_window(self, obs: Observation, now: datetime) -> Decision:
        """Open the deploy window on its start line; close it on the matching ``deploys.jsonl`` line.

        The match is the same ``sha`` with a ``started_at`` at or after the window's start. The
        start line's own ``started_at`` (the deploy's start, stamped just before the line) is the
        window's start when present.
        """
        if obs.source == HOST and text_field(obs, "msg") == DEPLOY_STARTED:
            sha = text_field(obs, "sha")
            start = min(_parse_utc(text_field(obs, "started_at")) or now, now)
            window_id = f"deploy:{sha[:12] or 'unknown'}:{start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            until = now + timedelta(seconds=self._config.deploy_window_s)
            return self.open_hold(Hold(HoldKind.DEPLOY, window_id, start, until, sha=sha))
        hold = self._holds.get(HoldKind.DEPLOY)
        if obs.source != DEPLOY or hold is None or text_field(obs, "sha") != hold.sha:
            return Decision()
        started_at = _parse_utc(text_field(obs, "started_at"))
        if started_at is None or started_at < hold.started_at:
            return Decision()
        return self.close_hold(HoldKind.DEPLOY, now)

    def _close(self, kind: HoldKind, now: datetime) -> Decision:
        """End the ``kind`` window, ungated.

        A held critical (only ``trader_down``) still open is pushed now. Held warnings still open
        go out as one summary push, once no other window holds them; while one does, they move to
        it. Held info and held episodes that already resolved are dropped.
        """
        hold = self._holds.pop(kind, None)
        if hold is None:
            return Decision()
        decision = Decision()
        for episode in list(self._open.values()):
            if episode.delivery is not hold.delivery or key_of(episode) in self._inflight:
                continue
            if episode.severity is Tier.CRITICAL:
                back = replace(episode, delivery=Delivery.SENT if episode.sends else Delivery.PENDING)
                self._open[back.fingerprint] = back
                decision += Decision(store=(back,)) + self._release(back, now)
            elif self._holds and episode.severity is Tier.WARNING:
                other = next(iter(self._holds.values()))
                moved = replace(episode, delivery=other.delivery)
                self._open[moved.fingerprint] = moved
                decision += Decision(store=(moved,))
        if self._holds:
            return decision
        held = [
            e
            for e in self.open_episodes
            if e.severity is Tier.WARNING and e.delivery in _HELD and key_of(e) not in self._inflight
        ]
        return decision + self._summary(hold, held, now) if held else decision

    def _summary(self, hold: Hold, held: list[Episode], now: datetime) -> Decision:
        text_title, text_body = messages.summary(hold.kind, [e.title for e in held])
        episode = Episode(
            env=self._config.env,
            rule=SUMMARY.id,
            fingerprint=f"{SUMMARY.id}:{hold.id}",
            severity=SUMMARY.tier,
            source=SUMMARY.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details={"window": hold.kind.value, "id": hold.id, "covers": [e.fingerprint for e in held]},
            opened_at=now,
            last_seen_at=now,
            resolved_at=now,
        )
        key = key_of(episode)
        self._remember(episode)
        self._inflight.add(key)
        self._summaries[key] = tuple(key_of(e) for e in held)
        push = Push(episode, PushReason.SUMMARY, episode.severity, episode.title, episode.message)
        return Decision(store=(episode,), pushes=(push,))

    def _hold(self, decision: Decision) -> Decision:
        """Hold each push an open window covers: its episode takes the window's delivery state.

        A held note is dropped. A failed episode held here leaves the retries; the summary sends it.
        """
        if not self._holds:
            return decision
        store = list(decision.store)
        pushes: list[Push] = []
        for push in decision.pushes:
            hold = self._holding(push)
            if hold is None:
                pushes.append(push)
                continue
            key = key_of(push.episode)
            self._inflight.discard(key)
            if push.reason is PushReason.RESOLVED:
                continue
            self._failed.pop(key, None)
            held = replace(self._find(key) or push.episode, delivery=hold.delivery)
            self._put(held)
            store.append(held)
        return Decision(store=tuple(store), pushes=tuple(pushes))

    def _holding(self, push: Push) -> Hold | None:
        """The window that holds ``push``: test, summary, and daily pushes never; a critical only
        when it is ``trader_down`` in a deploy window; a deploy window before a mute."""
        if push.reason in (PushReason.TEST, PushReason.SUMMARY, PushReason.DAILY):
            return None
        if push.tier is Tier.CRITICAL:
            return self._holds.get(HoldKind.DEPLOY) if push.episode.rule == TRADER_DOWN.id else None
        return self._holds.get(HoldKind.DEPLOY) or self._holds.get(HoldKind.MUTE)

    def _login_burst(self, obs: Observation, now: datetime) -> bool:
        """Counts dashboard ``login_failed`` lines; True once a window holds ``LOGIN_BURST_COUNT``."""
        if obs.service != DASHBOARD or obs.code not in LOGIN_FAILED_BURST.codes:
            return False
        self._logins.append(now)
        window = timedelta(seconds=LOGIN_BURST_WINDOW_S)
        while self._logins and now - self._logins[0] > window:
            self._logins.popleft()
        return len(self._logins) >= LOGIN_BURST_COUNT

    # The storm cap (AC-10) -------------------------------------------------------------------

    def _gate(self, decision: Decision, now: datetime) -> Decision:
        """Admit each warning and info push while the hour has room; past it, cap.

        The push that would reach the cap is replaced by one storm push; later warning and info
        pushes that hour are capped. A capped open or re-notify push marks its episode ``capped``;
        a capped note is dropped. Critical, test, and daily pushes always pass and never count.
        Pushes an open hold window covers are held first and never reach the cap.
        """
        decision = self._hold(decision)
        if not decision.pushes:
            return decision
        self._roll(now)
        store = list(decision.store)
        pushes: list[Push] = []
        for push in decision.pushes:
            if push.tier is Tier.CRITICAL or push.reason in (PushReason.TEST, PushReason.DAILY):
                pushes.append(push)
                continue
            if self._storm is None and self._hour_sent + len(self._counted) < self._config.hourly_cap - 1:
                self._counted.add(id(push))
                pushes.append(push)
                continue
            store += self._cap(push)
            if self._storm is None:
                storm_push = self._open_storm(now)
                store.append(storm_push.episode)
                self._counted.add(id(storm_push))
                pushes.append(storm_push)
            else:
                self._storm = replace(self._storm, occurrences=self._storm.occurrences + 1, last_seen_at=now)
                self._put(self._storm)
                store.append(self._storm)
        return Decision(store=tuple(store), pushes=tuple(pushes))

    def _cap(self, push: Push) -> list[Episode]:
        key = key_of(push.episode)
        self._inflight.discard(key)
        if push.reason is PushReason.RESOLVED:
            return []
        # A capped summary leaves what it covered capped too: none of it is pushed later.
        keys = [key, *self._summaries.pop(key, ())]
        capped = []
        for k in keys:
            current = self._find(k) or (push.episode if k == key else None)
            if current is not None:
                capped.append(replace(current, delivery=Delivery.CAPPED))
                self._put(capped[-1])
        return capped

    def _open_storm(self, now: datetime) -> Push:
        hour = now.replace(minute=0, second=0, microsecond=0)
        text_title, text_body = messages.storm(1, hour + timedelta(hours=1))
        episode = Episode(
            env=self._config.env,
            rule=ALERT_STORM.id,
            fingerprint=f"{ALERT_STORM.id}:{hour.strftime('%Y-%m-%dT%H')}",
            severity=ALERT_STORM.tier,
            source=ALERT_STORM.source,
            title=messages.title(self._config.env, text_title),
            message=messages.message(text_body),
            details={"hour": hour.isoformat()},
            opened_at=now,
            last_seen_at=now,
            resolved_at=now,
        )
        self._storm = episode
        self._remember(episode)
        self._inflight.add(key_of(episode))
        return Push(episode, PushReason.OPEN, episode.severity, episode.title, episode.message)

    def _roll(self, now: datetime) -> None:
        hour = now.replace(minute=0, second=0, microsecond=0)
        if hour != self._hour:
            self._hour, self._hour_sent, self._storm = hour, 0, None

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


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise AlertEngineError(f"alert times must be UTC aware, got {at!r}")
    return at


def _parse_utc(text: str) -> datetime | None:
    """An ISO UTC time from a host line (``2026-10-16T03:00:00Z``); None when absent, bad, or not UTC."""
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        return None
    return at


def _since(episode: Episode) -> datetime:
    """When the episode's condition began: ``details["since"]`` when recorded, else ``opened_at``."""
    since = episode.details.get("since")
    parsed = _parse_utc(since) if isinstance(since, str) else None
    return parsed if parsed is not None and parsed <= episode.opened_at else episode.opened_at
