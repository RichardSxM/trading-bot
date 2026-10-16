"""The alert rule table (spec 0013, *Rule table*), in precedence order.

A log rule carries a matcher and a fingerprint function over one ``Observation``; the first rule
whose matcher accepts a line wins and the rest are ignored. Condition rules are fed
``CheckResult``s by the alerter's checks instead. Host and ``deploys.jsonl`` lines carry no
``code``, so their rules match a field. The fill row joins with its source (Build plan 4.1).
Pure: no I/O.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from trading_core.alerts.model import Observation, RuleKind, Tier

Matcher = Callable[[Observation], bool]
Keyer = Callable[[Observation], str]


@dataclass(frozen=True)
class Rule:
    """One row of the rule table.

    ``min_open_s`` holds the first push until the episode has been open that long; an episode
    that resolves sooner is stored and never pushed. ``source`` is the ``alerts.source`` value
    (a log rule takes its line's ``log:<service>`` instead). ``matches`` and ``key`` make a log
    rule: ``key`` gives the episode fingerprint of a matching line. ``codes`` names the log codes
    the rule depends on, for the coverage test (AC-19). ``partner`` is the code that resolves a
    pair rule's episode.
    """

    id: str
    tier: Tier
    kind: RuleKind
    source: str
    min_open_s: int = 0
    matches: Matcher | None = field(default=None, compare=False)
    key: Keyer | None = field(default=None, compare=False)
    codes: frozenset[str] = frozenset()
    partner: str | None = None

    def fingerprint(self, subject: str | None = None) -> str:
        """The episode fingerprint: the rule id, plus ``:<subject>`` when the rule has several subjects."""
        return self.id if subject is None else f"{self.id}:{subject}"


def text_field(obs: Observation, name: str) -> str:
    """A field of the line as text; empty when it is absent or empty."""
    value = obs.fields.get(name)
    return "" if value is None else str(value)


def _code_rule(
    rule_id: str,
    tier: Tier,
    codes: Iterable[str],
    *,
    kind: RuleKind = RuleKind.EVENT,
    services: frozenset[str] | None = None,
    level: str | None = None,
    key: Keyer | None = None,
    partner: str | None = None,
    min_open_s: int = 0,
    extra: Matcher | None = None,
) -> Rule:
    """A log rule matching ``code`` (and optionally the service, level, or one more test)."""
    names = frozenset(codes)

    def matches(obs: Observation) -> bool:
        return (
            obs.code in names
            and (services is None or obs.service in services)
            and (level is None or obs.level == level)
            and (extra is None or extra(obs))
        )

    return Rule(
        rule_id,
        tier,
        kind,
        source="log",
        min_open_s=min_open_s,
        matches=matches,
        key=key or _by_code,
        codes=names,
        partner=partner,
    )


def _field_rule(rule_id: str, tier: Tier, source: str, name: str, value: str, *, subject: str | None = None) -> Rule:
    """A rule for a host or ``deploys.jsonl`` line, which has no ``code``: it matches ``name == value``.

    The fingerprint is ``<rule>:<subject field>`` (just the rule when the line lacks the field).
    """

    def matches(obs: Observation) -> bool:
        return obs.source == source and text_field(obs, name) == value

    key = _subject(rule_id, subject) if subject is not None else (lambda obs: rule_id)
    return Rule(rule_id, tier, RuleKind.EVENT, source=source, matches=matches, key=key, codes=frozenset({value}))


def _by_code(obs: Observation) -> str:
    return obs.code or ""


def _code_and_service(obs: Observation) -> str:
    return f"{obs.code}:{obs.service}"


def _event_key(obs: Observation) -> str:
    return text_field(obs, "event_key") or _by_code(obs)


def _subject(prefix: str, name: str) -> Keyer:
    """``<prefix>:<field>``, or just ``<prefix>`` when the line lacks the field."""

    def key(obs: Observation) -> str:
        value = text_field(obs, name)
        return f"{prefix}:{value}" if value else prefix

    return key


def _long_gap(obs: Observation) -> bool:
    try:
        return float(text_field(obs, "duration_s") or 0) > BOOK_GAP_LONG_S
    except ValueError:
        return False


def _backup_key(obs: Observation) -> str:
    step = "repo" if obs.code == "restic_repo_missing" else text_field(obs, "step")
    return f"backup_failed:{step}" if step else "backup_failed"


def _risk_critical(obs: Observation) -> bool:
    return obs.service == TRADER and obs.level == "CRITICAL" and bool(text_field(obs, "event_key"))


def _engine_error(obs: Observation) -> bool:
    return obs.service in {TRADER, RECORDER} and obs.code is None and "[ERROR]" in (obs.text or "")


def _trade_cap(obs: Observation) -> bool:
    return text_field(obs, "event_key").startswith("trade_cap:")


TRADER: Final = "trader"
RECORDER: Final = "recorder"
DASHBOARD: Final = "dashboard"
# The ``source`` (and ``service``) of a ``host.jsonl`` line and of a ``deploys.jsonl`` line.
HOST: Final = "host"
DEPLOY: Final = "deploy"
_TRADER_ONLY: Final = frozenset({TRADER})
BOOK_GAP_LONG_S: Final = 60
# login_failed_burst: this many dashboard login failures within the window, all clients together.
LOGIN_BURST_COUNT: Final = 5
LOGIN_BURST_WINDOW_S: Final = 600
# The core services whose absence is critical (AC-4).
CORE_SERVICES: Final = ("trader", "recorder", "postgres", "redis", "clock")

TRADER_DOWN: Final = Rule("trader_down", Tier.CRITICAL, RuleKind.CONDITION, source="redis")
LOSS_LIMIT_HALT: Final = _code_rule(
    "loss_limit_halt", Tier.CRITICAL, ["loss_limit_halt"], services=_TRADER_ONLY, key=_subject("halt", "halt_id")
)
RISK_CRITICAL: Final = Rule(
    "risk_critical", Tier.CRITICAL, RuleKind.EVENT, source="log", matches=_risk_critical, key=_event_key
)
RISK_HALT_MANUAL: Final = _code_rule(
    "risk_halt_manual", Tier.INFO, ["halt"], services=_TRADER_ONLY, level="WARNING", key=_event_key
)
# ``record_conflict`` is logged at ERROR with no ``event_key``, so ``risk_critical`` misses it;
# spec 0013's ``trader_critical_codes`` row names it, keyed by its code.
TRADER_CRITICAL_CODES: Final = _code_rule(
    "trader_critical_codes",
    Tier.CRITICAL,
    ["positions_abandoned", "kill_switch_task_crashed", "halt_key_invalid", "record_conflict"],
    services=_TRADER_ONLY,
)
# ``kfs-autoheal`` logs its giving up as the message itself (spec 0005).
AUTOHEAL_GAVE_UP: Final = _field_rule(
    "autoheal_gave_up", Tier.CRITICAL, HOST, "msg", "autoheal_gave_up", subject="container"
)
# The host's "rollback failed" line is not matched, so a rollback makes one push.
ROLLBACK_FAILED: Final = _field_rule(
    "rollback_failed", Tier.CRITICAL, DEPLOY, "outcome", "rollback_failed", subject="sha"
)
DISK_LOW: Final = Rule("disk_low", Tier.CRITICAL, RuleKind.CONDITION, source="disk")
CATALOG_DISK_FULL: Final = _code_rule(
    "catalog_disk_full", Tier.CRITICAL, ["catalog_disk_full"], kind=RuleKind.PAIR, partner="catalog_disk_ok"
)
RECORDER_WRITE_FAILED: Final = _code_rule("recorder_write_failed", Tier.CRITICAL, ["recorder_write_failed"])
POSTGRES_UNREACHABLE: Final = Rule(
    "postgres_unreachable", Tier.CRITICAL, RuleKind.CONDITION, source="self", min_open_s=60
)
REDIS_UNREACHABLE: Final = Rule("redis_unreachable", Tier.CRITICAL, RuleKind.CONDITION, source="self", min_open_s=60)
SERVICE_DOWN: Final = Rule("service_down", Tier.CRITICAL, RuleKind.CONDITION, source="docker", min_open_s=120)
KEY_AND_SECRET_CODES: Final = _code_rule(
    "key_and_secret_codes",
    Tier.CRITICAL,
    ["kraken_auth_failed", "secret_unavailable", "kraken_key_check_crashed"],
    key=_code_and_service,
)
CONTAINER_UNHEALTHY: Final = Rule(
    "container_unhealthy", Tier.WARNING, RuleKind.CONDITION, source="docker", min_open_s=120
)
BACKUP_FAILED: Final = _code_rule(
    "backup_failed", Tier.WARNING, ["backup_failed", "restic_repo_missing"], key=_backup_key
)
# ``min_open_s`` is the default; the engine takes ``ALERTS_ENTRY_BLOCK_S`` from its config.
ENTRY_BLOCK: Final = Rule("entry_block", Tier.WARNING, RuleKind.CONDITION, source="redis", min_open_s=3600)
CONTEXT_FEED_DOWN: Final = _code_rule(
    "context_feed_down",
    Tier.WARNING,
    ["context_feed_down"],
    kind=RuleKind.PAIR,
    partner="context_feed_up",
    min_open_s=300,
    key=_code_and_service,
)
# One push per hour, however many of its context tables miss a file.
CONTEXT_HOUR_MISSING: Final = _code_rule(
    "context_hour_missing",
    Tier.WARNING,
    ["context_hour_missing"],
    services=frozenset({"compactor"}),
    key=_subject("context_hour_missing", "hour"),
)
BOOK_GAP_LONG: Final = _code_rule(
    "book_gap_long", Tier.WARNING, ["book_gap"], key=_subject("book_gap_long", "kind"), extra=_long_gap
)
BOOK_SEQUENCE_HOLE: Final = _code_rule("book_sequence_hole", Tier.WARNING, ["book_sequence_hole"])
RECORDER_CODES: Final = _code_rule(
    "recorder_codes",
    Tier.WARNING,
    [
        "catalog_retention_blocked",
        "catalog_late_rows",
        "recorder_queue_full",
        "recorder_stop_queue_full",
        "compactor_pass_failed",
        "recorder_writer_busy",
        "recorder_flush_slow",
    ],
)
KRAKEN_KEY_ROTATION_DUE: Final = _code_rule("kraken_key_rotation_due", Tier.WARNING, ["kraken_key_rotation_due"])
# Fed by the engine's burst counter, not by one line (its matcher stays None).
LOGIN_FAILED_BURST: Final = Rule(
    "login_failed_burst", Tier.WARNING, RuleKind.EVENT, source="log", codes=frozenset({"login_failed"})
)
SNAPSHOT_WRITE_FAILED: Final = _code_rule(
    "snapshot_write_failed", Tier.WARNING, ["snapshot_write_failed"], services=_TRADER_ONLY
)
ENGINE_ERROR: Final = Rule(
    "engine_error",
    Tier.WARNING,
    RuleKind.EVENT,
    source="log",
    matches=_engine_error,
    key=lambda obs: f"engine_error:{obs.service}",
)
STALE_FEED: Final = _code_rule("stale_feed", Tier.WARNING, ["stale_feed"], services=_TRADER_ONLY)
TRADE_CAP: Final = Rule("trade_cap", Tier.INFO, RuleKind.EVENT, source="log", matches=_trade_cap, key=_event_key)
ALERT_STORM: Final = Rule("alert_storm", Tier.WARNING, RuleKind.INTERNAL, source="self")
SUMMARY: Final = Rule("summary", Tier.WARNING, RuleKind.INTERNAL, source="self")
# A test push takes the tier it tests; ``tier`` here is only the default.
TEST: Final = Rule("test", Tier.INFO, RuleKind.INTERNAL, source="self")

RULES: Final[tuple[Rule, ...]] = (
    TRADER_DOWN,
    LOSS_LIMIT_HALT,
    RISK_CRITICAL,
    RISK_HALT_MANUAL,
    TRADER_CRITICAL_CODES,
    AUTOHEAL_GAVE_UP,
    ROLLBACK_FAILED,
    DISK_LOW,
    CATALOG_DISK_FULL,
    RECORDER_WRITE_FAILED,
    POSTGRES_UNREACHABLE,
    REDIS_UNREACHABLE,
    SERVICE_DOWN,
    KEY_AND_SECRET_CODES,
    CONTAINER_UNHEALTHY,
    BACKUP_FAILED,
    ENTRY_BLOCK,
    CONTEXT_FEED_DOWN,
    CONTEXT_HOUR_MISSING,
    BOOK_GAP_LONG,
    BOOK_SEQUENCE_HOLE,
    RECORDER_CODES,
    KRAKEN_KEY_ROTATION_DUE,
    LOGIN_FAILED_BURST,
    SNAPSHOT_WRITE_FAILED,
    ENGINE_ERROR,
    STALE_FEED,
    TRADE_CAP,
    ALERT_STORM,
    SUMMARY,
    TEST,
)

BY_ID: Final[Mapping[str, Rule]] = MappingProxyType({rule.id: rule for rule in RULES})


def first_match(obs: Observation, rules: Iterable[Rule] = RULES) -> Rule | None:
    """The first log rule, in table order, whose matcher accepts ``obs``; None when no rule does."""
    return next((rule for rule in rules if rule.matches is not None and rule.matches(obs)), None)


def partner_of(obs: Observation, rules: Iterable[Rule] = RULES) -> Rule | None:
    """The pair rule whose partner code ``obs`` carries; None when it carries none."""
    if obs.code is None:
        return None
    return next((rule for rule in rules if rule.partner == obs.code), None)
