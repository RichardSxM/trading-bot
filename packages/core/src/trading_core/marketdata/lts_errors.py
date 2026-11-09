"""Typed errors of the liquidity trap scalper's market data (spec 0031)."""


class MarketDataIntegrityError(Exception):
    """A market data row breaks a rule the feed relies on, such as an unknown trade type."""


class InstrumentMetadataError(Exception):
    """An instrument's metadata is missing, invalid, or describes a market we refuse to trade."""
