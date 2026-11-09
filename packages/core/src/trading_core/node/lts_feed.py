"""The liquidity trap scalper's feed layer on Nautilus (spec 0031 AC-3)."""

from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide as NautilusAggressorSide

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.lts_errors import MarketDataIntegrityError
from trading_core.marketdata.normalized import NormalizedTrade, TradeKind

_AGGRESSORS: Final = {
    NautilusAggressorSide.BUYER: AggressorSide.BUY,
    NautilusAggressorSide.SELLER: AggressorSide.SELL,
}


def normalized_trade(tick: TradeTick, contract_size: Decimal, kind: TradeKind = TradeKind.FILL) -> NormalizedTrade:
    """A ``TradeTick`` as a ``NormalizedTrade``; a tick with no aggressor raises ``MarketDataIntegrityError``.

    ``TradeTick`` has no type, so ``kind`` stays ``fill`` unless the caller already knows better.
    """
    aggressor = _AGGRESSORS.get(tick.aggressor_side)
    if aggressor is None:
        raise MarketDataIntegrityError(f"trade {tick.trade_id} has no aggressor side")
    price = tick.price.as_decimal()
    size = tick.size.as_decimal()
    return NormalizedTrade(
        instrument_id=tick.instrument_id.value,
        trade_id=tick.trade_id.value,
        ts_event_ns=tick.ts_event,
        ts_init_ns=tick.ts_init,
        price=price,
        size=size,
        notional=price * size * contract_size,
        aggressor=aggressor,
        kind=kind,
    )
