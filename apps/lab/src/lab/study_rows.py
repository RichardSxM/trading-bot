"""The rows spec 0015 adds to a lab bundle: daily equity, measures, the daily Sharpe, and the study parts.

Thin: the numbers come from ``trading_core.lab.daily`` and ``trading_core.gate.series``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final
from uuid import UUID

from trading_core.gate.series import DayEquity, annualized, daily_series, sharpe, to_decimal
from trading_core.lab.daily import DayClose, TradeMeasures
from trading_core.recording.enums import Env, EquityBasis, RunStatus, StrategyKind, StudyRole
from trading_core.recording.rows import (
    DailyEquityRow,
    InstrumentRow,
    RunBundle,
    RunMeasuresRow,
    StrategyVersionRow,
    StudyRow,
    StudyRunRow,
)

RATIO_KIND: Final = "sharpe_daily"


@dataclass(frozen=True)
class StudyContext:
    """What makes a lab batch part of a study: the study, the runs' role, and each variant's grid point and version."""

    study: StudyRow
    role: StudyRole
    strategy_name: str
    strategy_kind: StrategyKind
    # The studied strategy; a benchmark's own strategy (above) differs.
    study_strategy_name: str
    study_strategy_kind: StrategyKind
    # Every coin of the run in sorted order; the bundle's ``instrument`` is the first (spec 0022 AC-12).
    instruments: tuple[InstrumentRow, ...]
    grid_points: Mapping[str, int | None]
    versions: Mapping[str, StrategyVersionRow]

    def attach(self, bundle: RunBundle, variant: str) -> RunBundle:
        """``bundle`` with the version, instruments, study, and its ``study_runs`` row (status from the run)."""
        version = self.versions[variant]
        if version.frozen_hash is None:
            raise ValueError(f"version {version.id} has no frozen hash; a study run needs one")
        return replace(
            bundle,
            strategy_name=self.strategy_name,
            strategy_kind=self.strategy_kind,
            version=version,
            instrument=self.instruments[0],
            instruments=self.instruments,
            study=self.study,
            study_strategy_name=self.study_strategy_name,
            study_strategy_kind=self.study_strategy_kind,
            study_run=StudyRunRow(
                study_id=self.study.id,
                run_id=bundle.run.id,
                role=self.role,
                grid_point=self.grid_points[variant],
                frozen_hash=version.frozen_hash,
                status=RunStatus(bundle.run.status),
            ),
        )


def daily_equity_rows(run_id: UUID, closes: Sequence[DayClose], basis: EquityBasis) -> list[DailyEquityRow]:
    """``run_daily_equity`` rows of a demo lab run."""
    return [
        DailyEquityRow(
            run_id=run_id,
            env=Env.DEMO,
            day=c.day,
            equity_close=c.equity_close,
            basis=basis,
            trades_closed=c.trades_closed,
            fees=c.fees,
            funding=c.funding,
            currency="USD",
        )
        for c in closes
    ]


def daily_sharpe(closes: Sequence[DayClose], starting_balance: Decimal, *, chained: bool) -> Decimal | None:
    """The run's annualized daily Sharpe (``run_results.risk_adjusted_ratio``), or None when undefined."""
    rows = [DayEquity(c.day, c.equity_close, c.trades_closed, c.fees, c.funding) for c in closes]
    return to_decimal(annualized(sharpe(daily_series(rows, starting_balance, chained=chained).returns)))


def measures_row(
    run_id: UUID,
    basis: EquityBasis,
    trades: TradeMeasures,
    *,
    marked_max_drawdown: Decimal | None = None,
    longest_drawdown_hours: int | None = None,
    exposure: Decimal | None = None,
) -> RunMeasuresRow:
    """The ``run_measures`` row of a demo lab run."""
    return RunMeasuresRow(
        run_id=run_id,
        env=Env.DEMO,
        drawdown_basis=basis,
        marked_max_drawdown=marked_max_drawdown,
        longest_drawdown_hours=longest_drawdown_hours,
        exposure=exposure,
        long_trades=trades.long_trades,
        long_net_pnl=trades.long_net_pnl,
        short_trades=trades.short_trades,
        short_net_pnl=trades.short_net_pnl,
        profit_factor=trades.profit_factor,
        expectancy=trades.expectancy,
        top_trades_profit_share=trades.top_trades_profit_share,
        avg_gross_edge=trades.avg_gross_edge,
        avg_round_trip_cost=trades.avg_round_trip_cost,
        currency="USD",
    )
