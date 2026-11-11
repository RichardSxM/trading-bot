"""``TrendVol``: ``trend_daily`` sized by the risk of the whole book, with a no trade band.

Everything ``TrendDaily`` does (the daily schedule, orders, flips, ``check_rebalance``, held orders, retries,
decisions, the warmup seed, the start and resume rebalances) is inherited unchanged. Two things differ, both
from the pure rule in ``trading_core.strategy.trend_vol``:

- Every plan is rescaled by ``book_vol_plan``: same signals, same split between coins, but the gross follows
  the book's own 30 day volatility toward ``book_vol_target`` instead of sitting at the gross cap.
- A rebalance leaves a coin alone when its position is already ``within_band`` of its target.

A plan that cannot be rescaled is dropped (no rebalance that day), so the book never trades on the base
plan's larger size.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from nautilus_trader.model.identifiers import InstrumentId

from trading_core.node import guard
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_daily import TrendDaily, TrendDailyConfig
from trading_core.node.trend_portfolio import Record
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose
from trading_core.strategy.trend_vol import BookVolParams, book_vol_plan, within_band

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "trend_vol"
ORDER_ID_TAG: Final = "TV"


class TrendVolConfig(TrendDailyConfig, frozen=True, kw_only=True):
    """``TrendDailyConfig`` plus the book level settings: ``book_vol_target`` (annual, 0.5 is 50 %) and
    ``rebalance_band`` (0 is off). ``gross_cap`` is the most gross the book may reach; ``vol_target`` only
    sets the split between coins."""

    book_vol_target: Decimal = Decimal("0.5")
    rebalance_band: Decimal = Decimal("0.3")

    def __post_init__(self) -> None:
        """The base checks, then the book settings' own (a bad target or band raises ``ValueError``)."""
        super().__post_init__()
        self.book_params()

    def book_params(self) -> BookVolParams:
        """The book level rule's parameters."""
        return BookVolParams(
            book_vol_target=self.book_vol_target,
            vol_days=self.vol_days,
            gross_cap=self.gross_cap,
            band=self.rebalance_band,
        )


class TrendVol(TrendDaily):
    """The daily trend portfolio with book volatility sizing and a no trade band (module docstring)."""

    def __init__(
        self,
        config: TrendVolConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Record,
        seeds: Mapping[str, Sequence[DailyClose]] | None = None,
        last_start_ns: int | None = None,
    ) -> None:
        super().__init__(config, gate, blocks, record, seeds, last_start_ns)
        self._book = config.book_params()
        self.unit_vol: Decimal | None = None
        self.book_gross: Decimal | None = None

    def _make_plan(self, day: date) -> bool:
        # The base plan, rescaled to the book volatility target; the old plan stays if either step fails.
        before, before_day = self.plan, self.plan_day
        if not super()._make_plan(day):
            return False
        assert self.plan is not None
        try:
            sized = book_vol_plan(self.closes_until(day), self.plan, self._book)
        except ValueError:
            self.plan, self.plan_day = before, before_day
            log.exception("book volatility sizing failed", extra={"fields": {"code": "plan_empty"}})
            return False
        self.plan = sized.plan
        self.unit_vol = sized.unit_vol
        self.book_gross = sized.gross
        log.info(
            "book sized by volatility",
            extra={
                "fields": {
                    "code": "book_vol_sized",
                    "day": day.isoformat(),
                    "unit_vol": None if sized.unit_vol is None else str(sized.unit_vol),
                    "gross": str(sized.gross),
                    "book_vol_target": str(self._book.book_vol_target),
                }
            },
        )
        return True

    def _step(self, coin: InstrumentId, *, first: bool) -> None:
        # A position already close to its target is left alone; everything else is the base's step.
        if first and self.plan is not None:
            mid = guard.mid(self.cache, coin)
            if mid is not None:
                target = self.plan.coins[str(coin)].target_notional
                if within_band(target, self._qty(coin) * mid, self._book.band):
                    log.info(
                        "rebalance inside the band",
                        extra={"fields": {"code": "rebalance_skip_band", "instrument": str(coin)}},
                    )
                    return
        super()._step(coin, first=first)

    def plan_snapshot(self) -> Mapping[str, object]:
        """``TrendDaily``'s ``portfolio_plan`` data plus the book settings and the last sizing."""
        data = dict(super().plan_snapshot())
        data["book_vol_target"] = str(self._book.book_vol_target)
        data["rebalance_band"] = str(self._book.band)
        data["unit_vol"] = None if self.unit_vol is None else str(self.unit_vol)
        data["book_gross"] = None if self.book_gross is None else str(self.book_gross)
        return data
