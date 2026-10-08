"""A replay variant's ``run_results`` numbers (spec 0008, Feature design, Result fields).

Everything is USD ``Decimal``. Equity is realized only: it steps after each closed trade and
each funding payment, from ``starting_balance`` as the first peak. A funding payment and a close
at the same nanosecond step funding first.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ClosedTrade:
    """One closed position: its realized P&L after fees, its fees, and the funding it paid or earned."""

    closed_ns: int
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal = Decimal(0)

    @property
    def net_pnl(self) -> Decimal:
        """Realized after fees, plus funding."""
        return self.realized_pnl + self.funding


@dataclass(frozen=True)
class FundingCash:
    """One funding payment: positive when the position earned it, negative when it paid."""

    paid_ns: int
    amount: Decimal


@dataclass(frozen=True)
class RunMetrics:
    """The numbers ``run_results`` stores for one variant (sample ``full``)."""

    net_pnl: Decimal
    return_after_costs: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trade_count: int
    fees_total: Decimal
    funding_total: Decimal


def max_drawdown(starting_balance: Decimal, trades: Sequence[ClosedTrade], funding: Sequence[FundingCash]) -> Decimal:
    """The largest fall of realized equity from its running peak, as a fraction of that peak.

    Steps are ordered by time, funding before a close at the same nanosecond, and otherwise
    in the order given. Zero when equity never falls.
    """
    steps = sorted(
        [(f.paid_ns, 0, n, f.amount) for n, f in enumerate(funding)]
        + [(t.closed_ns, 1, n, t.realized_pnl) for n, t in enumerate(trades)]
    )
    equity = peak = starting_balance
    worst = Decimal(0)
    for _, _, _, amount in steps:
        equity += amount
        if equity > peak:
            peak = equity
        elif peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def run_metrics(
    starting_balance: Decimal,
    trades: Sequence[ClosedTrade],
    funding: Sequence[FundingCash],
    fees_total: Decimal,
) -> RunMetrics:
    """Score a variant from its closed trades, its funding payments, and the fees of all its fills.

    ``win_rate`` counts closed trades whose net P&L (after fees and funding) is above zero; it is
    zero when nothing closed. Raises ``ValueError`` when ``starting_balance`` is zero or below.
    """
    if starting_balance <= 0:
        raise ValueError(f"starting balance must be above zero, got {starting_balance}")
    funding_total = sum((f.amount for f in funding), Decimal(0))
    net = sum((t.realized_pnl for t in trades), Decimal(0)) + funding_total
    wins = sum(1 for t in trades if t.net_pnl > 0)
    return RunMetrics(
        net_pnl=net,
        return_after_costs=net / starting_balance,
        max_drawdown=max_drawdown(starting_balance, trades, funding),
        win_rate=Decimal(wins) / Decimal(len(trades)) if trades else Decimal(0),
        trade_count=len(trades),
        fees_total=fees_total,
        funding_total=funding_total,
    )
