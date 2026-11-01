"""``Tsmom``: the ``tsmom`` candidate (spec 0027.2 AC-13) on the shared ``BarRuleStrategy`` base, daily bars.

Only its config and how it builds its pure rule live here; the lifecycle is the base's (market entries
and exits at the bar close, the floored risk sized protective stop, the time stop).
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.tsmom import TsmomRule, warmup_bars

STRATEGY_NAME: Final = "tsmom"
# The lookback counts days (spec 0027.2 AC-13).
BAR_SPEC: Final = "1-DAY-LAST"


class TsmomConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``tsmom``'s ``[strategy.config]`` settings (spec 0027.2 AC-13) on top of the base's.

    ``lookback_days`` defaults to the grid's first point, because the lab checks ``[strategy.config]``
    alone before it adds a point. ``long_only`` is the gate's declaration (spec 0027.1 AC-6): the
    strategy has no short side, so it must stay true, and the study file writes it explicitly so the
    frozen version config holds it.
    """

    bar_spec: str = BAR_SPEC
    lookback_days: int = 20
    stop_atr: Decimal
    long_only: bool = True

    def __post_init__(self) -> None:
        """The base's checks, then this candidate's (AC-13)."""
        super().__post_init__()
        if self.long_only is not True:
            raise ValueError(f"long_only must be true (tsmom has no short side), got {self.long_only!r}")
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.lookback_days < 1:
            raise ValueError(f"lookback_days must be at least 1, got {self.lookback_days}")
        if self.stop_atr <= 0:
            raise ValueError(f"stop_atr must be above 0, got {self.stop_atr}")

    def warmup_bars(self) -> int:
        """Daily bars the lab's trend pre roll feeds before each segment (spec 0027.1 AC-10a)."""
        return warmup_bars(self.lookback_days, self.atr_period)


class Tsmom(BarRuleStrategy):
    """``BarRuleStrategy`` with ``TsmomRule`` on daily bars."""

    def __init__(
        self, config: TsmomConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        super().__init__(
            config,
            gate,
            blocks,
            record,
            TsmomRule(lookback_days=config.lookback_days, stop_atr=config.stop_atr, atr_period=config.atr_period),
        )
