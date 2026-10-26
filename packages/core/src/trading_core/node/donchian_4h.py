"""``Donchian4h``: the ``donchian_4h`` candidate (spec 0020.3) on the shared ``BarRuleStrategy`` base.

Only its config and how it builds its pure rule live here; the lifecycle is the base's.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian import DonchianRule

STRATEGY_NAME: Final = "donchian_4h"
# Its bar counts (the 10 day time stop, the channel lengths) are written for 4 hour bars (spec 0020 AC-4).
BAR_SPEC: Final = "4-HOUR-LAST"


class Donchian4hConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``donchian_4h``'s ``[strategy.config]`` settings (spec 0020.3 *Settings*) on top of the base's.

    The grid settings (``entry_bars``, ``stop_atr``) default to the grid's first point, because the lab
    checks ``[strategy.config]`` alone before it adds a point; every study run sets them from its point.
    """

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 60
    entry_bars: int = 20
    stop_atr: Decimal = Decimal(2)

    def __post_init__(self) -> None:
        """The base's checks, then this candidate's (AC-4)."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.entry_bars < 2:
            raise ValueError(f"entry_bars must be at least 2, got {self.entry_bars}")
        if self.stop_atr < 1:
            raise ValueError(f"stop_atr must be at least 1, got {self.stop_atr}")


class Donchian4h(BarRuleStrategy):
    """``BarRuleStrategy`` with ``DonchianRule``."""

    def __init__(
        self, config: Donchian4hConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        super().__init__(
            config, gate, blocks, record, DonchianRule(entry_bars=config.entry_bars, stop_atr=config.stop_atr)
        )
