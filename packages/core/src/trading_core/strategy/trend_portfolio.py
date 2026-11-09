"""``trend_portfolio``'s pure rule (spec 0030 AC-1, AC-2): a 30 day trend portfolio across many coins.

Each coin is long when its close is above the close 30 days before and short when below, sized by
``vol_target / vol`` (a calmer coin gets a bigger share), divided by the number of eligible coins, and the
whole book is scaled down so its gross weight never passes ``gross_cap``. The definitions match the
backtests ``slow_ideas.portfolio()`` and ``old_data.portfolio()`` (spec 0030 rationale). No I/O, no
Nautilus import, and no float: every value is ``Decimal``, square roots at 28 digits.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Final

SQRT_PRECISION: Final = 28
DAY_NS: Final = 86_400 * 1_000_000_000
EPOCH: Final = date(1970, 1, 1)


class PortfolioError(ValueError):
    """Base for every error the portfolio rule raises."""


class EquityNotPositiveError(PortfolioError):
    """The plan needs equity above zero."""


class BadCloseError(PortfolioError):
    """A coin's closes are not one positive close per day, oldest first."""


@dataclass(frozen=True)
class DailyClose:
    """One UTC day's closing price."""

    day: date
    close: Decimal


@dataclass(frozen=True)
class PortfolioParams:
    """The rule's settings (AC-1): lookback and volatility windows, the volatility target, the gross cap, and
    the history a coin needs to be eligible."""

    lookback_days: int = 30
    vol_days: int = 30
    vol_target: Decimal = Decimal("0.02")
    gross_cap: Decimal = Decimal(2)
    min_history_days: int = 120

    def __post_init__(self) -> None:
        """Refuse a window below 2, a target or cap at or below 0, or too short a history for the windows."""
        if self.lookback_days < 2:
            raise ValueError(f"lookback_days must be at least 2, got {self.lookback_days}")
        if self.vol_days < 2:
            raise ValueError(f"vol_days must be at least 2, got {self.vol_days}")
        if self.vol_target <= 0:
            raise ValueError(f"vol_target must be above 0, got {self.vol_target}")
        if self.gross_cap <= 0:
            raise ValueError(f"gross_cap must be above 0, got {self.gross_cap}")
        if self.min_history_days < max(self.lookback_days, self.vol_days) + 1:
            raise ValueError(
                f"min_history_days must be at least {max(self.lookback_days, self.vol_days) + 1}, "
                f"got {self.min_history_days}"
            )


@dataclass(frozen=True)
class CoinPlan:
    """One coin's part of the plan; a coin that is not eligible has signal 0, no vol, and weight and target 0."""

    eligible: bool
    signal: int
    vol: Decimal | None
    weight: Decimal
    target_notional: Decimal
    close_date: date | None


@dataclass(frozen=True)
class PortfolioPlan:
    """The plan for every coin plus the book figures: N, the gross weight before scaling, the scale, equity,
    and the newest close date used."""

    coins: Mapping[str, CoinPlan]
    n: int
    gross: Decimal
    gross_scale: Decimal
    equity: Decimal
    close_date: date | None


def close_day(close_ns: int) -> date:
    """The UTC day a daily bar or candle closing at ``close_ns`` (00:00 of the next day) belongs to."""
    return EPOCH + timedelta(days=close_ns // DAY_NS - 1)


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _check(coin: str, closes: Sequence[DailyClose]) -> None:
    last: date | None = None
    for item in closes:
        if item.close <= 0:
            raise BadCloseError(f"{coin}: close {item.close} on {item.day} is not above zero")
        if last is not None and item.day <= last:
            raise BadCloseError(f"{coin}: {item.day} does not come after {last}")
        last = item.day


def sample_vol(closes: Sequence[Decimal], days: int) -> Decimal:
    """The sample standard deviation (divisor n - 1) of the last ``days`` simple daily returns.

    Needs ``days + 1`` closes; raises ``ValueError`` with fewer.
    """
    if len(closes) < days + 1:
        raise ValueError(f"{days} returns need {days + 1} closes, got {len(closes)}")
    window = closes[-(days + 1) :]
    returns = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (len(returns) - 1)
    with localcontext() as ctx:
        ctx.prec = SQRT_PRECISION
        return variance.sqrt()


def portfolio_plan(
    closes: Mapping[str, Sequence[DailyClose]], equity: Decimal, params: PortfolioParams
) -> PortfolioPlan:
    """The plan for every coin in ``closes`` (AC-1).

    A coin is eligible with at least ``min_history_days`` closes. Its signal is the sign of
    ``close[t] / close[t - lookback] - 1``; its vol the sample standard deviation of its last ``vol_days``
    returns; its raw weight ``signal x vol_target / vol`` (0 when vol is 0, still eligible); its weight the
    raw weight over N, the eligible count. When the gross weight passes ``gross_cap`` every weight is scaled
    by ``gross_cap / gross``. The target is ``weight x equity`` (signed USD). Raises
    ``EquityNotPositiveError`` for equity at or below 0 and ``BadCloseError`` for a non positive close or
    days out of order.
    """
    if equity <= 0:
        raise EquityNotPositiveError(f"equity must be above 0, got {equity}")
    raw: dict[str, tuple[int, Decimal, Decimal]] = {}
    for coin, series in closes.items():
        _check(coin, series)
        if len(series) < params.min_history_days:
            continue
        values = [item.close for item in series]
        signal = _sign(values[-1] / values[-1 - params.lookback_days] - 1)
        vol = sample_vol(values, params.vol_days)
        raw_weight = Decimal(0) if vol == 0 else signal * params.vol_target / vol
        raw[coin] = (signal, vol, raw_weight)
    n = len(raw)
    weights = {coin: (w / n) for coin, (_, _, w) in raw.items()} if n else {}
    gross: Decimal = sum((abs(w) for w in weights.values()), Decimal(0))
    scale = params.gross_cap / gross if gross > params.gross_cap else Decimal(1)
    plans: dict[str, CoinPlan] = {}
    for coin, series in closes.items():
        last_day = series[-1].day if series else None
        if coin not in raw:
            plans[coin] = CoinPlan(False, 0, None, Decimal(0), Decimal(0), last_day)
            continue
        coin_signal, coin_vol, _ = raw[coin]
        weight = weights[coin] * scale
        plans[coin] = CoinPlan(True, coin_signal, coin_vol, weight, weight * equity, last_day)
    dates = [p.close_date for p in plans.values() if p.close_date is not None]
    return PortfolioPlan(plans, n, gross, scale, equity, max(dates, default=None))


def order_delta(
    target_notional: Decimal,
    mid: Decimal,
    current_qty: Decimal,
    size_increment: Decimal,
    min_quantity: Decimal | None,
) -> Decimal | None:
    """The signed quantity to trade to reach ``target_notional`` at ``mid`` (AC-2), or None to skip.

    The target quantity is ``target_notional / mid`` rounded toward zero to ``size_increment``; the delta
    is that minus ``current_qty`` (signed). A delta smaller than ``max(size_increment, min_quantity)`` is
    None. Raises ``ValueError`` when ``mid`` or ``size_increment`` is at or below 0.
    """
    delta = target_quantity(target_notional, mid, size_increment) - current_qty
    minimum = max(size_increment, min_quantity or Decimal(0))
    if abs(delta) < minimum:
        return None
    return delta


def target_quantity(target_notional: Decimal, mid: Decimal, size_increment: Decimal) -> Decimal:
    """``target_notional / mid`` rounded toward zero to ``size_increment`` (the target quantity of AC-2)."""
    if mid <= 0 or size_increment <= 0:
        raise ValueError(f"mid and size increment must be above 0, got {mid}, {size_increment}")
    steps = (target_notional / mid / size_increment).to_integral_value(rounding=ROUND_DOWN)
    return (steps * size_increment).quantize(size_increment)
