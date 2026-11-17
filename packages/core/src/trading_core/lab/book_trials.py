"""The pre check trials over the pure book replay (spec 0033 AC-12, AC-12a, AC-12b).

``ewma_planner`` is ``trend_vol``'s plan with exponentially weighted volatility for the coins and the book.
``replay_trial`` replays a book hour by hour so that rules ``replay_book`` cannot express run on the same
mechanics: tranches (K sub books, each planned at its own cut hour at the full target and holding 1/K of
equity, the book trading the summed target at every sub book cut), and scale in plus a stop on one book
(``ScaleStop``). With one tranche and no rules it trades exactly as ``replay_book``; ``replay_book`` itself is
unchanged, so the baseline is never replayed by this module's code.

Scale in and stop (AC-12a): a coin's ramp follows the sign of its planned weight. A new side starts at ramp day
1 with the coin's planned vol as its entry vol; each later decision on the same side adds a day; the target is
the planned target times ``min(1, a / n)`` and the band is off while ``a <= n``. A zero signal, or an
ineligible coin, clears the state. The entry price is the first fill on the new side; until then no stop is
armed. At every whole hour close a coin with an armed stop is stopped when ``side x (close / entry - 1) <=
-k x entry vol``; the whole position closes at the fill delay after that hour (retrying each hour while the
fill is missing, counted in ``no_fill``), before any decision at that hour, and that coin skips the decision.
A stopped coin targets 0 until its signal sign differs from the stopped side.

No I/O, no float: every value is ``Decimal``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Final

from trading_core.lab.book_replay import (
    PRECISION,
    CoinData,
    DayResult,
    Measures,
    Planned,
    Planner,
    ReplayConfig,
    ReplayResult,
    cut_ns,
    measures,
)
from trading_core.lab.book_replay import _cuts as cut_closes
from trading_core.lab.book_replay import _day_of_ns as day_of_ns
from trading_core.lab.book_replay import _Position as Position
from trading_core.lab.book_replay import _trade as apply_trade
from trading_core.lab.candles import expected_charge_times
from trading_core.lab.funding import funding_amount
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import (
    CoinPlan,
    DailyClose,
    PortfolioParams,
    PortfolioPlan,
    close_day,
    order_delta,
    portfolio_plan,
)
from trading_core.strategy.trend_vol import DAYS_PER_YEAR, BookVolParams, within_band

# The EWMA variance is seeded with the sample variance of the first 30 returns (AC-12).
EWMA_SEED_RETURNS: Final = 30


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return value.sqrt()


def ewma_variance(returns: Sequence[Decimal], half_life: int, seed: int = EWMA_SEED_RETURNS) -> Decimal:
    """The EWMA variance of ``returns`` (oldest first): seeded with the sample variance (divisor n - 1) of the
    first ``seed`` returns, then ``var = lam x var + (1 - lam) x r^2`` (zero mean) over every later return,
    with ``lam = 0.5 ^ (1 / half_life)``. Raises ``ValueError`` with fewer than ``seed`` returns or a half life
    below 1."""
    if half_life < 1:
        raise ValueError(f"half_life must be at least 1, got {half_life}")
    if seed < 2 or len(returns) < seed:
        raise ValueError(f"the EWMA seed needs {seed} returns, got {len(returns)}")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        lam = Decimal("0.5") ** (Decimal(1) / Decimal(half_life))
    first = returns[:seed]
    mean = sum(first, Decimal(0)) / seed
    variance = sum(((r - mean) ** 2 for r in first), Decimal(0)) / (seed - 1)
    for r in returns[seed:]:
        variance = lam * variance + (1 - lam) * r * r
    return variance


def _returns(values: Sequence[Decimal]) -> list[Decimal]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values))]


def ewma_vol(closes: Sequence[Decimal], half_life: int) -> Decimal:
    """The EWMA standard deviation of the simple daily returns of ``closes`` (``ewma_variance``)."""
    return _sqrt(ewma_variance(_returns(closes), half_life))


def ewma_planner(params: PortfolioParams, book: BookVolParams, half_life: int) -> Planner:
    """``trend_vol``'s plan with EWMA vols (AC-12): eligibility and signals as ``portfolio_plan``, each
    eligible coin's weight ``signal x vol_target / ewma vol`` over N (0 at vol 0) capped at ``gross_cap``,
    then the book sized as ``book_vol_plan`` with the EWMA vol of the unit book's daily returns over every
    close the weighted coins share (newest aligned with newest), with the band."""
    if half_life < 1:
        raise ValueError(f"half_life must be at least 1, got {half_life}")

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        base = portfolio_plan(closes, equity, params)
        raw: dict[str, tuple[Decimal, Decimal]] = {}
        for coin, item in base.coins.items():
            if not item.eligible:
                continue
            vol = ewma_vol([c.close for c in closes[coin]], half_life)
            raw[coin] = (vol, Decimal(0) if vol == 0 else item.signal * params.vol_target / vol)
        n = len(raw)
        weights = {coin: w / n for coin, (_, w) in raw.items()} if n else {}
        gross: Decimal = sum((abs(w) for w in weights.values()), Decimal(0))
        scale = params.gross_cap / gross if gross > params.gross_cap else Decimal(1)
        sized = {coin: w * scale for coin, w in weights.items()}
        gross_now: Decimal = sum((abs(w) for w in sized.values()), Decimal(0))
        if gross_now == 0:
            coins = {
                coin: CoinPlan(
                    item.eligible,
                    item.signal,
                    raw[coin][0] if coin in raw else None,
                    Decimal(0),
                    Decimal(0),
                    item.close_date,
                )
                for coin, item in base.coins.items()
            }
            return Planned(PortfolioPlan(coins, n, gross, scale, equity, base.close_date), book.band, None, Decimal(0))
        unit = {coin: w / gross_now for coin, w in sized.items()}
        unit_vol = _sqrt(ewma_variance(_book_returns(closes, unit), half_life))
        daily_target = book.book_vol_target / _sqrt(Decimal(DAYS_PER_YEAR))
        book_gross = book.gross_cap if unit_vol == 0 else min(book.gross_cap, daily_target / unit_vol)
        coins = {}
        for coin, item in base.coins.items():
            weight = unit.get(coin, Decimal(0)) * book_gross
            coin_vol = raw[coin][0] if coin in raw else None
            coins[coin] = CoinPlan(item.eligible, item.signal, coin_vol, weight, weight * equity, item.close_date)
        scaled = PortfolioPlan(coins, n, gross, scale * book_gross / gross_now, equity, base.close_date)
        return Planned(scaled, book.band, unit_vol, book_gross)

    return plan


def _book_returns(closes: Mapping[str, Sequence[DailyClose]], unit: Mapping[str, Decimal]) -> list[Decimal]:
    weighted = [coin for coin, w in unit.items() if w != 0]
    length = min(len(closes[coin]) for coin in weighted) - 1
    book = [Decimal(0)] * length
    for coin in weighted:
        values = [c.close for c in closes[coin][-(length + 1) :]]
        for i in range(length):
            book[i] += unit[coin] * (values[i + 1] / values[i] - 1)
    return book


@dataclass(frozen=True)
class ScaleStop:
    """AC-12a's rules: scale a new side in over ``ramp_days`` decisions (1 is off) and stop a coin at
    ``stop_vols`` entry vols (None is off)."""

    ramp_days: int = 1
    stop_vols: Decimal | None = None

    def __post_init__(self) -> None:
        """Refuse a ramp below 1 day or a stop at or below 0 vols."""
        if self.ramp_days < 1:
            raise ValueError(f"ramp_days must be at least 1, got {self.ramp_days}")
        if self.stop_vols is not None and self.stop_vols <= 0:
            raise ValueError(f"stop_vols must be above 0, got {self.stop_vols}")


@dataclass(frozen=True)
class TrialResult:
    """A trial's replay plus the AC-12a report lines: the mean daily turnover (traded notional over equity),
    the share of coin days that ended stopped, and how many stops filled."""

    replay: ReplayResult
    avg_turnover: Decimal
    stopped_share: Decimal
    stops: int


@dataclass
class _State:
    side: int = 0
    days: int = 0
    entry: Decimal | None = None
    entry_vol: Decimal | None = None
    stopped: int = 0


@dataclass
class _Tally:
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    turnover: Decimal = Decimal(0)
    traded: int = 0
    band: int = 0
    small: int = 0
    close_missing: int = 0
    no_fill: int = 0
    closed: int = 0
    stops: int = 0
    gaps: dict[str, Decimal] = field(default_factory=dict)


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def tranche_hours(cut_hour: int, tranches: int) -> list[int]:
    """The sub book cut hours: ``cut_hour`` then every ``24 / tranches`` hours, all inside the same UTC day.
    Raises ``ValueError`` when ``tranches`` does not divide 24 or the last cut passes 23:00."""
    if tranches < 1 or 24 % tranches:
        raise ValueError(f"tranches must divide 24, got {tranches}")
    step = 24 // tranches
    hours = [cut_hour + j * step for j in range(tranches)]
    if hours[-1] > 23:
        raise ValueError(f"cut hour {cut_hour} with {tranches} tranches passes 23:00")
    return hours


def replay_trial(
    coins: Mapping[str, CoinData],
    planner: Planner,
    config: ReplayConfig,
    *,
    tranches: int = 1,
    rules: ScaleStop | None = None,
    midpoint: date | None = None,
) -> TrialResult:
    """Replay the book from flat (module docstring), one record a day at the first sub book's fill.

    Raises ``ValueError`` for bad tranches, rules with more than one tranche, or a run without days.
    """
    hours = tranche_hours(config.cut_hour, tranches)
    if rules is not None and tranches != 1:
        raise ValueError("scale in and stop rules run on one book only")
    cuts = [{coin: cut_closes(data, hour) for coin, data in coins.items()} for hour in hours]
    plans: list[Planned | None] = [None] * tranches
    positions = {coin: Position() for coin in coins}
    states = {coin: _State() for coin in coins}
    marks: dict[str, Decimal] = {}
    pending: set[str] = set()
    cash = config.equity
    tally = _Tally()
    days: list[DayResult] = []
    missing_days: set[date] = set()
    turnovers: list[Decimal] = []
    stopped_days = 0
    stops = 0
    prev_fill: int | None = None
    prev_btc: Decimal | None = None
    planned_first = False
    ruined = False
    day = config.start
    hour_ns = cut_ns(config.start, hours[0])
    last_ns = cut_ns(config.end, hours[0])
    while hour_ns <= last_ns:
        now = hour_ns
        hour_ns += HOUR_NS
        tranche = hours.index((now // HOUR_NS) % 24) if (now // HOUR_NS) % 24 in hours else None
        if rules is not None and rules.stop_vols is not None:
            for coin in sorted(coins):
                state, pos = states[coin], positions[coin]
                close = coins[coin].hourly.get(now)
                if (
                    coin in pending
                    or close is None
                    or state.entry is None
                    or state.entry_vol is None
                    or pos.qty == 0
                    or _sign(pos.qty) != state.side
                ):
                    continue
                if state.side * (close / state.entry - 1) <= -rules.stop_vols * state.entry_vol:
                    pending.add(coin)
        if tranche is None and not pending:
            continue
        fill_ns = now + config.fill_delay_ns
        if prev_fill is not None:
            for charge in expected_charge_times(prev_fill, fill_ns):
                for coin, pos in positions.items():
                    mid = coins[coin].hourly.get(charge)
                    if mid is not None:
                        marks[coin] = mid
                    if pos.qty == 0:
                        continue
                    rate = coins[coin].funding.get(charge)
                    if rate is None:
                        missing_days.add(day_of_ns(charge))
                        continue
                    paid = funding_amount(pos.qty, marks[coin], rate)
                    cash += paid
                    tally.funding += paid
        prev_fill = fill_ns
        fills = {coin: coins[coin].minutes.get(fill_ns) for coin in coins}
        for coin, price in fills.items():
            if price is not None:
                marks[coin] = price
        equity = cash + _unrealised(positions, marks)
        if equity <= 0:
            ruined = True
            break
        stopped_now: set[str] = set()
        for coin in sorted(pending):
            price = fills[coin]
            if price is None:
                tally.no_fill += 1
                continue
            pos, state = positions[coin], states[coin]
            delta = -pos.qty
            realised, shut = apply_trade(pos, delta, price)
            fee = abs(delta) * price * config.cost
            cash += realised - fee
            tally.fees += fee
            tally.turnover += abs(delta) * price
            tally.traded += 1
            tally.closed += shut
            tally.stops += 1
            stops += 1
            states[coin] = _State(stopped=state.side)
            stopped_now.add(coin)
        pending -= stopped_now
        if tranche is not None:
            planned = False
            try:
                fresh = planner({c: cut.until(now, config.keep_days) for c, cut in cuts[tranche].items()}, equity)
            except ValueError:
                fresh = None
            if fresh is not None and fresh.plan.n > 0:
                plans[tranche], planned = fresh, True
            current = plans[tranche]
            if tranche == 0:
                planned_first = planned
            if planned and current is not None:
                plan_day = close_day(now - hours[tranche] * HOUR_NS)
                for coin in sorted(coins):
                    item = current.plan.coins[coin]
                    if item.eligible and item.close_date != plan_day:
                        tally.close_missing += 1
                        continue
                    if coin in stopped_now or coin in pending:
                        continue
                    if tranches == 1:
                        target = item.target_notional
                    else:
                        weight: Decimal = sum((p.plan.coins[coin].weight for p in plans if p is not None), Decimal(0))
                        target = weight * equity / tranches
                    band = current.band
                    state = states[coin]
                    if rules is not None:
                        target, band = _ruled(state, item, target, band, rules)
                    price = fills[coin]
                    if price is None:
                        tally.no_fill += 1
                        continue
                    pos = positions[coin]
                    if target != 0:
                        tally.gaps[coin] = abs(target - pos.qty * price) / abs(target)
                    if within_band(target, pos.qty * price, band):
                        tally.band += 1
                        continue
                    spec = coins[coin].spec
                    delta_or_none = order_delta(target, price, pos.qty, spec.size_increment, spec.min_quantity)
                    if delta_or_none is None:
                        tally.small += 1
                        continue
                    realised, shut = apply_trade(pos, delta_or_none, price)
                    fee = abs(delta_or_none) * price * config.cost
                    cash += realised - fee
                    tally.fees += fee
                    tally.turnover += abs(delta_or_none) * price
                    tally.traded += 1
                    tally.closed += shut
                    if (
                        rules is not None
                        and state.side != 0
                        and state.entry is None
                        and pos.qty != 0
                        and _sign(pos.qty) == state.side
                    ):
                        state.entry = price
        if tranche != 0:
            continue
        equity = cash + _unrealised(positions, marks)
        notional = sum((abs(p.qty * marks[c]) for c, p in positions.items() if p.qty != 0), Decimal(0))
        btc_price = fills.get(config.btc)
        btc_return = None if btc_price is None or prev_btc is None else btc_price / prev_btc - 1
        prev_btc = btc_price
        first = plans[0]
        days.append(
            DayResult(
                day,
                fill_ns,
                equity,
                notional / equity if equity > 0 else Decimal(0),
                tally.fees,
                tally.funding,
                planned_first,
                tally.traded,
                tally.band,
                tally.small,
                tally.close_missing,
                tally.no_fill,
                tally.closed,
                first.unit_vol if planned_first and first is not None else None,
                _book_gross(plans, tranches) if planned_first else None,
                btc_return,
                {c: p.qty for c, p in sorted(positions.items())},
                dict(tally.gaps),
            )
        )
        turnovers.append(tally.turnover / equity if equity > 0 else Decimal(0))
        stopped_days += sum(1 for s in states.values() if s.stopped)
        tally = _Tally()
        if equity <= 0:
            ruined = True
            break
        day += timedelta(days=1)
    if not days:
        raise ValueError(f"no day was replayed from {config.start}")
    replay = _result(days, config.equity, midpoint, len(missing_days), ruined)
    coin_days = len(days) * len(coins)
    return TrialResult(
        replay,
        sum(turnovers, Decimal(0)) / len(turnovers),
        Decimal(stopped_days) / coin_days if coin_days else Decimal(0),
        stops,
    )


def _ruled(state: _State, item: CoinPlan, target: Decimal, band: Decimal, rules: ScaleStop) -> tuple[Decimal, Decimal]:
    """Apply the scale in and stop state to one coin's decision; returns the target and band to trade."""
    signal = _sign(item.weight) if item.eligible else 0
    if state.stopped:
        if signal == state.stopped:
            return Decimal(0), band
        state.stopped = 0
    if signal == 0:
        state.side, state.days, state.entry, state.entry_vol = 0, 0, None, None
        return Decimal(0), band
    if signal != state.side:
        state.side, state.days, state.entry, state.entry_vol = signal, 1, None, item.vol
    else:
        state.days += 1
    if rules.ramp_days > 1 and state.days <= rules.ramp_days:
        return target * Decimal(state.days) / Decimal(rules.ramp_days), Decimal(0)
    return target, band


def _unrealised(positions: Mapping[str, Position], marks: Mapping[str, Decimal]) -> Decimal:
    return sum((p.qty * (marks[c] - p.avg) for c, p in positions.items() if p.qty != 0), Decimal(0))


def _book_gross(plans: Sequence[Planned | None], tranches: int) -> Decimal:
    return sum((p.gross for p in plans if p is not None), Decimal(0)) / tranches


def _result(
    days: Sequence[DayResult], start_equity: Decimal, midpoint: date | None, missing: int, ruined: bool
) -> ReplayResult:
    split: date = midpoint if midpoint is not None else days[len(days) // 2].day
    first_half = [d for d in days if d.day < split]
    second_half = [d for d in days if d.day >= split]
    if not first_half or not second_half:
        raise ValueError(f"midpoint {split} leaves a half without days")
    halves: tuple[Measures, Measures] = (
        measures(first_half, start_equity),
        measures(second_half, first_half[-1].equity),
    )
    years: dict[int, Measures] = {}
    equity = start_equity
    for year in sorted({d.day.year for d in days}):
        chunk = [d for d in days if d.day.year == year]
        years[year] = measures(chunk, equity)
        equity = chunk[-1].equity
    return ReplayResult(days, start_equity, split, measures(days, start_equity), halves, years, missing, ruined)
