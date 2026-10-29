"""The entry orders Kraken has not answered (spec 0023 AC-14), pure: no Nautilus import.

An entry (an order that is not reduce only) is unresolved from its submit until Kraken answers it:
an acknowledgement, a fill, a cancel, an expiry, or a real rejection. Nautilus's in flight check
gives up on an order with no answer by rejecting it itself, with one of ``INFLIGHT_REJECTS`` as the
reason; such an order may still rest on Kraken, so it stays unresolved until the next clean check
proves Kraken holds nothing we do not know. While any entry is unresolved, entries are blocked
with ``order_unresolved``.
"""

from typing import Final

INFLIGHT_REJECTS: Final = frozenset({"UNKNOWN", "ORDER_NOT_FOUND_AT_VENUE"})
"""The reasons Nautilus 1.231.0's in flight check (and its venue lookup) rejects an order with."""


class UnresolvedEntries:
    """The entry orders submitted with no answer, and those the in flight check rejected, by client order id."""

    def __init__(self) -> None:
        self._waiting: dict[str, str] = {}
        self._rejected: dict[str, str] = {}

    @property
    def active(self) -> bool:
        """True while any entry is unresolved."""
        return bool(self._waiting or self._rejected)

    @property
    def instruments(self) -> frozenset[str]:
        """The instruments with an unresolved entry."""
        return frozenset(self._waiting.values()) | frozenset(self._rejected.values())

    def submitted(self, client_order_id: str, instrument_id: str) -> None:
        """An entry went out (or was found in flight in the cache at start)."""
        self._waiting[client_order_id] = instrument_id

    def answered(self, client_order_id: str) -> None:
        """Kraken answered the entry: it is resolved."""
        self._waiting.pop(client_order_id, None)

    def rejected(self, client_order_id: str, reason: str) -> None:
        """The entry was rejected: resolved when Kraken rejected it, unresolved when the in flight check did."""
        instrument_id = self._waiting.pop(client_order_id, None)
        if instrument_id is not None and reason in INFLIGHT_REJECTS:
            self._rejected[client_order_id] = instrument_id

    def clear(self) -> None:
        """A clean check: Kraken holds nothing we do not know, so nothing is unresolved."""
        self._waiting.clear()
        self._rejected.clear()
