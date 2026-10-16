"""Alert titles and bodies (spec 0013): the env tag, plain words, Pushover's length limits, redaction.

Every text leaving here has passed ``trading_core.secrets.redact``. Pure apart from reading the
redaction registry.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from trading_core.alerts.model import Episode, Tier
from trading_core.secrets import redact

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
