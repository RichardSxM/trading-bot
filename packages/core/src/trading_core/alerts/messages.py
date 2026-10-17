"""Alert titles and bodies (spec 0013): the env tag, plain words, Pushover's length limits, redaction.

Every text leaving here has passed ``trading_core.secrets.redact``. Pure apart from reading the
redaction registry.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from trading_core.alerts.model import Episode, HoldKind, Tier
from trading_core.secrets import redact
from trading_core.strategy.blocks import PLAIN_WORDS, BlockReason

# Pushover's limits are 250 and 1,024 characters.
TITLE_MAX: Final = 250
MESSAGE_MAX: Final = 1024
_ELLIPSIS: Final = "…"


def cut(text: str, limit: int) -> str:
    """``text`` redacted and cut to ``limit`` characters, ending in "…" when it was longer."""
    clean = redact(text)
    if len(clean) <= limit:
        return clean
    return clean[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def title(env: str, text: str) -> str:
    """A push title: ``[demo]`` or ``[live]``, then the text, redacted and cut."""
    return cut(f"[{env}] {text}", TITLE_MAX)


def message(text: str) -> str:
    """A push body, redacted and cut."""
    return cut(text, MESSAGE_MAX)


def redact_details(details: Mapping[str, object]) -> dict[str, object]:
    """``details`` with every string value redacted (nested mappings and lists too)."""
    return {key: _redact_value(value) for key, value in details.items()}


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def trader_down(fields: Mapping[str, object]) -> tuple[str, str]:
    """Title text and body for ``trader_down``, from the check's ``age_s`` (absent when the key is missing)."""
    age = fields.get("age_s")
    if isinstance(age, int):
        return "Trader down", f"No heartbeat from the trader for {age} s."
    return "Trader down", "No trader heartbeat in Redis."


def words(code: str) -> str:
    """A code in plain words: ``stop_not_accepted`` becomes "Stop not accepted"."""
    return code.replace("_", " ").capitalize()


# The fields worth naming in a push body, in order; the rest stay in ``details``.
_BODY_FIELDS: Final = ("event_key", "reason", "step", "kind", "duration_s", "missing", "table", "error", "secret")
_RISK_RULES: Final = frozenset({"loss_limit_halt", "risk_critical", "risk_halt_manual", "trade_cap"})


def log_line(
    rule_id: str, service: str, code: str | None, fields: Mapping[str, object], text: str | None
) -> tuple[str, str]:
    """Title text and body for an episode opened by a log line.

    A risk event is titled by its ``event_key`` kind ("Stop not accepted"); any other line by its
    code and service. The body is the line's ``msg`` plus the fields that say what happened.
    """
    if rule_id == "engine_error":
        return f"Engine error on {service}", text or ""
    if rule_id == "login_failed_burst":
        return "Dashboard login failures", f"{fields.get('count', '?')} failed logins within 10 min, all clients."
    if rule_id == "fill":
        return fill(fields)
    event_key = str(fields.get("event_key") or "")
    if rule_id in _RISK_RULES:
        head = words(event_key.split(":", 1)[0] if event_key else (code or rule_id))
    else:
        head = f"{words(code or rule_id)} on {service}"
    parts = [str(fields.get("msg") or "")]
    parts += [f"{name}={fields[name]}" for name in _BODY_FIELDS if fields.get(name) not in (None, "")]
    return head, " ".join(p for p in parts if p)


# The ``fills`` and ``instruments`` columns the alerter adds to a ``fill_seen`` line (AC-8).
FILL_FIELDS: Final = (
    "instrument_id",
    "side",
    "quantity",
    "price",
    "fee",
    "fee_currency",
    "base_currency",
    "lot_size",
    "tick_size",
)
_SIDE_WORDS: Final = {"buy": "Bought", "sell": "Sold"}


def fill(fields: Mapping[str, object]) -> tuple[str, str]:
    """Title text and body of a live fill: "Bought 0.0010 BTC at 67,412.5, fee 0.02 USD" (AC-8).

    Quantity and price take the instrument's lot and tick precision. Without the fills row (the
    lookup found none in time), the body is "fill <trade_id>".
    """
    trade_id = str(fields.get("trade_id") or "?")
    instrument = str(fields.get("instrument_id") or "")
    head = f"Fill {instrument}" if instrument else "Fill"
    try:
        side = _SIDE_WORDS[str(fields["side"])]
        quantity = _step(_decimal(fields["quantity"]), _decimal(fields["lot_size"]))
        price = _step(_decimal(fields["price"]), _decimal(fields["tick_size"]))
        fee = _decimal(fields["fee"])
        base, fee_currency = str(fields["base_currency"]), str(fields["fee_currency"])
    except KeyError, InvalidOperation:
        return head, f"fill {trade_id}"
    return head, f"{side} {quantity:f} {base} at {price:,f}, fee {_places(fee, _decimals(fee), 2):f} {fee_currency}"


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise InvalidOperation(f"not a decimal: {value!r}")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise InvalidOperation(f"not a finite decimal: {value!r}")
    return parsed


def _decimals(value: Decimal) -> int:
    """How many decimals ``value`` needs (none for a whole number)."""
    return max(0, -int(value.normalize().as_tuple().exponent))


def _places(value: Decimal, places: int, at_least: int = 0) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-max(places, at_least)))


def _step(value: Decimal, step: Decimal) -> Decimal:
    """``value`` with as many decimals as ``step`` has."""
    return _places(value, _decimals(step))


def span(seconds: int) -> str:
    """A configured length in plain words: "an hour", "2 hours", "10 min", or "45 s"."""
    if seconds == 3600:
        return "an hour"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hours"
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds} s"


def check(
    rule_id: str, subject: str | None, fields: Mapping[str, object], *, entry_block_s: int = 3600
) -> tuple[str, str]:
    """Title text and body for an episode opened by a condition check.

    ``entry_block_s`` is the configured block length (``ALERTS_ENTRY_BLOCK_S``) the
    ``entry_block`` title names.
    """
    if rule_id == "trader_down":
        return trader_down(fields)
    if rule_id == "service_down":
        return f"Service down: {subject}", f"No running {subject} container. State: {fields.get('state', 'none')}."
    if rule_id == "container_unhealthy":
        return f"Container unhealthy: {subject}", f"Docker reports {subject} unhealthy: {fields.get('status', '')}."
    if rule_id == "postgres_unreachable":
        return "Postgres unreachable", "The alerter cannot reach Postgres; alerts queue in memory."
    if rule_id == "redis_unreachable":
        return "Redis unreachable", "The alerter cannot reach Redis; trader health is unknown."
    if rule_id == "disk_low":
        return "Disk space low", (
            f"{fields.get('free_gb', '?')} GB free on the host root filesystem "
            f"(the floor is {fields.get('floor_gb', '?')} GB)."
        )
    if rule_id == "entry_block":
        reason = str(subject or fields.get("reason") or "")
        plain = PLAIN_WORDS.get(BlockReason(reason)) if reason in BlockReason else None
        return f"Entries blocked for over {span(entry_block_s)}", f"{plain or words(reason)} ({reason})."
    return words(rule_id), ""


SUMMARY_TITLES: Final = 10


def summary(kind: HoldKind, titles: Sequence[str]) -> tuple[str, str]:
    """Title text and body of the one push that ends a deploy window or a mute (AC-11, AC-12).

    It lists up to ``SUMMARY_TITLES`` titles (without their env tag), then "and N more".
    """
    count = len(titles)
    noun = "warning" if count == 1 else "warnings"
    head = f"{count} {noun} during deploy" if kind is HoldKind.DEPLOY else f"{count} {noun} while muted"
    lines = [re.sub(r"^\[[a-z]+\] ", "", t) for t in titles[:SUMMARY_TITLES]]
    if count > SUMMARY_TITLES:
        lines.append(f"and {count - SUMMARY_TITLES} more")
    return head, "\n".join(lines)


def storm(held: int, until: datetime) -> tuple[str, str]:
    """Title text and body of the storm push that replaces the hour's last allowed push (AC-10)."""
    return (
        "Alert storm",
        f"alert storm: {held} alerts held, see the dashboard. Warnings and info stay held until {_hhmm(until)} UTC.",
    )


def duration(start: datetime, end: datetime) -> str:
    """How long ``start`` to ``end`` took in plain words: seconds under a minute, else whole minutes."""
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    return f"{round(seconds / 60)} min"


def resolved(episode: Episode, at: datetime) -> tuple[str, str]:
    """Title and body of the silent note when ``episode`` resolves at ``at``."""
    base = episode.title.removeprefix(f"[{episode.env}] ")
    return title(episode.env, f"Resolved: {base}"), message(f"Resolved after {duration(episode.opened_at, at)}.")


def renotify(episode: Episode) -> str:
    """The body of a re-notification: the first body plus how often it was seen."""
    return message(
        f"{episode.message} Still open, seen {episode.occurrences} times since {_hhmm(episode.opened_at)} UTC."
    )


def marked_test(tier: Tier, command_id: str) -> tuple[str, str]:
    """Title text and body of one marked test push."""
    return f"TEST {tier.value} alert", f"Test push ({tier.value}) from command {command_id}. No action needed."


def _hhmm(at: datetime) -> str:
    return at.strftime("%H:%M")
