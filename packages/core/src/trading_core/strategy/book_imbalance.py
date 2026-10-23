"""``book_imbalance``'s signal (spec 0017 child 0004): join a lopsided book when trade flow agrees.

On each 15 second firing that closes bucket ``k``: ``B`` and ``A`` are the summed sizes of the top
``levels`` bid and ask levels, ``imb = (B - A) / (B + A)``, and ``flow`` is bucket ``k``'s signed traded
size (0 when it had no trades). A long when ``imb >= imb_min`` on each of the last ``persist_bars``
firings and ``flow > 0``; a short the mirror. An empty book side gives no imbalance value, so the
persistence count starts again. The stop and the target sit ``stop_bps`` and ``target_bps`` from the
last quote's mid at the signal.
"""

from collections import deque
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from trading_core.recording.enums import OrderSide
from trading_core.strategy.scalp import BPS, ScalpSignal
from trading_core.strategy.scalp_indicators import DeltaWindow

BUCKET_NS: Final = 15_000_000_000


class BookImbalanceState:
    """The flow buckets and the recent imbalance values the rule reads."""

    def __init__(
        self, *, imb_min: Decimal, persist_bars: int, levels: int, stop_bps: Decimal, target_bps: Decimal
    ) -> None:
        if not 0 < imb_min < 1 or persist_bars < 1 or levels < 1 or stop_bps <= 0 or target_bps <= 0:
            raise ValueError(
                "imb_min must be above 0 and below 1, persist_bars and levels at least 1, "
                "stop_bps and target_bps above 0"
            )
        self._imb_min = imb_min
        self._persist = persist_bars
        self._levels = levels
        self._stop_bps = stop_bps
        self._target_bps = target_bps
        self.window = DeltaWindow(1, BUCKET_NS)
        self._imbalances: deque[Decimal] = deque(maxlen=persist_bars)

    def on_trade(self, ts_event_ns: int, size: Decimal, aggressor: str) -> None:
        """One trade tick (``aggressor``: ``buy``, ``sell``, or ``none``) into its ``ts_event`` bucket."""
        self.window.add(ts_event_ns, size, aggressor)

    def on_firing(
        self, bucket: int, bid_sizes: Sequence[Decimal], ask_sizes: Sequence[Decimal], mid: Decimal | None
    ) -> ScalpSignal | None:
        """Close ``bucket`` with the book's level sizes (best first) and the last quote's mid; return the signal."""
        delta = self.window.close(bucket)
        flow = delta.delta if delta is not None else Decimal(0)
        bid_depth = sum(bid_sizes[: self._levels], Decimal(0))
        ask_depth = sum(ask_sizes[: self._levels], Decimal(0))
        if bid_depth <= 0 or ask_depth <= 0:
            self._imbalances.clear()
            return None
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        self._imbalances.append(imbalance)
        if mid is None or len(self._imbalances) < self._persist:
            return None
        inputs: dict[str, Decimal | int | str | None] = {
            "imbalance": imbalance,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "flow": flow,
            "mid": mid,
        }
        stop, target = self._stop_bps / BPS, self._target_bps / BPS
        if all(i >= self._imb_min for i in self._imbalances) and flow > 0:
            return ScalpSignal(OrderSide.BUY, mid * (1 - stop), mid * (1 + target), inputs)
        if all(i <= -self._imb_min for i in self._imbalances) and flow < 0:
            return ScalpSignal(OrderSide.SELL, mid * (1 + stop), mid * (1 - target), inputs)
        return None
