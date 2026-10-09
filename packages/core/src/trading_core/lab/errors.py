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


class UnitFailedError(LabError):
    """An exception escaped a unit in its worker process (AC-13).

    The worker sends only the exception's type name and message, so a type the parent cannot
    import still reaches the report; ``error_type`` names the original exception.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class WorkerDiedError(LabError):
    """A worker process ended without sending its unit's result (killed, or out of memory) (AC-13)."""

    def __init__(self, exitcode: int | None) -> None:
        super().__init__(f"the worker process died with exit code {exitcode}")
        self.exitcode = exitcode


class ReplayInterruptedError(LabError):
    """SIGINT or SIGTERM reached the parent during a replay (AC-13).

    Running units stop, and nothing incomplete is written.
    """
