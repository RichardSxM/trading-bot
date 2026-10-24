"""``BollingerRsi`` (spec 0017 child 0001): feeds each 1 minute bar to the pure rule; ``ScalpStrategy`` trades it."""

from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import Bar

from trading_core.node.scalp_base import Record, ScalpConfig, ScalpStrategy
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.bollinger_rsi import BollingerRsiState
from trading_core.strategy.scalp import ScalpSignal

STRATEGY_NAME: Final = "bollinger_rsi"


class BollingerRsiConfig(ScalpConfig, frozen=True, kw_only=True):
    """``bollinger_rsi``'s ``[strategy.config]`` settings (child 0001, *Config and grid*)."""

    time_stop_min: int = 15
    bb_n: int = 20
    bb_k: Decimal = Decimal("2.0")
    rsi_n: int = 14
    rsi_low: int = 30
    atr_n: int = 14
    flat_band_frac: Decimal = Decimal("0.5")
    stop_atr_buffer: Decimal = Decimal("0.25")


class BollingerRsi(ScalpStrategy):
    """Fades a close back inside flat Bollinger bands (see ``trading_core.strategy.bollinger_rsi``)."""

    def __init__(self, config: BollingerRsiConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self.rule = BollingerRsiState(
            bb_n=config.bb_n,
            bb_k=config.bb_k,
            rsi_n=config.rsi_n,
            rsi_low=config.rsi_low,
            atr_n=config.atr_n,
            flat_band_frac=config.flat_band_frac,
            stop_atr_buffer=config.stop_atr_buffer,
        )

    @property
    def entry_reason(self) -> str:
        """``bollinger_rsi_entry``."""
        return f"{STRATEGY_NAME}_entry"

    def signal_on_bar(self, bar: Bar) -> ScalpSignal | None:
        """The rule's signal on this bar."""
        return self.rule.on_bar(bar.high.as_decimal(), bar.low.as_decimal(), bar.close.as_decimal())
