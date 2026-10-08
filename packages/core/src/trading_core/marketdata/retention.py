"""Catalog retention (spec 0007 AC-6): which local hours to delete, and only once B2 holds them.

Pure: the compactor lists the hours with their sizes and newest file time, reads free space and
the backup state, and passes them in. Times are Nautilus nanosecond ints since the Unix epoch.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from trading_core.marketdata.errors import BackupStateInvalid
from trading_core.marketdata.hours import SECOND_NS

# How the backup writes the cutoff: `date -u +%Y-%m-%dT%H:%M:%SZ` (spec 0007, *Backup state*).
CUTOFF_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class BackupState:
    """A valid ``catalog_backup.json``: every catalog file older than ``cutoff_ns`` is in that snapshot."""

    snapshot_id: str
    cutoff_ns: int


def parse_backup_state(raw: object, now_ns: int) -> BackupState:
    """Validate the parsed JSON of the backup state file, or raise ``BackupStateInvalid``.

    Valid means an object with a non empty ``snapshot_id`` and a ``cutoff`` in ``CUTOFF_FORMAT``
    that is not after ``now_ns``: a future cutoff would let retention delete hours not yet backed up.
    """
    if not isinstance(raw, dict):
        raise BackupStateInvalid("not a JSON object")
    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise BackupStateInvalid("snapshot_id is missing or empty")
    cutoff = raw.get("cutoff")
    if not isinstance(cutoff, str):
        raise BackupStateInvalid("cutoff is missing")
    try:
        moment = datetime.strptime(cutoff, CUTOFF_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise BackupStateInvalid(f"cutoff {cutoff!r} is not YYYY-MM-DDTHH:MM:SSZ") from None
    cutoff_ns = int(moment.timestamp()) * SECOND_NS
    if cutoff_ns > now_ns:
        raise BackupStateInvalid(f"cutoff {cutoff} is in the future")
    return BackupState(snapshot_id=snapshot_id, cutoff_ns=cutoff_ns)


@dataclass(frozen=True)
class HourFiles:
    """One catalog hour on disk: all its ``data/`` files, plus its ``quarantine/`` files.

    ``data_bytes`` counts toward the cap; ``total_bytes`` (data plus quarantine) is what deleting
    the hour frees. ``newest_mtime_ns`` is the newest modification time of any of its files.
    """

    hour_ns: int
    data_bytes: int
    total_bytes: int
    newest_mtime_ns: int


@dataclass(frozen=True)
class RetentionPlan:
    """The hours to delete, oldest first, and the bytes still needed when it is blocked (0 if not)."""

    delete: tuple[int, ...]
    needed_bytes: int

    @property
    def blocked(self) -> bool:
        """Space is still needed, but the oldest hour left is not backed up yet."""
        return self.needed_bytes > 0


def plan_retention(
    hours: Sequence[HourFiles],
    cutoff_ns: int | None,
    *,
    catalog_bytes: int,
    free_bytes: int,
    cap_bytes: int,
    floor_bytes: int,
) -> RetentionPlan:
    """Delete the oldest whole hours while ``data/`` is over the cap or free space under the floor.

    Only an hour whose files are all older than the backup cutoff may go, and hours go strictly
    oldest first: when the oldest one left is not backed up, the plan stops there and reports the
    bytes still needed. No cutoff (no valid backup state) means nothing may go.
    """
    delete: list[int] = []
    for hour in sorted(hours, key=lambda h: h.hour_ns):
        if catalog_bytes <= cap_bytes and free_bytes >= floor_bytes:
            break
        if cutoff_ns is None or hour.newest_mtime_ns >= cutoff_ns:
            break
        delete.append(hour.hour_ns)
        catalog_bytes -= hour.data_bytes
        free_bytes += hour.total_bytes
    needed = max(catalog_bytes - cap_bytes, floor_bytes - free_bytes, 0)
    return RetentionPlan(delete=tuple(delete), needed_bytes=needed)
