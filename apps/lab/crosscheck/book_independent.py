"""An independent Decimal copy of the ``trend_vol`` book (spec 0033 AC-10a), to catch bugs in the pure replay.

It imports nothing from ``trading_core`` (nor from ``lab``): every rule is written again here from the spec
text (spec 0030 AC-1, AC-2; spec 0032; spec 0033 AC-1, AC-9), so a mistake shared by the trader's pure
functions and the replay that calls them shows up as a parity fail. Only the standard library is used.

The book, day by day:

1. Each coin's daily close for day ``D`` is its hourly close ending at ``D + 1`` 00:00 UTC (cut hour 0). The
   newest ``keep_days`` (150) closes up to the plan day are kept.
2. On day ``T`` the plan uses closes up to ``T - 1``. A coin with at least ``min_history_days`` (120) closes is
   eligible. Its signal is the sign of its newest close over the close ``lookback_days`` (30) before, minus 1.
   Its vol is the sample standard deviation (n - 1) of its last ``vol_days`` (30) simple returns. Its raw
   weight is ``signal x vol_target / vol`` (0 when vol is 0), divided by the eligible count; when the sum of
   absolute weights passes ``gross_cap`` (5) they are scaled down to it.
3. Book sizing: the weights divided by their absolute sum give a book of gross 1; that book's daily return on
   each of the last ``vol_days`` days is the weighted sum of the coins' returns on that day (newest with
   newest); its sample standard deviation is the unit vol. The book gross is ``book_vol_target / sqrt(365) /
   unit vol``, at most ``gross_cap`` (``gross_cap`` when the unit vol is 0). Target = unit weight x gross x
   equity, where equity is cash plus the open positions marked at the fill prices.
4. Trading at the one minute close ending ``T`` 00:05: an eligible coin whose newest close is not ``T - 1``
   keeps its position; a coin with no such minute close is not traded; a position on the target's side no
   further from the target than ``band`` times it is left alone (never with band 0, a flat position, or a
   target of 0); otherwise the target quantity (target over price, rounded toward zero to the size step) less
   the position is traded, unless smaller than ``max(size step, min quantity)``. Each trade pays ``cost`` on
   its notional.
5. Funding at every whole hour strictly between two fills (every fourth hour before 2022-09-29 12:00 UTC):
   a held coin books ``-qty x hourly close x rate``; an hour with no rate is skipped.

A plan that cannot be made (no eligible coin, or a weighted coin too short for the book vol) leaves the old plan
in place and the day does not trade.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise

NS = 1_000_000_000
HOUR = 3600 * NS
DAY = 24 * HOUR
FILL_AFTER = 5 * 60 * NS
HOURLY_FUNDING_FROM = int(datetime(2022, 9, 29, 12, tzinfo=UTC).timestamp()) * NS


@dataclass(frozen=True)
class Coin:
    """One coin's prices: hourly closes by the hour's end, minute closes by the minute's end, funding rates by
    charge time, and its size step and minimum quantity."""

    hourly: Mapping[int, Decimal]
    minutes: Mapping[int, Decimal]
    funding: Mapping[int, Decimal]
    step: Decimal
    min_qty: Decimal | None


@dataclass(frozen=True)
class Rules:
    """The book's settings (spec 0033 AC-3)."""

    lookback_days: int = 30
    vol_days: int = 30
    vol_target: Decimal = Decimal("0.155")
    gross_cap: Decimal = Decimal(5)
    min_history_days: int = 120
    book_vol_target: Decimal = Decimal("0.5")
    band: Decimal = Decimal("0.3")
    keep_days: int = 150
    cost: Decimal = Decimal("0.0007")


@dataclass(frozen=True)
class Day:
    """One traded day: equity after its trades and each coin's signed quantity then."""

    day: date
    equity: Decimal
    positions: Mapping[str, Decimal]


@dataclass
class _Holding:
    qty: Decimal = Decimal(0)
    entry: Decimal = Decimal(0)  # average entry price of the open quantity


@dataclass
class _Plan:
    targets: dict[str, Decimal] = field(default_factory=dict)
    eligible: set[str] = field(default_factory=set)
    newest: dict[str, date] = field(default_factory=dict)


def _midnight_ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * NS


def _daily_closes(coin: Coin) -> list[tuple[date, Decimal]]:
    """(day, close) for every hourly close at 00:00 UTC, the day being the one that just ended."""
    out = []
    for ns in sorted(coin.hourly):
        if ns % DAY == 0:
            out.append((datetime.fromtimestamp(ns // NS - 86_400, UTC).date(), coin.hourly[ns]))
    return out


def _stdev(xs: Sequence[Decimal]) -> Decimal:
    m = sum(xs, Decimal(0)) / len(xs)
    return (sum(((x - m) * (x - m) for x in xs), Decimal(0)) / (len(xs) - 1)).sqrt()


def _changes(prices: Sequence[Decimal]) -> list[Decimal]:
    return [b / a - 1 for a, b in pairwise(prices)]


def make_plan(history: Mapping[str, Sequence[tuple[date, Decimal]]], equity: Decimal, rules: Rules) -> _Plan | None:
    """The day's targets from each coin's closes up to the plan day, or None when no plan can be made."""
    raw: dict[str, Decimal] = {}
    returns: dict[str, list[Decimal]] = {}
    plan = _Plan()
    for name, rows in history.items():
        if rows:
            plan.newest[name] = rows[-1][0]
        if len(rows) < rules.min_history_days:
            continue
        prices = [p for _, p in rows]
        move = prices[-1] / prices[-1 - rules.lookback_days] - 1
        signal = (move > 0) - (move < 0)
        coin_returns = _changes(prices[-(rules.vol_days + 1) :])
        vol = _stdev(coin_returns)
        raw[name] = Decimal(0) if vol == 0 else signal * rules.vol_target / vol
        returns[name] = coin_returns
        plan.eligible.add(name)
    if not raw:
        return None
    count = len(raw)
    weights = {name: w / count for name, w in raw.items()}
    total = sum((abs(w) for w in weights.values()), Decimal(0))
    if total > rules.gross_cap:
        weights = {name: w * (rules.gross_cap / total) for name, w in weights.items()}
        total = sum((abs(w) for w in weights.values()), Decimal(0))
    if total == 0:
        plan.targets = {name: Decimal(0) for name in history}
        return plan
    unit = {name: w / total for name, w in weights.items()}
    book_returns = [Decimal(0)] * rules.vol_days
    for name, u in unit.items():
        if u == 0:
            continue
        for i, r in enumerate(returns[name]):
            book_returns[i] += u * r
    unit_vol = _stdev(book_returns)
    if unit_vol == 0:
        gross = rules.gross_cap
    else:
        gross = min(rules.gross_cap, rules.book_vol_target / Decimal(365).sqrt() / unit_vol)
    plan.targets = {name: unit.get(name, Decimal(0)) * gross * equity for name in history}
    return plan


def _rest(target: Decimal, current_notional: Decimal, band: Decimal) -> bool:
    """True when the band leaves the coin alone."""
    if band == 0 or target == 0 or current_notional == 0:
        return False
    if (target < 0) != (current_notional < 0):
        return False
    return abs(target - current_notional) <= band * abs(target)


def _fill(hold: _Holding, delta: Decimal, price: Decimal) -> Decimal:
    """Apply a signed trade; returns the realised P&L."""
    if hold.qty == 0 or (hold.qty > 0) == (delta > 0):
        new = hold.qty + delta
        hold.entry = (abs(hold.qty) * hold.entry + abs(delta) * price) / abs(new)
        hold.qty = new
        return Decimal(0)
    shut = min(abs(delta), abs(hold.qty))
    pnl = shut * (price - hold.entry) if hold.qty > 0 else shut * (hold.entry - price)
    new = hold.qty + delta
    if new == 0:
        hold.qty, hold.entry = Decimal(0), Decimal(0)
    elif (new > 0) != (hold.qty > 0):
        hold.qty, hold.entry = new, price
    else:
        hold.qty = new
    return pnl


def _value(cash: Decimal, holds: Mapping[str, _Holding], marks: Mapping[str, Decimal]) -> Decimal:
    """Cash plus every open holding marked at its latest price."""
    return cash + sum((h.qty * (marks[n] - h.entry) for n, h in holds.items() if h.qty != 0), Decimal(0))


def _charge_hours(after_ns: int, before_ns: int) -> list[int]:
    hours = []
    t = (after_ns // HOUR + 1) * HOUR
    while t < before_ns:
        if t >= HOURLY_FUNDING_FROM or t % (4 * HOUR) == 0:
            hours.append(t)
        t += HOUR
    return hours


def run_book(coins: Mapping[str, Coin], first: date, last: date, equity: Decimal, rules: Rules) -> list[Day]:
    """Run the book from flat over ``first`` to ``last`` (both traded, inclusive)."""
    closes = {name: _daily_closes(c) for name, c in coins.items()}
    holds = {name: _Holding() for name in coins}
    marks: dict[str, Decimal] = {}
    cash = equity
    plan: _Plan | None = None
    out: list[Day] = []
    previous_fill: int | None = None
    today = first
    while today <= last:
        fill_ns = _midnight_ns(today) + FILL_AFTER
        if previous_fill is not None:
            for hour in _charge_hours(previous_fill, fill_ns):
                for name, hold in holds.items():
                    price = coins[name].hourly.get(hour)
                    if price is not None:
                        marks[name] = price
                    rate = coins[name].funding.get(hour)
                    if hold.qty != 0 and rate is not None:
                        cash -= hold.qty * marks[name] * rate
        prices = {name: c.minutes.get(fill_ns) for name, c in coins.items()}
        for name, price in prices.items():
            if price is not None:
                marks[name] = price

        now_equity = _value(cash, holds, marks)
        if now_equity <= 0:
            break
        plan_day = today - timedelta(days=1)
        history = {
            name: [row for row in rows if row[0] <= plan_day][-rules.keep_days :] for name, rows in closes.items()
        }
        try:
            fresh = make_plan(history, now_equity, rules)
        except IndexError, ZeroDivisionError:
            fresh = None
        if fresh is not None:
            plan = fresh
            for name in sorted(coins):
                if name in plan.eligible and plan.newest.get(name) != plan_day:
                    continue
                price = prices[name]
                if price is None:
                    continue
                hold = holds[name]
                target = plan.targets.get(name, Decimal(0))
                if _rest(target, hold.qty * price, rules.band):
                    continue
                step = coins[name].step
                want = ((target / price / step).to_integral_value(rounding=ROUND_DOWN) * step).quantize(step)
                delta = want - hold.qty
                if abs(delta) < max(step, coins[name].min_qty or Decimal(0)):
                    continue
                cash += _fill(hold, delta, price) - abs(delta) * price * rules.cost
        out.append(Day(today, _value(cash, holds, marks), {n: holds[n].qty for n in sorted(holds)}))
        previous_fill = fill_ns
        today += timedelta(days=1)
    return out
