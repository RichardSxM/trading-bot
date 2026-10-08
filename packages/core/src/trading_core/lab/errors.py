"""Typed errors of the replay lab (spec 0008). Each one fails the variant it happens in (AC-13)."""


class LabError(Exception):
    """Base of every lab error."""


class UnexpectedSnapshotError(LabError):
    """A ``CLEAR`` or snapshot row inside a segment after its first batch (AC-4).

    The engine would move every resting order to the front of its queue, so the replay refuses.
    """


class RecordingFailedError(LabError):
    """Mapping an engine event to a row failed inside a unit, so its bundle would be incomplete."""


class FundingRateMissingError(LabError):
    """A funding boundary inside a segment has no ``funding_rate_update`` at or before it (AC-9)."""
