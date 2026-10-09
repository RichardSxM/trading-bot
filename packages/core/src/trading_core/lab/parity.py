"""The parity check's pure rules (spec 0008 AC-15): the paper config, which hours count, and the matching.

``lab parity`` replays ``placeholder_timed`` with the paper loop's config over the clean own hours
of the paper runs whose config hash equals the run file's, and compares the replay's ``enter``
decisions with the paper ones. Paper refusals for ``paused`` or ``blocked:*`` are excluded
boundaries: paper was blocked there for reasons the replay cannot see, so the boundary is left
out on both sides. ``hold_min`` is below ``interval_min``, so a refused boundary affects only itself.

Times are Nautilus nanosecond ints (UTC).
"""

import hashlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from trading_core.lab.errors import LabError
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import Intent
from trading_core.recording.rows import JsonObject, canonical_json
from trading_core.risk.check import BLOCKED_PREFIX

NS_PER_S: Final = 1_000_000_000
NS_PER_MIN: Final = 60 * NS_PER_S
# Two decisions of one boundary are at most this far apart (AC-15).
MATCH_NS: Final = NS_PER_S
THRESHOLD: Final = Decimal("0.95")
PAUSED: Final = "paused"

type PaperValue = int | Decimal
type Kind = Literal["int", "decimal"]

# The trader's ``strategy_config()`` keys (spec 0006 ``PaperLoopSettings``), in the order of the settings.
PAPER_KEYS: Final[Mapping[str, Kind]] = {
    "placeholder_interval_min": "int",
    "placeholder_hold_min": "int",
    "placeholder_lookback_min": "int",
    "placeholder_stop_pct": "decimal",
    "placeholder_notional_usd": "decimal",
    "risk_max_order_notional_usd": "decimal",
    "risk_max_leverage": "decimal",
    "risk_stale_quote_s": "decimal",
}


class ParityInputError(LabError):
    """The ``[paper]`` table or the paper CSV cannot be used; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class PaperStrategy:
    """The paper loop's placeholder and risk settings, read from a ``[paper]`` table."""

    interval_min: int
    hold_min: int
    lookback_min: int
    stop_pct: Decimal
    notional_usd: Decimal
    max_order_notional: Decimal
    max_leverage: Decimal
    stale_quote_s: Decimal

    @property
    def warmup_ns(self) -> int:
        """How long after a segment start no boundary counts: ``lookback_min + hold_min`` minutes."""
        return (self.lookback_min + self.hold_min) * NS_PER_MIN


def _value(key: str, kind: Kind, raw: object) -> PaperValue | str:
    """The checked value, or a problem as text."""
    if kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return f"{key}: must be a whole number above 0, like 15"
        return raw
    if not isinstance(raw, str):
        return f'{key}: must be a string, like "0.3"'
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return f"{key}: {raw!r} is not a number"
    if not number.is_finite() or number <= 0:
        return f"{key}: must be above 0, got {raw!r}"
    return number


def paper_config(table: Mapping[str, object]) -> PaperStrategy:
    """The ``PaperStrategy`` a ``[paper]`` table gives.

    The table holds exactly the trader's ``strategy_config()`` keys: whole numbers for the
    ``*_min`` settings, decimal strings for the rest. Raises ``ParityInputError`` listing every
    missing, unknown, or bad key, and the paper loop's own timing rules (``interval_min`` divides
    60, ``hold_min`` is below it).
    """
    problems = [f"{key}: not a paper setting (known: {sorted(PAPER_KEYS)})" for key in table if key not in PAPER_KEYS]
    problems += [f"{key}: missing" for key in PAPER_KEYS if key not in table]
    values: dict[str, PaperValue] = {}
    for key, kind in PAPER_KEYS.items():
        if key in table:
            checked = _value(key, kind, table[key])
            if isinstance(checked, str):
                problems.append(checked)
            else:
                values[key] = checked
    if problems:
        raise ParityInputError(sorted(problems))
    strategy = PaperStrategy(
        interval_min=int(values["placeholder_interval_min"]),
        hold_min=int(values["placeholder_hold_min"]),
        lookback_min=int(values["placeholder_lookback_min"]),
        stop_pct=Decimal(values["placeholder_stop_pct"]),
        notional_usd=Decimal(values["placeholder_notional_usd"]),
        max_order_notional=Decimal(values["risk_max_order_notional_usd"]),
        max_leverage=Decimal(values["risk_max_leverage"]),
        stale_quote_s=Decimal(values["risk_stale_quote_s"]),
    )
    if 60 % strategy.interval_min:
        problems.append("placeholder_interval_min: must divide 60")
    if strategy.hold_min >= strategy.interval_min:
        problems.append("placeholder_hold_min: must be below placeholder_interval_min")
    if strategy.stop_pct >= 100:
        problems.append("placeholder_stop_pct: must be below 100")
    if problems:
        raise ParityInputError(problems)
    return strategy


def config_sha256(config: JsonObject) -> str:
    """sha256 hex of ``canonical_json(config)``: the hash both sides of the config comparison use."""
    return hashlib.sha256(canonical_json(config).encode()).hexdigest()


def differing_keys(a: JsonObject, b: JsonObject) -> list[str]:
    """The keys whose values differ between two configs (a key in only one counts), sorted."""
    return sorted(key for key in a.keys() | b.keys() if canonical_json(a.get(key)) != canonical_json(b.get(key)))


@dataclass(frozen=True)
class PaperRun:
    """One paper run of ``placeholder_timed``: its window and the config it ran with."""

    run_id: str
    started_ns: int
    ended_ns: int
    config: JsonObject
    config_sha256: str


@dataclass(frozen=True)
class Decided:
    """One decision, paper or replay, as the comparison reads it."""

    decided_ns: int
    intent: str
    side: str | None
    approved: bool
    risk_reason: str | None = None

    @property
    def blocked(self) -> bool:
        """A refusal for ``paused`` or ``blocked:*``: paper was not free to enter here."""
        reason = self.risk_reason or ""
        return not self.approved and (reason == PAUSED or reason.startswith(BLOCKED_PREFIX))


def eligible_hours(hours: Iterable[int], runs: Iterable[PaperRun], run_sha256: str) -> list[int]:
    """The hours that lie wholly inside a run whose config hash is ``run_sha256``, in order."""
    windows = [(r.started_ns, r.ended_ns) for r in runs if r.config_sha256 == run_sha256]
    return sorted(h for h in hours if any(start <= h and h + HOUR_NS <= end for start, end in windows))


@dataclass(frozen=True)
class Window:
    """A stretch where boundaries count: a segment, after its warm up."""

    start_ns: int
    end_ns: int

    def holds(self, ns: int) -> bool:
        """True when ``ns`` lies in the window, both ends included."""
        return self.start_ns <= ns <= self.end_ns


def counted_windows(segments: Iterable[tuple[int, int]], warmup_ns: int) -> list[Window]:
    """Each segment ``(start_ns, end_ns)`` without its first ``warmup_ns``; a segment shorter than that has none."""
    return [Window(start + warmup_ns, end) for start, end in segments if start + warmup_ns <= end]


@dataclass(frozen=True)
class ParityResult:
    """The comparison: matched pairs, what was compared, what was left out, and every unmatched decision."""

    matches: int
    paper: int
    replay: int
    excluded: tuple[Decided, ...]
    unmatched_paper: tuple[Decided, ...]
    unmatched_replay: tuple[Decided, ...]

    @property
    def ratio(self) -> Decimal | None:
        """Matches ÷ max(paper, replay); None when there is no decision left to compare."""
        most = max(self.paper, self.replay)
        return None if most == 0 else Decimal(self.matches) / Decimal(most)

    @property
    def passed(self) -> bool:
        """True when the ratio is at least 0.95."""
        ratio = self.ratio
        return ratio is not None and ratio >= THRESHOLD


def _inside(decisions: Iterable[Decided], windows: Collection[Window]) -> list[Decided]:
    return sorted(
        (d for d in decisions if d.intent == Intent.ENTER and any(w.holds(d.decided_ns) for w in windows)),
        key=lambda d: d.decided_ns,
    )


def compare(paper: Iterable[Decided], replay: Iterable[Decided], windows: Sequence[Window]) -> ParityResult:
    """Match the ``enter`` decisions inside ``windows`` (AC-15).

    A pair matches when both have the same side and decide within 1 s of each other, approved or
    rejected. Each decision matches at most once, earliest first. Paper refusals for ``paused`` or
    ``blocked:*`` are left out, together with every replay decision within 1 s of one.
    """
    paper_in = _inside(paper, windows)
    excluded = [d for d in paper_in if d.blocked]
    compared_paper = [d for d in paper_in if not d.blocked]
    compared_replay = [
        d for d in _inside(replay, windows) if not any(abs(d.decided_ns - x.decided_ns) <= MATCH_NS for x in excluded)
    ]
    used: set[int] = set()
    unmatched_paper: list[Decided] = []
    for p in compared_paper:
        found = next(
            (
                i
                for i, r in enumerate(compared_replay)
                if i not in used and r.side == p.side and abs(r.decided_ns - p.decided_ns) <= MATCH_NS
            ),
            None,
        )
        if found is None:
            unmatched_paper.append(p)
        else:
            used.add(found)
    return ParityResult(
        matches=len(used),
        paper=len(compared_paper),
        replay=len(compared_replay),
        excluded=tuple(excluded),
        unmatched_paper=tuple(unmatched_paper),
        unmatched_replay=tuple(r for i, r in enumerate(compared_replay) if i not in used),
    )
