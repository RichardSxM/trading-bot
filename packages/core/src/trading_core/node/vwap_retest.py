"""``VwapRetest`` (spec 0017 child 0002): feeds each 1 minute bar to the pure rule; ``ScalpStrategy`` trades it."""

from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import Bar

from trading_core.node.scalp_base import Record, ScalpConfig, ScalpStrategy
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.scalp import ScalpSignal
from trading_core.strategy.vwap_retest import VwapRetestState

STRATEGY_NAME: Final = "vwap_retest"


class VwapRetestConfig(ScalpConfig, frozen=True, kw_only=True):
    """``vwap_retest``'s ``[strategy.config]`` settings (child 0002, *Config and grid*)."""

    time_stop_min: int = 15
    atr_n: int = 14
    vwap_min_bars: int = 60
    stop_atr: Decimal = Decimal("1.0")
    target_r: Decimal = Decimal("1.5")


class VwapRetest(ScalpStrategy):
    """Buys a held VWAP pullback in an uptrend, sells one in a downtrend (``trading_core.strategy.vwap_retest``)."""

    def __init__(self, config: VwapRetestConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self.rule = VwapRetestState(
            atr_n=config.atr_n, vwap_min_bars=config.vwap_min_bars, stop_atr=config.stop_atr, target_r=config.target_r
        )
        self._bar_ns = int(self.bar_type.spec.timedelta.total_seconds()) * 1_000_000_000

    @property
    def entry_reason(self) -> str:
        """``vwap_retest_entry``."""
        return f"{STRATEGY_NAME}_entry"

    def signal_on_bar(self, bar: Bar) -> ScalpSignal | None:
        """The rule's signal on this bar (it opened one bar step before its close)."""
        return self.rule.on_bar(
            bar.ts_event - self._bar_ns,
            bar.high.as_decimal(),
            bar.low.as_decimal(),
            bar.close.as_decimal(),
            bar.volume.as_decimal(),
        )
