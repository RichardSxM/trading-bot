"""The paper → live verdict (spec 0015 AC-16), pure: does the paper record match what the lab promised?

The paper runs of one frozen version, started after its latest ``candidate → paper`` pass, are chained
into one realized equity series from the first run's starting balance: each closed position moves it by
its realized P&L (already after fees), each funding payment by its amount. ``evaluated_at`` comes from
the caller. Without a gate pass with a completed holdout behind it, the verdict is ``insufficient``, so a
version promoted only by hand never reaches live.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_core.gate.checks import (
    FAILED,
    MISSING_METRIC,
    MISSING_RUN,
    TOO_FEW_DAYS,
    TOO_FEW_TRADES,
    Outcome,
)
from trading_core.gate.thresholds import PaperThresholds, Thresholds
from trading_core.gate.verdict import GateResult, decide

_BPS = Decimal(10_000)
_SIX = Decimal("0.000001")


@dataclass(frozen=True)
class PaperEvent:
    """One step of the chained paper equity: a closed position's realized P&L, or a funding payment."""

    at: datetime
    amount: Decimal
    closes_trade: bool


@dataclass(frozen=True)
class EntryFill:
    """One paper entry fill: its price against the decision's mid, and whether its order was post only."""

    buy: bool
    price: Decimal
    mid: Decimal | None


@dataclass(frozen=True)
class EntryOrder:
    """One paper entry order (``origin = strategy``, decision intent ``enter``)."""

    post_only: bool
    filled: bool


@dataclass(frozen=True)
class PaperInputs:
    """Everything ``evaluate_paper`` reads for one frozen hash.

    ``gate_pass`` is True when the latest ``candidate → paper`` promotion is a gate pass whose study has a
    completed holdout; ``holdout_trade_bound`` is that promotion's ``trade_return_lower_bound``. The lab
    side of the drift checks is the holdout's maker fill rate and its entry slippage in bps.
    """

    frozen_hash: str
    strategy_kind: str
    thresholds: Thresholds | None
    evaluated_at: datetime
    gate_pass: bool
    entry_mode: str | None
    first_started_at: datetime | None
    starting_balance: Decimal | None
    events: tuple[PaperEvent, ...] = ()
    entries: tuple[EntryOrder, ...] = ()
    entry_fills: tuple[EntryFill, ...] = ()
    holdout_trade_bound: Decimal | None = None
    lab_fill_rate: Decimal | None = None
    lab_slippage_bps: Decimal | None = None


def entry_slippage_bps(fill: EntryFill) -> Decimal | None:
    """How much worse than the decision's mid the fill was, in bps (positive is worse for the trade)."""
    if fill.mid is None or fill.mid == 0:
        return None
    signed = (fill.price - fill.mid) if fill.buy else (fill.mid - fill.price)
    return signed / fill.mid * _BPS


@dataclass(frozen=True)
class PaperRecord:
    """The chained paper record: return, realized max drawdown, and the per trade returns."""

    trades: int
    paper_return: Decimal
    max_drawdown: Decimal
    trade_returns: tuple[Decimal, ...]


def paper_record(starting_balance: Decimal, events: Sequence[PaperEvent]) -> PaperRecord:
    """Chain the events in time order from the starting balance."""
    equity = peak = starting_balance
    worst = Decimal(0)
    returns: list[Decimal] = []
    for event in sorted(events, key=lambda e: (e.at, not e.closes_trade)):
        if event.closes_trade:
            returns.append(event.amount / equity if equity > 0 else Decimal(0))
        equity += event.amount
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return PaperRecord(len(returns), (equity - starting_balance) / starting_balance, worst, tuple(returns))


def evaluate_paper(inputs: PaperInputs) -> GateResult:
    """Score the paper record of one frozen hash and decide paper → live."""
    out = Outcome()
    out.metric("frozen_hash", inputs.frozen_hash)
    if not inputs.gate_pass:
        out.reason(MISSING_RUN, "candidate_pass", "no gate pass with a completed holdout")
    if inputs.thresholds is None or inputs.first_started_at is None or inputs.starting_balance is None:
        out.reason(MISSING_RUN, "paper_runs")
        return GateResult(decide(out.reasons), tuple(out.reasons), out.metrics)
    t = inputs.thresholds.paper
    days = (inputs.evaluated_at - inputs.first_started_at).total_seconds() / 86_400
    out.metric("paper_days", Decimal(repr(days)).quantize(Decimal("0.01")))
    if days < t.min_days:
        out.reason(TOO_FEW_DAYS, "paper_duration", Decimal(repr(days)).quantize(Decimal("0.01")), t.min_days)
    record = paper_record(inputs.starting_balance, inputs.events)
    floor = t.trade_floor(inputs.strategy_kind)
    out.metric("paper_trades", record.trades)
    if record.trades < floor:
        out.reason(TOO_FEW_TRADES, "paper_trade_floor", record.trades, floor)
    out.metric("paper_return", record.paper_return.quantize(_SIX))
    if record.paper_return <= 0:
        out.reason(FAILED, "paper_net_positive", record.paper_return.quantize(_SIX), 0)
    out.metric("paper_max_drawdown", record.max_drawdown.quantize(_SIX))
    budget = inputs.thresholds.max_drawdown
    if record.max_drawdown > budget:
        out.reason(FAILED, "paper_drawdown_budget", record.max_drawdown.quantize(_SIX), budget)
    _lab_band(inputs, record, out)
    _fill_rate(inputs, out)
    _slippage(inputs, t, out)
    return GateResult(decide(out.reasons), tuple(out.reasons), out.metrics)


def _lab_band(inputs: PaperInputs, record: PaperRecord, out: Outcome) -> None:
    mean_return = sum(record.trade_returns, Decimal(0)) / len(record.trade_returns) if record.trade_returns else None
    out.metric("paper_mean_trade_return", None if mean_return is None else mean_return.quantize(_SIX))
    out.metric("holdout_trade_return_lower_bound", inputs.holdout_trade_bound)
    if inputs.holdout_trade_bound is None:
        out.reason(MISSING_METRIC, "paper_lab_band")
    elif mean_return is not None and mean_return < inputs.holdout_trade_bound:
        out.reason(FAILED, "paper_lab_band", mean_return.quantize(_SIX), inputs.holdout_trade_bound)


def paper_fill_rate(entries: Sequence[EntryOrder]) -> Decimal | None:
    """Filled post only entries over all post only entries, or None without one."""
    maker = [e for e in entries if e.post_only]
    return Decimal(sum(e.filled for e in maker)) / Decimal(len(maker)) if maker else None


def _fill_rate(inputs: PaperInputs, out: Outcome) -> None:
    """``fill_rate_drift``: maker entries only; not applicable to a taker study."""
    if inputs.entry_mode != "maker":
        out.metric("fill_rate_drift", None)
        return
    paper, lab = paper_fill_rate(inputs.entries), inputs.lab_fill_rate
    out.metric("paper_fill_rate", None if paper is None else paper.quantize(_SIX))
    out.metric("lab_fill_rate", lab)
    if paper is None or lab is None or lab == 0:
        out.reason(MISSING_METRIC, "fill_rate_drift")
        return
    drift = abs(paper / lab - 1)
    out.metric("fill_rate_drift", drift.quantize(_SIX))
    assert inputs.thresholds is not None
    tolerance = inputs.thresholds.paper.fill_rate_tolerance
    if drift > tolerance:
        out.reason(FAILED, "fill_rate_drift", drift.quantize(_SIX), tolerance)


def _slippage(inputs: PaperInputs, t: PaperThresholds, out: Outcome) -> None:
    """``slippage_drift``: mean paper entry slippage over the lab's by more than the tolerance fails."""
    values = [v for v in map(entry_slippage_bps, inputs.entry_fills) if v is not None]
    if not values:
        out.metric("paper_slippage_bps", None)
        out.reason(MISSING_METRIC, "slippage_drift", "no paper entry fill with a decision mid")
        return
    paper = sum(values, Decimal(0)) / len(values)
    out.metric("paper_slippage_bps", paper.quantize(_SIX))
    out.metric("lab_slippage_bps", inputs.lab_slippage_bps)
    if inputs.lab_slippage_bps is None:
        out.reason(MISSING_METRIC, "slippage_drift", "no lab slippage")
    elif paper - inputs.lab_slippage_bps > t.slippage_tolerance_bps:
        out.reason(FAILED, "slippage_drift", (paper - inputs.lab_slippage_bps).quantize(_SIX), t.slippage_tolerance_bps)
