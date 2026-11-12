"""Overview's standings and scoreboard, as pure functions of the book view, daily closes, and the season curve.

The book trader (``trend_vol``) signs ``close[t] / close[t - 30] - 1`` per coin at ``run_at_utc``, over UTC daily
closes (``trading_core.strategy.trend_portfolio``). At tonight's run ``t`` is today, so the close 30 days before
today is each coin's flip price: above it the coin plans long, below it short. The live 30 day trend is the mark
over that price; the move to flip is that price over the mark. One daily move is the sample standard deviation of
the last ``VOL_DAYS`` daily returns, and a coin less than one daily move from its flip price is ``near``. A held
side the live trend disagrees with ``flips`` at the next run if the trend holds until then.

Rows rank by the live trend, strongest long first and strongest short last; the flip line sits under the last
row with a positive trend. Form is the last ``FORM_DAYS`` complete days: won when that day's return had the sign
of the side held now. Closes come from Kraken's public daily trade candles (``dashboard.data.kraken_daily``), so
they can differ from the trader's own bars in the last digits; the page says where they come from.
"""

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from dashboard.live.portfolio import PageStatus, PlanRow, PortfolioView

LOOKBACK_DAYS: Final = 30
VOL_DAYS: Final = 30
FORM_DAYS: Final = 7
PCT: Final = Decimal("0.01")
Side = Literal["long", "short", "flat", "unknown"]


@dataclass(frozen=True)
class FormDay:
    """One complete UTC day of the form guide: won, lost, or None when the day or the side is unknown."""

    day: date
    won: bool | None


@dataclass(frozen=True)
class Standing:
    """One coin's row.

    ``side`` is the position held (``long``, ``short``, ``flat``, or ``unknown`` when the trader wrote none).
    ``trend_pct`` is the live 30 day trend in percent, ``flip_move_pct`` the move in percent the mark needs to
    reach ``flip_px``, and ``moves`` that move in daily moves; each None when the closes do not reach far enough.
    """

    instrument_id: str
    base: str
    side: Side
    quantity: Decimal | None
    weight_pct: Decimal | None
    unrealized: Decimal | None
    mark: Decimal | None
    flip_px: Decimal | None
    trend_pct: Decimal | None
    flip_move_pct: Decimal | None
    moves: Decimal | None
    near: bool
    flips: bool
    failed: bool
    form: tuple[FormDay, ...]


@dataclass(frozen=True)
class StandingsView:
    """The table: the book view's status, rows ranked by trend, how many sit above the flip line, the next run,
    and whether daily closes were available (``closes_day`` is the newest complete close day used)."""

    status: PageStatus
    rows: tuple[Standing, ...] = ()
    above: int = 0
    next_run: datetime | None = None
    closes_day: date | None = None
    has_closes: bool = False


@dataclass(frozen=True)
class SeasonPoint:
    """The book's equity at the end of one hour (UTC)."""

    at: datetime
    equity: Decimal


@dataclass(frozen=True)
class Season:
    """The equity curve over every run of the current strategy on the book trader, hourly, oldest first."""

    strategy: str | None
    points: tuple[SeasonPoint, ...]


@dataclass(frozen=True)
class ScoreView:
    """The scoreboard: book value, today's and the season's change, gross against its cap, and the next run.

    ``today`` compares with the last point before 00:00 UTC; ``season_change`` with the season's first point. Each
    is None when the season curve is unknown or too short. ``held`` counts open positions by side.
    """

    status: PageStatus
    equity: Decimal | None = None
    today: Decimal | None = None
    season_change: Decimal | None = None
    season_start: datetime | None = None
    strategy: str | None = None
    gross: Decimal | None = None
    gross_cap: Decimal = Decimal(5)
    next_run: datetime | None = None
    until: str | None = None
    longs: int = 0
    shorts: int = 0
    near: int = 0
    flips: int = 0
    failed: tuple[str, ...] = ()
    state_text: str = ""
    state_level: str = "ok"


def _side(row: PlanRow) -> tuple[Side, Decimal | None]:
    position = row.position
    if position is None or position.status in ("unknown", "bad"):
        return "unknown", None
    if position.status == "flat":
        return "flat", None
    side = position.side
    quantity = None if position.quantity is None else abs(position.quantity)
    return ("long" if side == "long" else "short" if side == "short" else "unknown"), quantity


def daily_moves(closes: Mapping[date, Decimal], today: date, days: int = VOL_DAYS) -> Decimal | None:
    """The sample standard deviation of the last ``days`` daily returns before ``today``; None when too short."""
    values = [
        closes[today - timedelta(days=back)]
        for back in range(days + 1, 0, -1)
        if today - timedelta(days=back) in closes
    ]
    if len(values) < days + 1:
        return None
    returns = [b / a - 1 for a, b in itertools.pairwise(values) if a > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (len(returns) - 1)
    return variance.sqrt()


def form_guide(closes: Mapping[date, Decimal], today: date, side: Side, days: int = FORM_DAYS) -> tuple[FormDay, ...]:
    """The last ``days`` complete days, oldest first, each won when its return had the sign of ``side``."""
    sign = 1 if side == "long" else -1 if side == "short" else 0
    out: list[FormDay] = []
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        before, close = closes.get(day - timedelta(days=1)), closes.get(day)
        if sign == 0 or before is None or close is None or before <= 0 or close == before:
            out.append(FormDay(day, None))
        else:
            out.append(FormDay(day, (close > before) == (sign > 0)))
    return tuple(out)


def standing(row: PlanRow, closes: Mapping[date, Decimal] | None, today: date) -> Standing:
    """One coin's row for ``today`` (UTC) from its plan row and its daily closes (None when unknown)."""
    side, quantity = _side(row)
    position = row.position
    mark = None if position is None else position.mark
    known = closes or {}
    if mark is None:
        mark = known.get(today)
    flip_px = known.get(today - timedelta(days=LOOKBACK_DAYS))
    trend_pct = flip_move_pct = moves = None
    near = flips = False
    if flip_px is not None and flip_px > 0 and mark is not None and mark > 0:
        trend = mark / flip_px - 1
        move = flip_px / mark - 1
        trend_pct, flip_move_pct = (trend * 100).quantize(PCT), (move * 100).quantize(PCT)
        sigma = daily_moves(known, today)
        if sigma is not None and sigma > 0:
            moves = (abs(move) / sigma).quantize(PCT)
            near = moves < 1
        flips = (side == "long" and trend < 0) or (side == "short" and trend > 0)
    return Standing(
        row.instrument_id,
        row.base,
        side,
        quantity,
        row.weight_pct,
        None if position is None else position.unrealized,
        mark,
        flip_px,
        trend_pct,
        flip_move_pct,
        moves,
        near,
        flips,
        row.failed,
        form_guide(known, today, side),
    )


def _rank(item: Standing) -> tuple[int, Decimal]:
    return (0, -item.trend_pct) if item.trend_pct is not None else (1, Decimal(0))


def standings_view(
    book: PortfolioView, closes: Mapping[str, Mapping[date, Decimal]] | None, today: date
) -> StandingsView:
    """The table for ``today`` (UTC) from the book view and each coin's daily closes (None: unavailable)."""
    if book.status != "running":
        return StandingsView(book.status)
    rows = tuple(
        sorted(
            (standing(r, None if closes is None else closes.get(r.instrument_id), today) for r in book.rows), key=_rank
        )
    )
    days = [max(d for d in c if d < today) for c in (closes or {}).values() if any(d < today for d in c)]
    return StandingsView(
        "running",
        rows=rows,
        above=sum(1 for r in rows if r.trend_pct is not None and r.trend_pct > 0),
        next_run=book.next_rebalance,
        closes_day=max(days) if days else None,
        has_closes=any(r.trend_pct is not None for r in rows),
    )


def _before(points: Sequence[SeasonPoint], moment: datetime) -> SeasonPoint | None:
    found = None
    for point in points:
        if point.at >= moment:
            break
        found = point
    return found


def score_view(
    book: PortfolioView, table: StandingsView, season: Season | None, now: datetime, gross_cap: Decimal
) -> ScoreView:
    """The scoreboard at ``now`` from the book view, its standings, and the season curve (None: unknown)."""
    if book.status != "running":
        return ScoreView(book.status, gross_cap=gross_cap)
    today = start = None
    first = None
    if season is not None and season.points and book.equity is not None:
        first = season.points[0]
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        opening = _before(season.points, midnight) or first
        today = book.equity - opening.equity
        start = book.equity - first.equity
    return ScoreView(
        "running",
        equity=book.equity,
        today=today,
        season_change=start,
        season_start=None if first is None else first.at,
        strategy=None if season is None else season.strategy,
        gross=book.gross_leverage,
        gross_cap=gross_cap,
        next_run=book.next_rebalance,
        until=None if book.next_rebalance is None else until_words(book.next_rebalance - now),
        longs=sum(1 for r in table.rows if r.side == "long"),
        shorts=sum(1 for r in table.rows if r.side == "short"),
        near=sum(1 for r in table.rows if r.near),
        flips=sum(1 for r in table.rows if r.flips),
        failed=book.failed,
        state_text=book.state_text,
        state_level=book.state_level,
    )


def until_words(left: timedelta) -> str:
    """Time left as ``7 h 31 min``, ``31 min``, or ``now`` under a minute."""
    minutes = int(left.total_seconds()) // 60
    if minutes < 1:
        return "now"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def gross_share(score: ScoreView) -> Decimal:
    """Gross over its cap as a fraction from 0 to 1, for the fixed length bar (0 when unknown)."""
    if score.gross is None or score.gross_cap <= 0:
        return Decimal(0)
    return min(max(score.gross / score.gross_cap, Decimal(0)), Decimal(1))
