"""The pre check pass rules (spec 0033 AC-12, AC-12a), from each run's Sharpe per half.

Families and roles come from the pre registration file. ``ewma`` and ``tranche`` pass when their one main
run beats the baseline reference (the mean of the baseline's Sharpe at its cut hours, per half) in both
halves and no neighbour is below it in either half; ``tranche`` must also cut timing luck: the spread
(highest minus lowest full window Sharpe) of its main run and its offset runs under half the baseline's
spread across its cut hours. ``scale`` (AC-12a) passes when every main run beats the baseline at its own
cut hour in both halves and no neighbour is below the baseline at 00 in either half; its control decides
nothing. A Sharpe that could not be computed fails the check it is in. No I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

FAMILIES: Final = ("ewma", "tranche", "scale")
ROLES: Final = ("main", "neighbour", "offset", "control")


@dataclass(frozen=True)
class RunFigures:
    """One run's name, family, role, cut hour, and Sharpe in the first half, second half, and full window."""

    name: str
    family: str
    role: str
    cut_hour: int
    sharpe_h1: Decimal | None
    sharpe_h2: Decimal | None
    sharpe_full: Decimal | None


@dataclass(frozen=True)
class Verdict:
    """A family's result and the reasons, one line per check."""

    family: str
    passed: bool
    reasons: tuple[str, ...]


def _fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value.quantize(Decimal("0.001")))


def _beats(run: RunFigures, ref: tuple[Decimal, Decimal], strict: bool) -> bool:
    if run.sharpe_h1 is None or run.sharpe_h2 is None:
        return False
    if strict:
        return run.sharpe_h1 > ref[0] and run.sharpe_h2 > ref[1]
    return run.sharpe_h1 >= ref[0] and run.sharpe_h2 >= ref[1]


def _line(run: RunFigures, ref: tuple[Decimal, Decimal], ok: bool, what: str) -> str:
    return (
        f"{run.name} {what}: H1 {_fmt(run.sharpe_h1)} vs {_fmt(ref[0])}, H2 {_fmt(run.sharpe_h2)} vs "
        f"{_fmt(ref[1])}, {'ok' if ok else 'FAIL'}"
    )


def _spread(values: Sequence[Decimal | None]) -> Decimal | None:
    if not values or any(v is None for v in values):
        return None
    known = [v for v in values if v is not None]
    return max(known) - min(known)


def reference(baselines: Mapping[int, RunFigures]) -> tuple[Decimal, Decimal] | None:
    """The mean baseline Sharpe per half over every cut hour, or None when one is missing."""
    h1 = [b.sharpe_h1 for b in baselines.values()]
    h2 = [b.sharpe_h2 for b in baselines.values()]
    if not baselines or any(v is None for v in (*h1, *h2)):
        return None
    k1 = [v for v in h1 if v is not None]
    k2 = [v for v in h2 if v is not None]
    return sum(k1, Decimal(0)) / len(k1), sum(k2, Decimal(0)) / len(k2)


def precheck_verdicts(baselines: Mapping[int, RunFigures], runs: Sequence[RunFigures]) -> list[Verdict]:
    """Every family's verdict (module docstring). Raises ``ValueError`` for an unknown family or role, or an
    ``ewma`` or ``tranche`` family without exactly one main run."""
    for run in runs:
        if run.family not in FAMILIES or run.role not in ROLES:
            raise ValueError(f"{run.name}: unknown family {run.family} or role {run.role}")
    ref = reference(baselines)
    verdicts: list[Verdict] = []
    for family in FAMILIES:
        members = [r for r in runs if r.family == family]
        if not members:
            continue
        mains = [r for r in members if r.role == "main"]
        neighbours = [r for r in members if r.role == "neighbour"]
        reasons: list[str] = []
        passed = True
        if family in ("ewma", "tranche"):
            if len(mains) != 1:
                raise ValueError(f"{family} needs exactly one main run, got {len(mains)}")
            if ref is None:
                verdicts.append(Verdict(family, False, ("the baseline reference could not be computed",)))
                continue
            ok = _beats(mains[0], ref, strict=True)
            passed &= ok
            reasons.append(_line(mains[0], ref, ok, "beats the reference"))
            for run in neighbours:
                ok = _beats(run, ref, strict=False)
                passed &= ok
                reasons.append(_line(run, ref, ok, "not below the reference"))
            if family == "tranche":
                offsets = [mains[0], *(r for r in members if r.role == "offset")]
                mine = _spread([r.sharpe_full for r in offsets])
                theirs = _spread([b.sharpe_full for b in baselines.values()])
                ok = mine is not None and theirs is not None and mine < theirs / 2
                passed &= ok
                reasons.append(
                    f"offset spread {_fmt(mine)} over {len(offsets)} runs vs half the baseline spread "
                    f"{_fmt(None if theirs is None else theirs / 2)}, {'ok' if ok else 'FAIL'}"
                )
        else:
            if not mains:
                raise ValueError("scale needs at least one main run")
            for run in mains:
                base = baselines.get(run.cut_hour)
                if base is None or base.sharpe_h1 is None or base.sharpe_h2 is None:
                    passed = False
                    reasons.append(f"{run.name}: no baseline at cut hour {run.cut_hour}, FAIL")
                    continue
                own = (base.sharpe_h1, base.sharpe_h2)
                ok = _beats(run, own, strict=True)
                passed &= ok
                reasons.append(_line(run, own, ok, f"beats the baseline at {run.cut_hour:02d}"))
            base0 = baselines.get(0)
            for run in neighbours:
                if base0 is None or base0.sharpe_h1 is None or base0.sharpe_h2 is None:
                    passed = False
                    reasons.append(f"{run.name}: no baseline at 00, FAIL")
                    continue
                own = (base0.sharpe_h1, base0.sharpe_h2)
                ok = _beats(run, own, strict=False)
                passed &= ok
                reasons.append(_line(run, own, ok, "not below the baseline at 00"))
            for run in (r for r in members if r.role == "control"):
                reasons.append(
                    f"{run.name} (control, decides nothing): H1 {_fmt(run.sharpe_h1)}, H2 {_fmt(run.sharpe_h2)}"
                )
        verdicts.append(Verdict(family, passed, tuple(reasons)))
    return verdicts
