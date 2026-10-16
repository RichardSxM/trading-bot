"""Closed hours that miss a context file (spec 0013 AC-7, spec 0009 AC-17). Pure: no I/O.

Spec 0009 AC-17 expects every closed hour to have a file for each steady context table. An hour
is checked once a later book hour is finished, so the context streams, which close on the same
grace, have had a whole hour to catch up. An hour the feed could not have filled is skipped: one
wholly inside a ``recorder_down`` gap for every table, and one wholly inside a context feed
outage for the raw feed's tables.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class MissingContextHour:
    """One closed hour with no file for ``table``."""

    hour_ns: int
    table: str

    @property
    def label(self) -> str:
        """The hour as ``2026-10-16T03:00Z`` (the alert's fingerprint subject)."""
        return datetime.fromtimestamp(self.hour_ns // 1_000_000_000, UTC).strftime("%Y-%m-%dT%H:00Z")


def missing_context_hours(
    *,
    book_hours: Collection[int],
    context_hours: Mapping[str, Collection[int]],
    skip: Mapping[str, Collection[int]],
    checked_to: int | None,
) -> tuple[list[MissingContextHour], int | None]:
    """The missing context files among the book hours not checked yet, and the new ``checked_to``.

    ``book_hours`` are the instrument's finished book hours; ``context_hours`` maps each expected
    table to its finished hours; ``skip`` maps a table to hours it is excused from. Hours up to
    ``checked_to`` were checked before. With ``checked_to`` None (the first check), nothing is
    reported and every ready hour counts as checked, so hours from before this check never alert.
    """
    if not book_hours:
        return [], checked_to
    newest = max(book_hours)
    ready = sorted(h for h in book_hours if h < newest and (checked_to is None or h > checked_to))
    if not ready:
        return [], checked_to
    if checked_to is None:
        return [], ready[-1]
    missing = [
        MissingContextHour(hour, table)
        for hour in ready
        for table, hours in sorted(context_hours.items())
        if hour not in hours and hour not in skip.get(table, ())
    ]
    return missing, ready[-1]
