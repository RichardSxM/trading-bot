"""``DeltaDivergence`` (spec 0017 child 0003): feeds trade ticks and 1 minute bars to the pure rule.

The scalping base trades it.
"""

from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import Bar, TradeTick
from nautilus_trader.model.enums import AggressorSide

from trading_core.node.scalp_base import Record, ScalpConfig, ScalpStrategy
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.delta_divergence import DeltaDivergenceState
from trading_core.strategy.scalp import MINUTE_NS, ScalpSignal

STRATEGY_NAME: Final = "delta_divergence"
_AGGRESSORS: Final = {AggressorSide.BUYER: "buy", AggressorSide.SELLER: "sell"}


class DeltaDivergenceConfig(ScalpConfig, frozen=True, kw_only=True):
    """``delta_divergence``'s ``[strategy.config]`` settings (child 0003, *Config and grid*)."""

    time_stop_min: int = 10
    lookback_bars: int = 5
    move_atr: Decimal = Decimal("1.5")
    atr_n: int = 14
    stop_atr_buffer: Decimal = Decimal("0.25")


class DeltaDivergence(ScalpStrategy):
    """Fades a move its delta does not back (``trading_core.strategy.delta_divergence``)."""

    def __init__(self, config: DeltaDivergenceConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self.rule = DeltaDivergenceState(
            lookback_bars=config.lookback_bars,
            move_atr=config.move_atr,
            atr_n=config.atr_n,
            stop_atr_buffer=config.stop_atr_buffer,
        )

    @property
    def entry_reason(self) -> str:
        """``delta_divergence_entry``."""
        return f"{STRATEGY_NAME}_entry"

    def on_start(self) -> None:
        """The base's subscriptions, plus the instrument's trade ticks for the delta."""
        super().on_start()
        self.subscribe_trade_ticks(self._instrument_id)

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Fold the trade into its ``ts_event`` minute's bucket."""
        if tick.instrument_id == self._instrument_id:
            self.rule.on_trade(tick.ts_event, tick.size.as_decimal(), _AGGRESSORS.get(tick.aggressor_side, "none"))

    def signal_on_bar(self, bar: Bar) -> ScalpSignal | None:
        """The rule's signal on the bar that closes minute ``ts_event / 60 s - 1``."""
        minute = bar.ts_event // MINUTE_NS - 1
        return self.rule.on_bar(minute, bar.high.as_decimal(), bar.low.as_decimal(), bar.close.as_decimal())
