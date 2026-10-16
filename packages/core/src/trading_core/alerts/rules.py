"""The alert rule table (spec 0013, *Rule table*), in precedence order.

Milestone 1 holds the heartbeat rule and the internal rules; the log, host, and Docker rules join
it with their sources. Pure: no I/O.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from trading_core.alerts.model import RuleKind, Tier


@dataclass(frozen=True)
class Rule:
    """One row of the rule table.

    ``min_open_s`` holds the first push until the episode has been open that long; an episode
    that resolves sooner is stored and never pushed. ``source`` is the ``alerts.source`` value.
    """

    id: str
    tier: Tier
    kind: RuleKind
    source: str
    min_open_s: int = 0

    def fingerprint(self, subject: str | None = None) -> str:
        """The episode fingerprint: the rule id, plus ``:<subject>`` when the rule has several subjects."""
        return self.id if subject is None else f"{self.id}:{subject}"


TRADER_DOWN: Final = Rule("trader_down", Tier.CRITICAL, RuleKind.CONDITION, source="redis")
ALERT_STORM: Final = Rule("alert_storm", Tier.WARNING, RuleKind.INTERNAL, source="self")
SUMMARY: Final = Rule("summary", Tier.WARNING, RuleKind.INTERNAL, source="self")
# A test push takes the tier it tests; ``tier`` here is only the default.
TEST: Final = Rule("test", Tier.INFO, RuleKind.INTERNAL, source="self")

RULES: Final[tuple[Rule, ...]] = (TRADER_DOWN, ALERT_STORM, SUMMARY, TEST)

BY_ID: Final[Mapping[str, Rule]] = MappingProxyType({rule.id: rule for rule in RULES})
