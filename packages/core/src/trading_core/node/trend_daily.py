"""``TrendDaily``: ``trend_portfolio`` rebalanced every day instead of weekly (spec 0032 AC-1 to AC-3).

Everything spec 0030 pins (the rule, quantities and deltas, orders, flips, ``check_rebalance``, held orders,
retries, decisions, the warmup seed, the start and resume rebalances, the start cooldown) is inherited from
``TrendPortfolio`` unchanged. Only the schedule differs: every day at ``run_at_utc`` it plans from the closes
up to the day that just ended and, when the plan was made, rebalances with reason ``daily``, at most once per
UTC date. ``rebalance_weekday`` is ignored (kept only because the base config requires it). A ``start`` or
``resume`` rebalance does not count as that date's daily one.

The coins a rebalance skipped because their daily close is missing are kept in ``close_missing`` and written
to the ``portfolio_plan`` snapshot, so the alerter can name them (AC-2b).
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from trading_core.node.strategy import PauseGate
from trading_core.node.trend_portfolio import NS_PER_S, Record, TrendPortfolio, TrendPortfolioConfig
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "trend_daily"
ORDER_ID_TAG: Final = "TD"
DAILY: Final = "daily"
SCHEDULE: Final = "daily"


class TrendDailyConfig(TrendPortfolioConfig, frozen=True, kw_only=True):
    """``TrendPortfolioConfig`` unchanged; ``rebalance_weekday`` is recorded but ignored (AC-4)."""


class TrendDaily(TrendPortfolio):
    """The trend portfolio rebalanced once every UTC day at ``run_at_utc`` (module docstring)."""

    def __init__(
        self,
        config: TrendDailyConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Record,
        seeds: Mapping[str, Sequence[DailyClose]] | None = None,
        last_start_ns: int | None = None,
    ) -> None:
        super().__init__(config, gate, blocks, record, seeds, last_start_ns)
        self._last_daily: date | None = None
        self.close_missing: list[str] = []

    def _on_daily(self, event: object) -> None:
        # The base's daily run, with the weekday test replaced by "once per date" (AC-2).
        now = self.clock.timestamp_ns()
        self._arm_daily()
        if not self.fresh_signal(now):
            return
        today = datetime.fromtimestamp(now / NS_PER_S, tz=UTC).date()
        try:
            planned = self._make_plan(today - timedelta(days=1))
            if planned and self._last_daily != today:
                self._last_daily = today
                self._rebalance(DAILY)
        except Exception:
            log.exception("daily portfolio run failed", extra={"fields": {"code": "portfolio_run_failed"}})

    def _rebalance(self, reason: str, skip: frozenset[str] = frozenset()) -> None:
        # Note the coins the base will skip for a missing close (the same test it uses), then rebalance.
        plan = self.plan
        if plan is not None:
            self.close_missing = sorted(
                str(coin)
                for coin in self._coins
                if str(coin) not in skip
                and plan.coins[str(coin)].eligible
                and plan.coins[str(coin)].close_date != self.plan_day
            )
        super()._rebalance(reason, skip)

    def plan_snapshot(self) -> Mapping[str, object]:
        """The base's ``portfolio_plan`` data with ``schedule`` ``daily`` in place of ``rebalance_weekday`` (AC-2a),
        plus ``close_missing`` (AC-2b)."""
        data = {key: value for key, value in super().plan_snapshot().items() if key != "rebalance_weekday"}
        data["schedule"] = SCHEDULE
        data["close_missing"] = list(self.close_missing)
        return data
