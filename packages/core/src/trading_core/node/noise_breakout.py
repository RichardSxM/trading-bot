"""``NoiseBreakout``: the ``noise_breakout`` candidate (spec 0020.1) on the shared ``BarRuleStrategy`` base.

Only its config and how it builds its pure rule live here; the lifecycle is the base's.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.noise_breakout import NoiseBreakoutRule

STRATEGY_NAME: Final = "noise_breakout"
# Its slot math and bar counts (48 slots a day, the 8 hour time stop) are written for 30 minute bars (spec 0020 AC-4).
BAR_SPEC: Final = "30-MINUTE-LAST"


class NoiseBreakoutConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``noise_breakout``'s ``[strategy.config]`` settings (spec 0020.1 *Settings*) on top of the base's.

    The grid setting (``band_mult``) defaults to the grid's first point, because the lab checks
    ``[strategy.config]`` alone before it adds a point; every study run sets it from its point.
    """

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 16
    lookback_days: int = 14
    band_mult: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        """The base's checks, then this candidate's (AC-4)."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.lookback_days < 1:
            raise ValueError(f"lookback_days must be at least 1, got {self.lookback_days}")
        if self.band_mult <= 0:
            raise ValueError(f"band_mult must be above 0, got {self.band_mult}")


class NoiseBreakout(BarRuleStrategy):
    """``BarRuleStrategy`` with ``NoiseBreakoutRule``."""

    def __init__(
        self, config: NoiseBreakoutConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        rule = NoiseBreakoutRule(
            config.bar_interval_ns,
            lookback_days=config.lookback_days,
            band_mult=config.band_mult,
            time_stop_bars=config.time_stop_bars,
        )
        super().__init__(config, gate, blocks, record, rule)
