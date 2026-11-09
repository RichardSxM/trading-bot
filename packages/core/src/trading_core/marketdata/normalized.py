"""One normalized trade (spec 0031 AC-3).

Kraken's ``side`` on a trade row is the taker's side. A row's ``type`` says whether it was a plain
fill or a forced or block trade. A Nautilus ``TradeTick`` has no type, so its converter
(``trading_core.node.lts_feed``) takes the kind from what is already known about its id.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.context_feed import RECENT_IDS, RecentIds
from trading_core.marketdata.lts_errors import MarketDataIntegrityError


class TradeKind(StrEnum):
    """What a trade row was."""

    FILL = "fill"
    LIQUIDATION = "liquidation"
    TERMINATION = "termination"
    BLOCK = "block"


_KINDS: Final = {kind.value: kind for kind in TradeKind} | {"partialLiquidation": TradeKind.LIQUIDATION}
_SIDES: Final = {"buy": AggressorSide.BUY, "sell": AggressorSide.SELL}


def parse_trade_kind(raw: str) -> TradeKind:
    """The kind of a Kraken trade ``type``; anything unknown raises ``MarketDataIntegrityError``."""
    kind = _KINDS.get(raw)
    if kind is None:
        raise MarketDataIntegrityError(f"unknown trade type: {raw!r}")
    return kind


def parse_taker_side(raw: str) -> AggressorSide:
    """The aggressor of a Kraken trade ``side``, which is the taker's side."""
    side = _SIDES.get(raw)
    if side is None:
        raise MarketDataIntegrityError(f"unknown trade side: {raw!r}")
    return side


@dataclass(frozen=True)
class NormalizedTrade:
    """One trade. ``size`` is the raw venue size and ``notional`` its quote value."""

    instrument_id: str
    trade_id: str
    ts_event_ns: int
    ts_init_ns: int
    price: Decimal
    size: Decimal
    notional: Decimal
    aggressor: AggressorSide
    kind: TradeKind


class TradeDeduper:
    """Lets each trade id through once, including one seen in both a ``trade_snapshot`` and the live stream."""

    def __init__(self, limit: int = RECENT_IDS) -> None:
        self._ids = RecentIds(limit)

    def first_seen(self, trade: NormalizedTrade) -> bool:
        """True the first time this trade's id is seen, False for a repeat."""
        return self._ids.add(trade.trade_id)
