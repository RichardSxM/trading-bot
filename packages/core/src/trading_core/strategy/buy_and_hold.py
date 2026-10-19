"""``buy_and_hold``'s pure rules (spec 0014 AC-12): the size of the one buy, and the attempt cap.

It buys ``notional_usd`` worth at the first bar close, floored to the size step, and holds. A refused
entry is tried again at the next bar close, at most ``max_entry_attempts`` times; then it gives up for
the run. A position closed from any source resets the count, so it may buy again.
"""

from dataclasses import dataclass
from decimal import Decimal

from trading_core.risk.sizing import entry_quantity


def hold_quantity(notional_usd: Decimal, mid: Decimal, size_step: Decimal) -> Decimal:
    """``notional_usd / mid`` floored to the size step (zero below one step)."""
    return entry_quantity(notional_usd, mid, size_step)


@dataclass
class EntryAttempts:
    """How many entries ``check_entry`` refused, against the cap."""

    max_attempts: int
    refused: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_entry_attempts must be at least 1, got {self.max_attempts}")

    @property
    def abandoned(self) -> bool:
        """True once the cap is reached: no more entries this run (until a reset)."""
        return self.refused >= self.max_attempts

    def refuse(self) -> bool:
        """Count one refusal. True when this one reached the cap (log ``entry_abandoned`` once)."""
        self.refused += 1
        return self.refused == self.max_attempts

    def reset(self) -> None:
        """Start counting again after a position closed."""
        self.refused = 0
