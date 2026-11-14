"""``trend_vol``'s pure rule: the trend portfolio sized by the risk of the whole book, with a no trade band.

``trend_portfolio`` sizes each coin by its own volatility and then scales to a fixed gross cap, so twelve
coins that move together (all short in a sell off) are one large bet. This rule keeps the same signals and
the same split between coins but sets the size of the book from the book's own volatility: the plan's
weights, at a gross of 1, are applied to each coin's last ``vol_days`` daily returns, and the book is scaled
so the standard deviation of that return series, annualised over 365 days, equals ``book_vol_target`` (never
past ``gross_cap``). ``within_band`` then lets a rebalance leave alone a position that is already close to
its target, which cuts the fees a daily rebalance pays. No I/O, no Nautilus import, and no float.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

from trading_core.strategy.trend_portfolio import SQRT_PRECISION, CoinPlan, DailyClose, PortfolioPlan

DAYS_PER_YEAR: Final = 365


@dataclass(frozen=True)
class BookVolParams:
    """The book level settings: the annual volatility target (0.5 is 50 %), the window of daily returns the
    book's volatility is measured on, the most gross the book may hold, and the no trade band (0 is off)."""

    book_vol_target: Decimal = Decimal("0.5")
    vol_days: int = 30
    gross_cap: Decimal = Decimal(5)
    band: Decimal = Decimal("0.3")

    def __post_init__(self) -> None:
        """Refuse a target or cap at or below 0, a window below 2, or a band outside 0 up to (not at) 1."""
        if self.book_vol_target <= 0:
            raise ValueError(f"book_vol_target must be above 0, got {self.book_vol_target}")
        if self.vol_days < 2:
            raise ValueError(f"vol_days must be at least 2, got {self.vol_days}")
        if self.gross_cap <= 0:
            raise ValueError(f"gross_cap must be above 0, got {self.gross_cap}")
        if not 0 <= self.band < 1:
            raise ValueError(f"band must be at least 0 and below 1, got {self.band}")


@dataclass(frozen=True)
class BookVolPlan:
    """A plan sized by book volatility: the plan, the book's daily volatility at a gross of 1 (None when the
    plan holds no weight), and the gross the book was scaled to."""

    plan: PortfolioPlan
    unit_vol: Decimal | None
    gross: Decimal


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = SQRT_PRECISION
        return value.sqrt()


def unit_book_vol(
    closes: Mapping[str, Sequence[DailyClose]], unit_weights: Mapping[str, Decimal], vol_days: int
) -> Decimal:
    """The sample standard deviation (divisor n - 1) of the book's last ``vol_days`` daily returns, where the
    book holds ``unit_weights`` (signed, fractions of equity) the whole time.

    Each coin gives its own last ``vol_days`` simple returns, newest aligned with newest. Raises
    ``ValueError`` when a weighted coin has fewer than ``vol_days + 1`` closes.
    """
    book = [Decimal(0)] * vol_days
    for coin, weight in unit_weights.items():
        if weight == 0:
            continue
        series = closes.get(coin, ())
        if len(series) < vol_days + 1:
            raise ValueError(f"{coin}: {vol_days} returns need {vol_days + 1} closes, got {len(series)}")
        window = [item.close for item in series[-(vol_days + 1) :]]
        for i in range(vol_days):
            book[i] += weight * (window[i + 1] / window[i] - 1)
    mean = sum(book, Decimal(0)) / vol_days
    return _sqrt(sum(((r - mean) ** 2 for r in book), Decimal(0)) / (vol_days - 1))


def book_vol_plan(
    closes: Mapping[str, Sequence[DailyClose]], plan: PortfolioPlan, params: BookVolParams
) -> BookVolPlan:
    """``plan`` with the same signals and the same split between coins, scaled to the book volatility target.

    The plan's weights are divided by their gross (a book of gross 1), ``unit_book_vol`` measures that book,
    and the gross becomes ``book_vol_target / sqrt(365) / unit vol``, at most ``gross_cap`` (``gross_cap``
    when the unit vol is 0). Each coin's weight is its unit weight times that gross and its target the weight
    times the plan's equity; ``gross_scale`` is updated so a weight is still the raw weight over N times
    ``gross_scale``. A plan with no weight comes back unchanged.
    """
    gross_now: Decimal = sum((abs(item.weight) for item in plan.coins.values()), Decimal(0))
    if gross_now == 0:
        return BookVolPlan(plan, None, Decimal(0))
    unit = {coin: item.weight / gross_now for coin, item in plan.coins.items()}
    unit_vol = unit_book_vol(closes, unit, params.vol_days)
    daily_target = params.book_vol_target / _sqrt(Decimal(DAYS_PER_YEAR))
    gross = params.gross_cap if unit_vol == 0 else min(params.gross_cap, daily_target / unit_vol)
    coins = {
        coin: CoinPlan(
            item.eligible,
            item.signal,
            item.vol,
            unit[coin] * gross,
            unit[coin] * gross * plan.equity,
            item.close_date,
        )
        for coin, item in plan.coins.items()
    }
    scaled = PortfolioPlan(
        coins, plan.n, plan.gross, plan.gross_scale * gross / gross_now, plan.equity, plan.close_date
    )
    return BookVolPlan(scaled, unit_vol, gross)


def within_band(target_notional: Decimal, current_notional: Decimal, band: Decimal) -> bool:
    """True when a position may be left alone: it is open, on the target's side, and no further from the
    target than ``band`` times the target (both signed USD). Always False with a band of 0, a flat position,
    a target of 0, or a position on the other side."""
    if band <= 0 or target_notional == 0 or current_notional == 0:
        return False
    if (target_notional > 0) != (current_notional > 0):
        return False
    return abs(target_notional - current_notional) <= band * abs(target_notional)
