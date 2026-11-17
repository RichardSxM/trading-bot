"""The pure book replay (spec 0033 AC-9): a book strategy replayed day by day with the trader's own pure rule.

Every trading day ``T`` the replay plans at ``cut_hour`` (from the hourly closes ending at that hour, one per
day, the newest ``keep_days`` of them, as the trader keeps its daily closes) and trades at the one minute
close ending five minutes later (the trader plans at 00:00 and trades at 00:05; nothing fills on the bar
that made the signal). The plan comes from a ``Planner``, which calls the same functions the trader runs
(``portfolio_plan``, ``book_vol_plan``); each coin then follows the trader's step: an eligible coin whose
newest close is not the plan day keeps its position, a position ``within_band`` of its target is left
alone, and ``order_delta`` (the pinned ``size_increment`` and ``min_quantity``) gives the trade or None.

Equity for the plan is marked equity: cash plus every position's unrealised P&L at the fill prices. Each
trade pays ``cost`` (7 bps a side) on its traded notional. Funding is charged at every charge time between
two fills, ``-signed_qty * mid * rate`` as ``lab/funding.py``, with the hourly close at the charge time as
the mid; a charge time with no stored rate is skipped and its day counted. No I/O, no float: every value is
``Decimal``, square roots and powers at 28 digits.

Two mechanics are not in the trader and are kept simple on purpose: a coin with no minute close at ``H:05``
is not traded that day (the trader's stale quote) and is counted, and no risk check clips the book.
"""

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Final

from trading_core.lab.candles import MINUTE_NS, expected_charge_times
from trading_core.lab.funding import funding_amount
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.strategy.trend_portfolio import (
    CoinPlan,
    DailyClose,
    PortfolioParams,
    PortfolioPlan,
    close_day,
    order_delta,
    portfolio_plan,
)
from trading_core.strategy.trend_vol import BookVolParams, book_vol_plan, within_band

# The trader trades five minutes after the cut (``TV_RUN_AT_UTC`` 00:05 against the 00:00 daily close).
FILL_DELAY_NS: Final = 5 * MINUTE_NS
DAYS_PER_YEAR: Final = 365
PRECISION: Final = 28
# 7 bps a side on traded notional (AC-9).
DEFAULT_COST: Final = Decimal("0.0007")
BTC: Final = "PF_XBTUSD.KRAKEN"


@dataclass(frozen=True)
class CoinSpec:
    """The pinned instrument figures a trade is rounded to."""

    size_increment: Decimal
    min_quantity: Decimal | None = None


@dataclass(frozen=True)
class CoinData:
    """One coin's inputs: hourly closes keyed by the hour's end, minute closes keyed by the minute's end (at
    least the ``H:05`` ones), funding rates keyed by their charge time, and the pinned figures."""

    hourly: Mapping[int, Decimal]
    minutes: Mapping[int, Decimal]
    funding: Mapping[int, Decimal]
    spec: CoinSpec


@dataclass(frozen=True)
class Planned:
    """A plan as the replay trades it: the plan, the no trade band (0 is off), the book's unit vol (None when
    not vol sized), and the book gross it was sized to."""

    plan: PortfolioPlan
    band: Decimal
    unit_vol: Decimal | None
    gross: Decimal


# From the closes up to the plan day and the marked equity, the plan; ``ValueError`` (``PortfolioError`` is one)
# means no plan that day, and the replay then keeps the old plan and does not rebalance, as the trader does.
Planner = Callable[[Mapping[str, Sequence[DailyClose]], Decimal], Planned]


def trend_daily_planner(params: PortfolioParams) -> Planner:
    """``trend_daily``'s plan: ``portfolio_plan`` as is, no band."""

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        base = portfolio_plan(closes, equity, params)
        gross: Decimal = sum((abs(item.weight) for item in base.coins.values()), Decimal(0))
        return Planned(base, Decimal(0), None, gross)

    return plan


def trend_vol_planner(params: PortfolioParams, book: BookVolParams) -> Planner:
    """``trend_vol``'s plan: ``portfolio_plan`` rescaled by ``book_vol_plan``, with its band."""

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        sized = book_vol_plan(closes, portfolio_plan(closes, equity, params), book)
        return Planned(sized.plan, book.band, sized.unit_vol, sized.gross)

    return plan


# A band so wide that a position on the target's side is never resized: ``sign_planner`` holds until a flip.
HOLD_BAND: Final = Decimal(1_000_000_000)


def sign_planner(lookback_days: int, min_history_days: int) -> Planner:
    """The plain sign rule for the Jesse cross check (spec 0033 AC-11): every coin with at least
    ``min_history_days`` closes gets the sign of ``close[t] / close[t - lookback_days] - 1`` over N eligible
    coins as its weight (gross 1, so one coin is all in), with ``HOLD_BAND``: a position is set at gross 1 when
    it opens or flips and then held, as a single coin Jesse strategy trades it. Raises ``ValueError`` when
    ``min_history_days`` is not above ``lookback_days``."""
    if min_history_days <= lookback_days:
        raise ValueError(f"min_history_days must be above lookback_days {lookback_days}, got {min_history_days}")

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        if equity <= 0:
            raise ValueError(f"equity must be above 0, got {equity}")
        signals = {
            coin: _sign(series[-1].close / series[-1 - lookback_days].close - 1)
            for coin, series in closes.items()
            if len(series) >= min_history_days
        }
        n = len(signals)
        coins: dict[str, CoinPlan] = {}
        for coin, series in closes.items():
            last_day = series[-1].day if series else None
            signal = signals.get(coin)
            if signal is None:
                coins[coin] = CoinPlan(False, 0, None, Decimal(0), Decimal(0), last_day)
                continue
            weight = Decimal(signal) / n
            coins[coin] = CoinPlan(True, signal, None, weight, weight * equity, last_day)
        gross: Decimal = sum((abs(c.weight) for c in coins.values()), Decimal(0))
        dates = [c.close_date for c in coins.values() if c.close_date is not None]
        book = PortfolioPlan(coins, n, gross, Decimal(1), equity, max(dates, default=None))
        return Planned(book, HOLD_BAND, None, gross)

    return plan


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


@dataclass(frozen=True)
class ReplayConfig:
    """The run: the cut hour (0 to 23), the first and last traded UTC day (both inclusive), the starting
    equity, the cost a side, how many daily closes are kept, and the coin the correlation is taken with."""

    cut_hour: int
    start: date
    end: date
    equity: Decimal = Decimal(10000)
    cost: Decimal = DEFAULT_COST
    keep_days: int = 150
    btc: str = BTC

    def __post_init__(self) -> None:
        """Refuse a cut hour outside 0 to 23, an end before the start, equity at or below 0, a negative cost,
        or keeping fewer than 2 closes."""
        if not 0 <= self.cut_hour <= 23:
            raise ValueError(f"cut_hour must be 0 to 23, got {self.cut_hour}")
        if self.end < self.start:
            raise ValueError(f"end {self.end} is before start {self.start}")
        if self.equity <= 0:
            raise ValueError(f"equity must be above 0, got {self.equity}")
        if self.cost < 0:
            raise ValueError(f"cost must be at least 0, got {self.cost}")
        if self.keep_days < 2:
            raise ValueError(f"keep_days must be at least 2, got {self.keep_days}")


@dataclass(frozen=True)
class DayResult:
    """One traded day: its equity after the day's trades (marked at the fill prices), the book's gross then
    (absolute notional over equity), what it paid, and what each coin did.

    ``positions`` is each coin's signed quantity after the day's trades. ``band_gaps`` is, for each coin the
    day's plan judged at a fill price with a target other than 0, how far its position was from the target
    before the trade, as a share of the target (``|target - qty x price| / |target|``); the band edge is
    where it equals the band (spec 0033 AC-10a)."""

    day: date
    fill_ns: int
    equity: Decimal
    gross: Decimal
    fees: Decimal
    funding: Decimal
    planned: bool
    traded: int
    band: int
    small: int
    close_missing: int
    no_fill: int
    closed: int
    unit_vol: Decimal | None
    book_gross: Decimal | None
    btc_return: Decimal | None
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    band_gaps: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Measures:
    """A period's figures (AC-9). A figure that cannot be computed (too few days, no spread, no drawdown)
    is None."""

    first: date
    last: date
    days: int
    end_multiple: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    max_drawdown: Decimal
    avg_gross: Decimal
    closed: int
    btc_corr: Decimal | None


@dataclass(frozen=True)
class ReplayResult:
    """Every traded day, the figures for the whole run, each half (split at ``midpoint``: days before it are
    the first half), and each UTC year, the days with a missing funding rate on a held coin, and whether
    equity fell to 0 (the run then stops)."""

    days: Sequence[DayResult]
    start_equity: Decimal
    midpoint: date
    full: Measures
    halves: tuple[Measures, Measures]
    years: Mapping[int, Measures]
    funding_missing_days: int
    ruined: bool


def cut_ns(day: date, hour: int) -> int:
    """The nanosecond of ``hour``:00 UTC on ``day``."""
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()) * SECOND_NS


def _day_of_ns(ns: int) -> date:
    return datetime.fromtimestamp(ns // SECOND_NS, UTC).date()


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return value.sqrt()


@dataclass
class _Cuts:
    """One coin's daily closes at the cut hour, by cut time, labelled with their plan day."""

    times: list[int] = field(default_factory=list)
    closes: list[DailyClose] = field(default_factory=list)

    def until(self, ns: int, keep: int) -> list[DailyClose]:
        end = bisect_right(self.times, ns)
        return self.closes[max(0, end - keep) : end]

    def count_until(self, ns: int) -> int:
        return bisect_right(self.times, ns)


def _cuts(data: CoinData, hour: int) -> _Cuts:
    cuts = _Cuts()
    for ns in sorted(data.hourly):
        if (ns // HOUR_NS) % 24 == hour and ns % HOUR_NS == 0:
            cuts.times.append(ns)
            cuts.closes.append(DailyClose(close_day(ns - hour * HOUR_NS), data.hourly[ns]))
    return cuts


def first_ready_day(coins: Mapping[str, CoinData], cut_hour: int, min_history_days: int) -> date | None:
    """The first UTC day on which every coin has at least ``min_history_days`` closes at the cut hour (the
    day of that cut), or None when some coin never has."""
    ready: list[int] = []
    for data in coins.values():
        cuts = _cuts(data, cut_hour)
        if len(cuts.times) < min_history_days:
            return None
        ready.append(cuts.times[min_history_days - 1])
    return _day_of_ns(max(ready)) if ready else None


@dataclass
class _Position:
    qty: Decimal = Decimal(0)
    avg: Decimal = Decimal(0)


def _trade(pos: _Position, delta: Decimal, price: Decimal) -> tuple[Decimal, int]:
    """Apply a signed trade at ``price``; returns the realised P&L and how many positions it closed."""
    qty = pos.qty
    if qty == 0 or (qty > 0) == (delta > 0):
        new = qty + delta
        pos.avg = (abs(qty) * pos.avg + abs(delta) * price) / abs(new)
        pos.qty = new
        return Decimal(0), 0
    closing = min(abs(delta), abs(qty))
    realised = closing * (price - pos.avg) * (1 if qty > 0 else -1)
    new = qty + delta
    if new == 0:
        pos.qty, pos.avg = Decimal(0), Decimal(0)
        return realised, 1
    if (new > 0) != (qty > 0):
        pos.qty, pos.avg = new, price
        return realised, 1
    pos.qty = new
    return realised, 0


def replay_book(
    coins: Mapping[str, CoinData], planner: Planner, config: ReplayConfig, midpoint: date | None = None
) -> ReplayResult:
    """Replay the book from flat over ``config.start`` to ``config.end`` (module docstring).

    ``midpoint`` splits the traded days into halves; by default it is the middle traded day.
    """
    cuts = {coin: _cuts(data, config.cut_hour) for coin, data in coins.items()}
    positions = {coin: _Position() for coin in coins}
    marks: dict[str, Decimal] = {}
    cash = config.equity
    plan: Planned | None = None
    days: list[DayResult] = []
    missing_days: set[date] = set()
    prev_fill_ns: int | None = None
    prev_btc: Decimal | None = None
    ruined = False
    day = config.start
    while day <= config.end and not ruined:
        cut = cut_ns(day, config.cut_hour)
        fill_ns = cut + FILL_DELAY_NS
        funding_paid = Decimal(0)
        if prev_fill_ns is not None:
            for charge in expected_charge_times(prev_fill_ns, fill_ns):
                for coin, pos in positions.items():
                    mid = coins[coin].hourly.get(charge)
                    if mid is not None:
                        marks[coin] = mid
                    if pos.qty == 0:
                        continue
                    rate = coins[coin].funding.get(charge)
                    if rate is None:
                        missing_days.add(_day_of_ns(charge))
                        continue
                    funding_paid += funding_amount(pos.qty, marks[coin], rate)
        cash += funding_paid
        fills = {coin: coins[coin].minutes.get(fill_ns) for coin in coins}
        for coin, price in fills.items():
            if price is not None:
                marks[coin] = price
        equity = cash + sum((p.qty * (marks[c] - p.avg) for c, p in positions.items() if p.qty != 0), Decimal(0))
        if equity <= 0:
            ruined = True
            break
        plan_day = close_day(cut - config.cut_hour * HOUR_NS)
        closes = {coin: c.until(cut, config.keep_days) for coin, c in cuts.items()}
        planned = False
        try:
            fresh = planner(closes, equity)
        except ValueError:
            fresh = None
        if fresh is not None and fresh.plan.n > 0:
            plan, planned = fresh, True
        fees = Decimal(0)
        traded = band = small = close_missing = no_fill = closed = 0
        gaps: dict[str, Decimal] = {}
        if planned and plan is not None:
            for coin in sorted(coins):
                item = plan.plan.coins[coin]
                if item.eligible and item.close_date != plan_day:
                    close_missing += 1
                    continue
                price = fills[coin]
                if price is None:
                    no_fill += 1
                    continue
                pos = positions[coin]
                if item.target_notional != 0:
                    gaps[coin] = abs(item.target_notional - pos.qty * price) / abs(item.target_notional)
                if within_band(item.target_notional, pos.qty * price, plan.band):
                    band += 1
                    continue
                spec = coins[coin].spec
                delta = order_delta(item.target_notional, price, pos.qty, spec.size_increment, spec.min_quantity)
                if delta is None:
                    small += 1
                    continue
                realised, shut = _trade(pos, delta, price)
                fee = abs(delta) * price * config.cost
                cash += realised - fee
                fees += fee
                traded += 1
                closed += shut
        equity = cash + sum((p.qty * (marks[c] - p.avg) for c, p in positions.items() if p.qty != 0), Decimal(0))
        notional = sum((abs(p.qty * marks[c]) for c, p in positions.items() if p.qty != 0), Decimal(0))
        btc_price = fills.get(config.btc)
        btc_return = None if btc_price is None or prev_btc is None else btc_price / prev_btc - 1
        prev_btc = btc_price
        days.append(
            DayResult(
                day,
                fill_ns,
                equity,
                notional / equity if equity > 0 else Decimal(0),
                fees,
                funding_paid,
                planned,
                traded,
                band,
                small,
                close_missing,
                no_fill,
                closed,
                plan.unit_vol if planned and plan is not None else None,
                plan.gross if planned and plan is not None else None,
                btc_return,
                {c: p.qty for c, p in sorted(positions.items())},
                gaps,
            )
        )
        if equity <= 0:
            ruined = True
        prev_fill_ns = fill_ns
        day += timedelta(days=1)
    if not days:
        raise ValueError(f"no day was replayed from {config.start}")
    split: date = midpoint if midpoint is not None else days[len(days) // 2].day
    full = measures(days, config.equity)
    first_half = [d for d in days if d.day < split]
    second_half = [d for d in days if d.day >= split]
    if not first_half or not second_half:
        raise ValueError(f"midpoint {split} leaves a half without days")
    halves = (measures(first_half, config.equity), measures(second_half, first_half[-1].equity))
    years: dict[int, Measures] = {}
    start_equity = config.equity
    for year in sorted({d.day.year for d in days}):
        chunk = [d for d in days if d.day.year == year]
        years[year] = measures(chunk, start_equity)
        start_equity = chunk[-1].equity
    return ReplayResult(days, config.equity, split, full, halves, years, len(missing_days), ruined)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _stdev(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    return _sqrt(sum(((v - mean) ** 2 for v in values), Decimal(0)) / (len(values) - 1))


def correlation(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal | None:
    """The Pearson correlation of two equal length series, or None with fewer than 2 points or no spread."""
    if len(xs) != len(ys):
        raise ValueError(f"series lengths differ: {len(xs)} and {len(ys)}")
    if len(xs) < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    cov = sum(((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)), Decimal(0))
    vx = sum(((x - mx) ** 2 for x in xs), Decimal(0))
    vy = sum(((y - my) ** 2 for y in ys), Decimal(0))
    if vx == 0 or vy == 0:
        return None
    return cov / _sqrt(vx * vy)


def measures(days: Sequence[DayResult], start_equity: Decimal) -> Measures:
    """The AC-9 figures of a run of consecutive days that starts from ``start_equity``.

    Daily returns are equity over the day before minus 1. Sharpe is their mean over their sample standard
    deviation, Sortino their mean over the downside deviation (target 0, root mean square of the negative
    returns over every day), both times sqrt 365. Calmar is CAGR (end multiple to the power 365 over the day
    count, minus 1) over max drawdown, the largest fall from a peak of the equity series that starts at
    ``start_equity``. Average gross is the mean of the days' gross; the BTC correlation pairs each day's
    return with BTC's fill to fill return where both exist.
    """
    if not days:
        raise ValueError("measures need at least one day")
    equities = [start_equity, *(d.equity for d in days)]
    returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
    root_year = _sqrt(Decimal(DAYS_PER_YEAR))
    spread = _stdev(returns)
    sharpe = None if spread is None or spread == 0 else _mean(returns) / spread * root_year
    downside = _sqrt(sum((min(r, Decimal(0)) ** 2 for r in returns), Decimal(0)) / len(returns))
    sortino = None if downside == 0 or len(returns) < 2 else _mean(returns) / downside * root_year
    peak, worst = equities[0], Decimal(0)
    for value in equities:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    multiple = equities[-1] / equities[0]
    calmar: Decimal | None = None
    if worst > 0 and multiple > 0:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            cagr = multiple ** (Decimal(DAYS_PER_YEAR) / len(days)) - 1
        calmar = cagr / worst
    pairs = [(r, d.btc_return) for r, d in zip(returns, days, strict=True) if d.btc_return is not None]
    corr = correlation([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
    return Measures(
        days[0].day,
        days[-1].day,
        len(days),
        multiple,
        sharpe,
        sortino,
        calmar,
        worst,
        _mean([d.gross for d in days]),
        sum(d.closed for d in days),
        corr,
    )
