"""Cache readers that feed the pure risk guard (spec 0011 AC-4, AC-6, AC-17).

Every open position counts, whatever its instrument. A position whose instrument has no quote
makes the inputs unjudgeable, so these raise ``InvalidRiskInput`` and the caller fails closed.
"""

from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.instruments import Instrument

from trading_core.node.strategy import KfsStrategyConfig
from trading_core.risk.check import InvalidRiskInput, RiskLimits


class GuardedConfig(KfsStrategyConfig, frozen=True, kw_only=True):
    """The ``RISK_*`` caps and sizing rules every guarded strategy's config carries (spec 0011 AC-19).

    ``taker_fee`` is the fee the guard sizes with, a fraction of notional: the fee the venue
    charges. Never the cached instrument's, which Kraken publishes as 0 (the sandbox puts its fee
    only on its own exchange's copy).
    """

    max_order_notional_usd: Decimal
    max_leverage: Decimal
    max_total_exposure_usd: Decimal
    risk_per_trade: Decimal
    maintenance_margin_rate: Decimal
    min_liq_distance_pct: Decimal
    liq_stop_multiple: Decimal
    stop_slippage_bps: Decimal
    taker_fee: Decimal

    def risk_limits(self) -> RiskLimits:
        """These settings as the pure guard's ``RiskLimits``."""
        return RiskLimits(
            max_order_notional=self.max_order_notional_usd,
            max_leverage=self.max_leverage,
            max_total_exposure=self.max_total_exposure_usd,
            risk_per_trade=self.risk_per_trade,
            maintenance_margin_rate=self.maintenance_margin_rate,
            min_liq_distance_pct=self.min_liq_distance_pct,
            liq_stop_multiple=self.liq_stop_multiple,
            stop_slippage_bps=self.stop_slippage_bps,
        )


def mid(cache: Cache, instrument_id: InstrumentId) -> Decimal | None:
    """The mid of the latest quote, or ``None`` before the first one."""
    quote = cache.quote_tick(instrument_id)
    if quote is None:
        return None
    value: Decimal = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
    return value


def _mid_or_raise(cache: Cache, instrument_id: InstrumentId) -> Decimal:
    value = mid(cache, instrument_id)
    if value is None:
        raise InvalidRiskInput(f"open position on {instrument_id} has no quote")
    return value


def open_notional(cache: Cache, exclude: PositionId | None = None) -> Decimal:
    """``|signed qty| * mid`` summed over every open position in the cache, except ``exclude`` (AC-4)."""
    total = Decimal(0)
    for position in cache.positions_open():
        if position.id == exclude:
            continue
        total += abs(position.signed_decimal_qty()) * _mid_or_raise(cache, position.instrument_id)
    return total


def unrealized(cache: Cache) -> Decimal:
    """The unrealized P&L of every open position, each at its own instrument's mid."""
    total = Decimal(0)
    for position in cache.positions_open():
        instrument = cache.instrument(position.instrument_id)
        if instrument is None:
            raise InvalidRiskInput(f"open position on {position.instrument_id} has no instrument")
        price = instrument.make_price(_mid_or_raise(cache, position.instrument_id))
        total += position.unrealized_pnl(price).as_decimal()
    return total


def margin_maint(instrument: Instrument) -> Decimal:
    """The instrument's maintenance margin rate as NautilusTrader carries it; 0 when unset (AC-6)."""
    raw = instrument.margin_maint
    return Decimal(0) if raw is None else Decimal(str(raw))
