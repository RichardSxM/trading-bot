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
