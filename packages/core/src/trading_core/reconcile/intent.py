"""What the trader wants for each instrument right now (spec 0023 AC-18, *Wanted* on the position card), pure.

An intent starts when an order goes out and ends on that order's terminal event: an entry (not reduce
only) is ``entry_submitted``, an exit or a flatten (reduce only, not a stop) is ``flatten_submitted``,
and a protective stop changes nothing, because a resting stop is part of holding. The check's own stop
fix is ``stop_replacing`` from the moment it places the target stop until the old stops are cancelled.
An instrument with nothing in flight is ``hold``; when two intents overlap the newest one shows.
"""

from enum import StrEnum


class Intent(StrEnum):
    """The trader's latest intent for one instrument."""

    HOLD = "hold"
    ENTRY_SUBMITTED = "entry_submitted"
    FLATTEN_SUBMITTED = "flatten_submitted"
    STOP_REPLACING = "stop_replacing"


def order_intent(reduce_only: bool, is_stop: bool) -> Intent | None:
    """The intent an order going out starts: None for a protective stop, which changes nothing."""
    if not reduce_only:
        return Intent.ENTRY_SUBMITTED
    if is_stop:
        return None
    return Intent.FLATTEN_SUBMITTED


class Intents:
    """The open intents by key (a client order id, or the check's own stop fix), newest last."""

    def __init__(self) -> None:
        self._open: dict[str, tuple[str, Intent]] = {}
        self._seen: set[str] = set()

    def start(self, key: str, instrument_id: str, intent: Intent) -> None:
        """``intent`` on ``instrument_id`` until ``end(key)``; restarting a key makes it the newest."""
        self._open.pop(key, None)
        self._open[key] = (instrument_id, intent)
        self._seen.add(instrument_id)

    def end(self, key: str) -> None:
        """The order (or the stop fix) behind ``key`` is done; an unknown key is ignored."""
        self._open.pop(key, None)

    def by_instrument(self) -> dict[str, str]:
        """Every instrument with an intent so far, mapped to its newest open intent, ``hold`` when none."""
        shown = dict.fromkeys(sorted(self._seen), str(Intent.HOLD))
        for instrument_id, intent in self._open.values():
            shown[instrument_id] = str(intent)
        return shown
