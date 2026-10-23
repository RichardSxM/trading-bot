"""What the gate reads, as frozen dataclasses (spec 0015): built from Postgres by the recording adapter."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trading_core.gate.series import DailySeries, DayEquity, daily_series
from trading_core.gate.thresholds import Thresholds


class Lab(StrEnum):
    """Which lab produced a study (mirrors ``recording.enums.StudyLab``; the gate imports no adapter)."""

    CANDLE = "candle"
    REPLAY = "replay"


class Role(StrEnum):
    """A run's role in its study."""

    DEVELOPMENT = "development"
    HOLDOUT = "holdout"
    BENCHMARK = "benchmark"


BUY_AND_HOLD = "buy_and_hold"
MACD_BASELINE = "macd_baseline"
HOUR_LEVEL = "hour_level"
COMPLETED = "completed"


@dataclass(frozen=True)
class RunSummary:
    """A run's ``run_results`` row (sample ``full``)."""

    return_after_costs: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trade_count: int
    fees_total: Decimal
    funding_total: Decimal


@dataclass(frozen=True)
class Measures:
    """A run's ``run_measures`` row."""

    drawdown_basis: str
    marked_max_drawdown: Decimal | None
    long_trades: int
    long_net_pnl: Decimal
    short_trades: int
    short_net_pnl: Decimal
    top_trades_profit_share: Decimal | None
    avg_gross_edge: Decimal | None
    avg_round_trip_cost: Decimal | None


@dataclass(frozen=True)
class StudyRunInput:
    """One run of the study with everything the checks read; ``summary`` is None unless it completed.

    ``funding_missing`` is set when a position spanned a funding charge time with no payment row, and
    ``cost_setting_missing`` when ``run_config`` lacks the slippage (candle) or latency (replay) setting.
    ``starting_balance`` is None for a run that never recorded one; such a run never counts as completed.
    ``frozen_hash`` is the ``study_runs`` copy and ``version_frozen_hash`` that of the run's own strategy
    version; the gate needs the two to agree.
    """

    run_id: UUID
    role: Role
    strategy: str
    grid_point: int | None
    frozen_hash: str
    status: str
    starting_balance: Decimal | None
    daily: tuple[DayEquity, ...] = ()
    summary: RunSummary | None = None
    measures: Measures | None = None
    funding_missing: bool = False
    cost_setting_missing: bool = False
    version_frozen_hash: str | None = None

    @property
    def completed(self) -> bool:
        """True when the run completed, its results were written, and it recorded a starting balance."""
        return self.status == COMPLETED and self.summary is not None and self.starting_balance is not None

    def series(self, lab: Lab) -> DailySeries:
        """Its daily series: chained for a candle run, one segment per day for a replay run."""
        if self.starting_balance is None:
            raise ValueError(f"run {self.run_id} recorded no starting balance")
        return daily_series(self.daily, self.starting_balance, chained=lab == Lab.CANDLE)


@dataclass(frozen=True)
class TradeReturn:
    """One closed holdout trade: its net P&L and its return over the equity before it (AC-11)."""

    net_pnl: Decimal
    ret: Decimal


@dataclass(frozen=True)
class CandidateInputs:
    """Everything ``evaluate_candidate`` reads for one study.

    ``code_hash`` is the final version's (None when it predates spec 0015). ``trial_count`` is N for
    the deflated Sharpe: distinct frozen hashes over the development and holdout study runs of every
    strategy of the same kind in the same lab, failed ones too (benchmarks are yardsticks, not
    attempts; spec 0018 AC-19). ``trial_sharpes`` are that family's per day Sharpes, one per frozen
    hash with a completed development run, for the deflated Sharpe's trial variance.
    ``final_grid_point`` is the development winner as the lab ranks it (``repository.final_grid_point``),
    the only grid point the holdout may run.
    """

    study_id: UUID
    lab: Lab
    strategy_name: str
    strategy_kind: str
    thresholds: Thresholds
    seed: int
    code_hash: str | None
    runs: tuple[StudyRunInput, ...]
    holdout_trades: tuple[TradeReturn, ...] = ()
    trial_count: int = 1
    trial_sharpes: tuple[float, ...] = ()
    fold_overrides: tuple[tuple[str, int], ...] = ()
    final_grid_point: int | None = None

    def by_role(self, role: Role) -> list[StudyRunInput]:
        """This role's runs, in grid point order then run id."""
        return sorted((r for r in self.runs if r.role == role), key=lambda r: (r.grid_point or 0, str(r.run_id)))

    def completed_holdouts(self) -> list[StudyRunInput]:
        """Every completed holdout run; more than one is a broken study, never a choice."""
        return [r for r in self.by_role(Role.HOLDOUT) if r.completed]

    def holdout(self) -> StudyRunInput | None:
        """The study's one completed holdout run, or None (none, or more than one)."""
        done = self.completed_holdouts()
        return done[0] if len(done) == 1 else None

    def benchmark(self, strategy: str) -> StudyRunInput | None:
        """The completed benchmark run of ``strategy``, or None."""
        return next((r for r in self.by_role(Role.BENCHMARK) if r.strategy == strategy and r.completed), None)
