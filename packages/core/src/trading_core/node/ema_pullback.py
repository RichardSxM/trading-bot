"""``EmaPullback``: the ``ema_pullback`` candidate (spec 0019.2) on the shared ``BarRuleStrategy`` base.

Only its config and how it builds its pure rule live here; the lifecycle is the base's.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.ema_pullback import EmaPullbackRule

STRATEGY_NAME: Final = "ema_pullback"
# Its bar counts (warmup, arm window, the 8 hour time stop) are written for 15 minute bars (spec 0019 AC-4).
BAR_SPEC: Final = "15-MINUTE-LAST"


class EmaPullbackConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``ema_pullback``'s ``[strategy.config]`` settings (spec 0019.2 *Settings*) on top of the base's.

    The grid settings (``target_r``, ``rsi_high``) default to the grid's first point, because the lab
    checks ``[strategy.config]`` alone before it adds a point; every study run sets them from its point.
    """

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 32
    fast: int = 8
    slow: int = 21
    rsi_period: int = 7
    rsi_low: int = 30
    arm_bars: int = 6
    target_r: Decimal = Decimal("1.5")
    rsi_high: int = 45

    def __post_init__(self) -> None:
        """The base's checks, then this candidate's (AC-4)."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.fast < 2:
            raise ValueError(f"fast must be at least 2, got {self.fast}")
        if self.slow <= self.fast:
            raise ValueError(f"slow must be above fast {self.fast}, got {self.slow}")
        if self.rsi_period < 2:
            raise ValueError(f"rsi_period must be at least 2, got {self.rsi_period}")
        if not 0 < self.rsi_low < self.rsi_high:
            raise ValueError(f"rsi_low must be above 0 and below rsi_high {self.rsi_high}, got {self.rsi_low}")
        if self.rsi_high > 50:
            raise ValueError(f"rsi_high must be at most 50, got {self.rsi_high}")
        if self.arm_bars < 1:
            raise ValueError(f"arm_bars must be at least 1, got {self.arm_bars}")
        if self.target_r <= 0:
            raise ValueError(f"target_r must be above 0, got {self.target_r}")


class EmaPullback(BarRuleStrategy):
    """``BarRuleStrategy`` with ``EmaPullbackRule``."""

    def __init__(
        self, config: EmaPullbackConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        rule = EmaPullbackRule(
            config.bar_interval_ns,
            fast=config.fast,
            slow=config.slow,
            rsi_period=config.rsi_period,
            rsi_low=config.rsi_low,
            rsi_high=config.rsi_high,
            arm_bars=config.arm_bars,
            target_r=config.target_r,
        )
        super().__init__(config, gate, blocks, record, rule)
