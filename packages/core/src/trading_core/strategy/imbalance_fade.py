"""``imbalance_fade``'s signal (spec 0021): ``book_imbalance``'s signal from the other side.

The same lopsided book, persistence, and trade flow test as ``BookImbalanceState``; when that rule would
go long, this one goes short, and the mirror. The stop and the target sit ``stop_bps`` and ``target_bps``
from the same mid, on the faded side.
"""

from collections.abc import Sequence
from decimal import Decimal

from trading_core.recording.enums import OrderSide
from trading_core.strategy.book_imbalance import BookImbalanceState
from trading_core.strategy.scalp import BPS, ScalpSignal


class ImbalanceFadeState(BookImbalanceState):
    """``BookImbalanceState`` with each signal's side, stop, and target mirrored (AC-2)."""

    def on_firing(
        self, bucket: int, bid_sizes: Sequence[Decimal], ask_sizes: Sequence[Decimal], mid: Decimal | None
    ) -> ScalpSignal | None:
        """The parent rule's signal at ``bucket``, faded; no signal stays no signal."""
        parent = super().on_firing(bucket, bid_sizes, ask_sizes, mid)
        if parent is None:
            return None
        at = parent.inputs["mid"]
        if not isinstance(at, Decimal):
            raise TypeError(f"the parent signal's mid must be a Decimal, not {type(at).__name__}")
        stop, target = self._stop_bps / BPS, self._target_bps / BPS
        if parent.side == OrderSide.BUY:
            return ScalpSignal(OrderSide.SELL, at * (1 + stop), at * (1 - target), parent.inputs)
        return ScalpSignal(OrderSide.BUY, at * (1 - stop), at * (1 + target), parent.inputs)
