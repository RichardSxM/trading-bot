"""``ImbalanceFade`` (spec 0021): ``BookImbalance`` on the node with the faded rule.

Everything but the rule and the entry reason is inherited: the L2 book and trade subscriptions, the 15
second timer, the maker entry, the maker target exit, the stop, and the time stop. Replay only.
"""

from typing import Final

from trading_core.node.book_imbalance import BookImbalance, BookImbalanceConfig
from trading_core.node.scalp_base import Record
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.imbalance_fade import ImbalanceFadeState

STRATEGY_NAME: Final = "imbalance_fade"


class ImbalanceFadeConfig(BookImbalanceConfig, frozen=True, kw_only=True):
    """``imbalance_fade``'s ``[strategy.config]`` settings: ``book_imbalance``'s, with the same defaults (AC-3)."""


class ImbalanceFade(BookImbalance):
    """Fades a lopsided book its trade flow agrees with (``trading_core.strategy.imbalance_fade``)."""

    def __init__(self, config: ImbalanceFadeConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self.rule = ImbalanceFadeState(
            imb_min=config.imb_min,
            persist_bars=config.persist_bars,
            levels=config.levels,
            stop_bps=config.stop_bps,
            target_bps=config.target_bps,
        )

    @property
    def entry_reason(self) -> str:
        """``imbalance_fade_entry``."""
        return f"{STRATEGY_NAME}_entry"
