"""``DonchianScalp``: ``donchian_4h``'s rule on 5 minute bars, a demo only paper strategy for quicker trades.

Its own module so its code hash is its own; the frozen ``donchian_4h`` module is untouched. It has no lab
study and no gate pass, so it runs only on demo after a manual ``candidate → paper`` promote. Only its config
and how it builds its pure rule live here; the lifecycle is the base's.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian import DonchianRule

STRATEGY_NAME: Final = "donchian_scalp"
# The donchian_4h_pair channel and stop on 5 minute bars; the time stop is 5 hours (60 bars).
BAR_SPEC: Final = "5-MINUTE-LAST"


class DonchianScalpConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``donchian_scalp``'s settings on top of the base's."""

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 60
    entry_bars: int = 20
    stop_atr: Decimal = Decimal(3)

    def __post_init__(self) -> None:
        """The base's checks, then this strategy's."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.entry_bars < 2:
            raise ValueError(f"entry_bars must be at least 2, got {self.entry_bars}")
        if self.stop_atr < 1:
            raise ValueError(f"stop_atr must be at least 1, got {self.stop_atr}")


class DonchianScalp(BarRuleStrategy):
    """``BarRuleStrategy`` with ``DonchianRule`` on 5 minute bars."""

    def __init__(
        self, config: DonchianScalpConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        super().__init__(
            config, gate, blocks, record, DonchianRule(entry_bars=config.entry_bars, stop_atr=config.stop_atr)
        )
