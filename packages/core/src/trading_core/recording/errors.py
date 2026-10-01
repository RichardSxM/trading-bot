"""Typed errors raised by the recording write API (spec 0002).

Anything touching orders fails closed: the caller catches these, rolls back, and refuses
the action. ``RecordConflictError`` is also reported as a critical risk event.
"""

from collections.abc import Mapping


class RecordingError(Exception):
    """Base for every error the recording package raises."""


class RecordConflictError(RecordingError):
    """A row with this natural key exists with different content; the stored row is kept."""

    def __init__(
        self,
        table: str,
        key: Mapping[str, object],
        stored: Mapping[str, object],
        incoming: Mapping[str, object],
    ) -> None:
        self.table = table
        self.key = dict(key)
        self.stored = dict(stored)
        self.incoming = dict(incoming)
        differing = sorted(name for name in self.incoming if self.stored.get(name) != self.incoming.get(name))
        super().__init__(f"{table} {self.key} exists with different content in {differing}")


class EnvMismatchError(RecordingError):
    """A row's ``env`` differs from ``Settings.trading_env`` of the writing process."""


class MissingParentError(RecordingError):
    """A row references a parent (order, position) that cannot be found."""


class InvalidTransitionError(RecordingError):
    """A status change the state machine does not allow (for example finishing a finished run)."""


class InvalidModeError(RecordingError):
    """A run bundle with a mode other than ``backtest`` or ``replay``."""


class ForeignRunError(RecordingError):
    """A run bundle row whose ``run_id`` or ``env`` is not the bundle's own run."""


class MissingEvidenceError(RecordingError):
    """A promotion with no evidence run, or one citing a run of another strategy version."""


class PromotionRequiredError(RecordingError):
    """A live run for a version whose latest ``paper → live`` promotion is missing or failed."""


class UnknownCommitError(RecordingError):
    """A live run for a version recorded with ``git_commit = 'unknown'``."""


class UnsupportedInstrumentError(RecordingError):
    """A journal range holds a fill on an inverse instrument; only linear contracts are supported."""
