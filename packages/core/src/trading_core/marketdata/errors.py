"""Typed errors of the catalog compactor (spec 0007). The compactor logs each one and fails closed."""


class CatalogError(Exception):
    """Base for every catalog compactor error."""


class HourCountMismatch(CatalogError):
    """A written Parquet file holds a different row count than the hour's source (AC-2)."""

    def __init__(self, hour: int, expected: int, written: int) -> None:
        super().__init__(f"hour {hour}: expected {expected} rows, the Parquet file holds {written}")
        self.hour = hour
        self.expected = expected
        self.written = written


class FeatherUnreadable(CatalogError):
    """A feather file failed to read before its end, so it is not a torn tail and rows would be lost (AC-2)."""

    def __init__(self, path: str, position: int, size: int, reason: str) -> None:
        super().__init__(f"{path}: read failed at byte {position} of {size}: {reason}")
        self.path = path
        self.position = position
        self.size = size
        self.reason = reason


class BackupStateInvalid(CatalogError):
    """The backup state file is unreadable or wrong, so no catalog hour counts as backed up (AC-6)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"backup state invalid: {reason}")
        self.reason = reason


class BridgeRefused(CatalogError):
    """The laptop backlog's bridge gap cannot be written yet, so nothing was changed (AC-9)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"bridge refused: {reason}")
        self.reason = reason
