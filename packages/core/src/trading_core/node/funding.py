"""Demo funding as the trader books it (spec 0011 AC-13, AC-16, AC-17).

The ``RecordingActor`` books a payment at every hourly boundary a position is open across, then
publishes a ``FundingBooked`` on ``FUNDING_BOOKED_TOPIC`` for the ``LossLimitActor`` and adds it to
the ``FundingLedger`` the strategy's equity reads. The sandbox balance never sees funding.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

FUNDING_BOOKED_TOPIC: Final = "kfs.funding.booked"


@dataclass(frozen=True)
class FundingBooked:
    """One booked payment of one position; ``amount`` is negative when paid."""

    instrument_id: str
    paid_ns: int
    position_id: UUID
    amount: Decimal

    @property
    def key(self) -> tuple[str, int, UUID]:
        """What a payment is counted once by: ``(instrument_id, paid_ns, position row id)``."""
        return (self.instrument_id, self.paid_ns, self.position_id)


class FundingLedger:
    """The run's booked funding total, seeded from Postgres at start, then each booking added."""

    def __init__(self, seed: Decimal = Decimal(0)) -> None:
        self._total = seed

    @property
    def run_total(self) -> Decimal:
        """Every payment booked in this run so far (negative when paid)."""
        return self._total

    def add(self, amount: Decimal) -> None:
        """Add one booked payment."""
        self._total += amount
