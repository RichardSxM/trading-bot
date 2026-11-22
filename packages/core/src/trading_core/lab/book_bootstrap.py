"""Block bootstrap reruns and the study's benchmarks (spec 0033 AC-14, AC-14a, AC-15, AC-15a, AC-16).

``bootstrap_paths`` builds synthetic daily price paths for a set of coins from real daily returns drawn in
moving blocks on shared dates; ``path_coins`` turns one path into the replay's ``CoinData`` (closes at the
00:00 key, fills at 00:05 at the same price, the drawn day's summed funding on the 01:00 charge key), so the
AC-9 replay reruns the rule on it unchanged. ``hold_planner`` and ``unit_gross`` make the benchmarks' gross 1
plans, and ``scaled_days`` puts a run at another average gross. No I/O, no float.
"""

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from trading_core.lab.book_replay import (
    FILL_DELAY_NS,
    CoinData,
    CoinSpec,
    DayResult,
    Planned,
    Planner,
    cut_ns,
)
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import CoinPlan, DailyClose, PortfolioPlan

ONE_DAY: Final = timedelta(days=1)
FUNDING_KEY_NS: Final = HOUR_NS  # a path day's funding sits on the 01:00 charge after its 00:00 close


def daily_closes(data: CoinData) -> dict[date, Decimal]:
    """A coin's 00:00 UTC hourly closes keyed by the day of that 00:00."""
    out: dict[date, Decimal] = {}
    for ns, close in data.hourly.items():
        if ns % (24 * HOUR_NS) == 0:
            out[date(1970, 1, 1) + timedelta(days=ns // (24 * HOUR_NS))] = close
    return out


def daily_funding(data: CoinData, days: Sequence[date]) -> tuple[dict[date, Decimal], int]:
    """For each day D, the sum of the coin's stored rates charged after D - 1 00:00 up to D 00:00 (the
    interval of D's return), and how many days had no stored rate (they sum to 0)."""
    by_day: dict[date, Decimal] = {}
    missing = 0
    for day in days:
        start, end = cut_ns(day - ONE_DAY, 0), cut_ns(day, 0)
        rates = [r for ns, r in data.funding.items() if start < ns <= end]
        by_day[day] = sum(rates, Decimal(0))
        missing += 0 if rates else 1
    return by_day, missing


def pool_runs(closes: Mapping[str, Mapping[date, Decimal]], first: date, last: date) -> list[list[date]]:
    """The maximal runs of consecutive days from ``first`` to ``last`` on which every coin has its close and
    the close the day before (so every coin has that day's return)."""
    runs: list[list[date]] = []
    current: list[date] = []
    day = first
    while day <= last:
        ok = all(day in c and day - ONE_DAY in c for c in closes.values())
        if ok:
            current.append(day)
        elif current:
            runs.append(current)
            current = []
        day += ONE_DAY
    if current:
        runs.append(current)
    return runs


def block_starts(runs: Sequence[Sequence[date]], block: int) -> list[date]:
    """Every day a block of ``block`` days can start on without leaving its run."""
    return [run[i] for run in runs for i in range(len(run) - block + 1)]


def bootstrap_paths(runs: Sequence[Sequence[date]], block: int, length: int, count: int, seed: int) -> list[list[date]]:
    """``count`` paths of ``length`` drawn days each: blocks of ``block`` consecutive days, each start drawn
    uniformly from ``block_starts`` by one ``random.Random(seed)`` in path order, the last block cut. Raises
    ``ValueError`` when no block fits."""
    starts = block_starts(runs, block)
    if not starts:
        raise ValueError(f"no {block} day block fits in the pool")
    rng = random.Random(seed)
    paths: list[list[date]] = []
    for _ in range(count):
        path: list[date] = []
        while len(path) < length:
            start = starts[rng.randrange(len(starts))]
            path.extend(start + timedelta(days=i) for i in range(block))
        paths.append(path[:length])
    return paths


@dataclass(frozen=True)
class CoinHistory:
    """One coin's real daily closes, daily funding sums, and pinned figures."""

    closes: Mapping[date, Decimal]
    funding: Mapping[date, Decimal]
    spec: CoinSpec


def path_coins(
    history: Mapping[str, CoinHistory], warmup: Sequence[date], path_days: Sequence[date], drawn: Sequence[date]
) -> dict[str, CoinData]:
    """One path's ``CoinData`` per coin: the real closes on the ``warmup`` days, then on each ``path_days[i]``
    the price before times one plus the coin's real return on ``drawn[i]``, with that drawn day's funding on
    the 01:00 charge of the day before. Closes sit at 00:00 and the fill at 00:05 at the same price."""
    if len(path_days) != len(drawn):
        raise ValueError(f"{len(path_days)} path days but {len(drawn)} drawn days")
    out: dict[str, CoinData] = {}
    for coin, h in history.items():
        hourly: dict[int, Decimal] = {}
        funding: dict[int, Decimal] = {}
        price: Decimal | None = None
        for day in warmup:
            close = h.closes.get(day)
            if close is not None:
                hourly[cut_ns(day, 0)] = close
                price = close
        if price is None:
            raise ValueError(f"{coin} has no warmup close")
        for day, source in zip(path_days, drawn, strict=True):
            price = price * (h.closes[source] / h.closes[source - ONE_DAY])
            hourly[cut_ns(day, 0)] = price
            rate = h.funding.get(source, Decimal(0))
            if rate:
                funding[cut_ns(day - ONE_DAY, 0) + FUNDING_KEY_NS] = rate
        minutes = {ns + FILL_DELAY_NS: close for ns, close in hourly.items()}
        out[coin] = CoinData(hourly, minutes, funding, h.spec)
    return out


def nearest_rank(values: Sequence[Decimal], p: Decimal) -> Decimal:
    """The value at rank ceil(p x n) (1 based) of the sorted values; the 95th of 1000 is the 950th."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def hold_planner(min_history_days: int, coins: Sequence[str] | None = None) -> Planner:
    """Long every coin (or only ``coins``) with at least ``min_history_days`` closes, each at weight 1 over
    their count, no band (AC-14a's hold BTC and hold all 12)."""

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        if equity <= 0:
            raise ValueError(f"equity must be above 0, got {equity}")
        chosen = [c for c, s in closes.items() if (coins is None or c in coins) and len(s) >= min_history_days]
        n = len(chosen)
        plans: dict[str, CoinPlan] = {}
        for coin, series in closes.items():
            last = series[-1].day if series else None
            if coin in chosen:
                weight = Decimal(1) / n
                plans[coin] = CoinPlan(True, 1, None, weight, weight * equity, last)
            else:
                plans[coin] = CoinPlan(False, 0, None, Decimal(0), Decimal(0), last)
        dates = [p.close_date for p in plans.values() if p.close_date is not None]
        gross = Decimal(1) if n else Decimal(0)
        return Planned(
            PortfolioPlan(plans, n, gross, Decimal(1), equity, max(dates, default=None)), Decimal(0), None, gross
        )

    return plan


def unit_gross(planner: Planner) -> Planner:
    """``planner`` with each day's weights divided by their gross (weights 0 on a day whose gross is 0), no
    band (AC-14a's ``trend_daily`` at gross 1)."""

    def plan(closes: Mapping[str, Sequence[DailyClose]], equity: Decimal) -> Planned:
        base = planner(closes, equity)
        gross: Decimal = sum((abs(c.weight) for c in base.plan.coins.values()), Decimal(0))
        coins = {
            coin: replace(
                item,
                weight=Decimal(0) if gross == 0 else item.weight / gross,
                target_notional=Decimal(0) if gross == 0 else item.weight / gross * equity,
            )
            for coin, item in base.plan.coins.items()
        }
        unit = Decimal(1) if gross else Decimal(0)
        return Planned(replace(base.plan, coins=coins), Decimal(0), base.unit_vol, unit)

    return plan


def scaled_days(days: Sequence[DayResult], start_equity: Decimal, scale: Decimal) -> list[DayResult]:
    """``days`` with every daily return times ``scale``, the equity rebuilt from ``start_equity`` and the gross
    times ``scale``; counts and the BTC return are kept (AC-14a)."""
    out: list[DayResult] = []
    before, rebuilt = start_equity, start_equity
    for day in days:
        r = day.equity / before - 1
        before = day.equity
        rebuilt = rebuilt * (1 + r * scale)
        out.append(replace(day, equity=rebuilt, gross=day.gross * scale))
    return out
