"""``ConnorsRsi``: the ``connors_rsi`` candidate (spec 0018.1) on the shared ``BarRuleStrategy`` base.

Only its config and how it builds its pure rule live here; the lifecycle is the base's.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.connors_rsi import ConnorsRsiRule

STRATEGY_NAME: Final = "connors_rsi"


class ConnorsRsiConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``connors_rsi``'s ``[strategy.config]`` settings (spec 0018.1 *Settings*) on top of the base's.

    The grid settings (``rsi_period``, ``rsi_lower``) default to the grid's first point, because the lab
    checks ``[strategy.config]`` alone before it adds a point; every study run sets them from its point.
    """

    rsi_period: int = 2
    rsi_lower: int = 5
    time_stop_bars: int = 10
    trend_sma: int = 200
    exit_sma: int = 5
    stop_atr_mult: Decimal = Decimal(3)

    def __post_init__(self) -> None:
        """The base's checks, then this candidate's (AC-8)."""
        super().__post_init__()
        for name in ("trend_sma", "exit_sma", "rsi_period"):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least 2, got {getattr(self, name)}")
        if self.stop_atr_mult <= 0:
            raise ValueError(f"stop_atr_mult must be above 0, got {self.stop_atr_mult}")
        if not 0 < self.rsi_lower < 50:
            raise ValueError(f"rsi_lower must be above 0 and below 50, got {self.rsi_lower}")


class ConnorsRsi(BarRuleStrategy):
    """``BarRuleStrategy`` with ``ConnorsRsiRule``."""

    def __init__(
        self, config: ConnorsRsiConfig, gate: PauseGate, blocks: EntryBlocks, record: Callable[[WriteItem], None]
    ) -> None:
        rule = ConnorsRsiRule(
            config.bar_interval_ns,
            rsi_period=config.rsi_period,
            rsi_lower=config.rsi_lower,
            trend_sma=config.trend_sma,
            exit_sma=config.exit_sma,
            stop_atr_mult=config.stop_atr_mult,
        )
        super().__init__(config, gate, blocks, record, rule)
