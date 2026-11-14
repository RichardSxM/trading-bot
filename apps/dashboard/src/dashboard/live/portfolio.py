"""What the Portfolio page shows (spec 0030 AC-19 to AC-21), as pure functions of the book trader's snapshots.

With ``DASHBOARD_PORTFOLIO_SLOT=portfolio`` (the default) the page reads ``{env}:snapshot:portfolio_heartbeat``,
``portfolio_trader_state``, ``portfolio_plan``, ``portfolio_positions``, and ``portfolio_account``, plus the
newest ``slot=portfolio`` result on ``{env}:control:results``. With ``main`` (spec 0032 AC-8) it reads main's
``heartbeat``, ``trader_state``, ``positions``, and ``account``, the same ``portfolio_plan``, and main's newest
result (one with no ``slot`` field). A plan with ``schedule`` ``daily`` (``trend_daily``) rebalances every day
at ``run_at_utc``; without the field it is weekly on ``rebalance_weekday``. A missing heartbeat, or one past
the red limit, makes the whole page ``not_running``, with the time it was last seen when there is one: stale
figures never look live. Rows follow the plan's coin order, which is the trader's ``PAPER_INSTRUMENTS`` order;
positions are joined on ``instrument_id``. Money arrives as strings and becomes ``Decimal`` here.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from uuid import UUID

from dashboard.live.position import CoinRow, coin_row
from dashboard.live.state import (
    AgeView,
    CommandLevel,
    Entry,
    age_state,
    ago,
    outcome_words,
    pause_view,
)
from trading_core.control.messages import ControlFormatError, field, result_from_fields
from trading_core.control.slots import TraderSlot, result_slot
from trading_core.snapshot.envelope import Snapshot, SnapshotFormatError, decode

PageStatus = Literal["running", "not_running", "bad", "lost"]
NS_PER_S: Final = 1_000_000_000
CENT: Final = Decimal("0.01")
SIGNAL_WORDS: Final = {1: "Long", -1: "Short", 0: "Flat"}


@dataclass(frozen=True)
class PortfolioRaw:
    """The raw Redis values the page is built from; None where a key is missing."""

    heartbeat: str | bytes | None
    trader_state: str | bytes | None
    plan: str | bytes | None
    positions: str | bytes | None
    account: str | bytes | None
    results: Sequence[Entry] | None
    commands: Sequence[Entry] | None


@dataclass(frozen=True)
class PlanRow:
    """One coin on the page (AC-20): the plan's signal, weight, and target, the position, and ``failed``.

    ``signal`` is plain words (``Long``, ``Short``, ``Flat``, ``Not eligible``, or ``No plan yet``), never a
    colour alone. ``position`` is the coin's ``positions`` entry, None when the trader wrote none for it.
    """

    instrument_id: str
    base: str
    signal: str
    weight_pct: Decimal | None
    target_usd: Decimal | None
    target_qty: Decimal | None
    failed: bool
    position: CoinRow | None


@dataclass(frozen=True)
class LastCommand:
    """The newest ``slot=portfolio`` result: the command's name when it is still in view, words, level, age."""

    cmd: str | None
    text: str
    level: CommandLevel
    at: datetime
    age: str


@dataclass(frozen=True)
class PortfolioView:
    """The page: its status, the account summary (AC-21), and one row per coin (AC-20).

    ``realized`` is None when Postgres did not answer or no run is known yet. ``last_seen`` is the stale
    heartbeat's time while ``not_running``, None when the trader was never seen.
    """

    status: PageStatus
    age: AgeView | None = None
    last_seen: datetime | None = None
    rows: tuple[PlanRow, ...] = ()
    has_plan: bool = False
    equity: Decimal | None = None
    gross_leverage: Decimal | None = None
    realized: Decimal | None = None
    last_rebalance: datetime | None = None
    last_reason: str | None = None
    next_rebalance: datetime | None = None
    schedule: str = "weekly"
    failed: tuple[str, ...] = ()
    state_text: str = ""
    state_level: str = "ok"
    last_command: LastCommand | None = None


LOST: Final = PortfolioView("lost")


def run_id(raw_state: str | bytes | None) -> UUID | None:
    """The paper run ``portfolio_trader_state`` names, None when missing or unreadable."""
    if raw_state is None:
        return None
    try:
        value = decode(raw_state).data.get("run_id")
    except SnapshotFormatError:
        return None
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _decode(raw: str | bytes | None) -> Snapshot | None:
    if raw is None:
        return None
    try:
        return decode(raw)
    except SnapshotFormatError:
        return None


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        return None
    return decimal if decimal.is_finite() else None


def _cents(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT)


def _ns_time(value: object) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value // NS_PER_S, UTC)


def base_of(instrument_id: str) -> str:
    """The coin's short name from its id: ``PF_ETHUSD.KRAKEN`` → ``ETH``; the id itself when it does not fit."""
    symbol = instrument_id.split(".", 1)[0].removeprefix("PF_")
    return symbol.removesuffix("USD") or instrument_id


def signal_words(eligible: object, signal: object) -> str:
    """The 30 day signal as text (AC-20): ``Not eligible`` before ``Long``, ``Short``, ``Flat``."""
    if eligible is False:
        return "Not eligible"
    if eligible is True and isinstance(signal, int) and not isinstance(signal, bool) and signal in SIGNAL_WORDS:
        return SIGNAL_WORDS[signal]
    return "No plan yet"


DAILY: Final = "daily"


def next_rebalance(run_at_utc: object, weekday: object, now: datetime, schedule: object = None) -> datetime | None:
    """The next rebalance strictly after ``now``: with ``schedule`` ``daily`` the next ``run_at_utc`` (``HH:MM``,
    UTC) on any day (spec 0032 AC-2a), else the next ``weekday`` (Monday 0) at that time; None if unreadable."""
    if not isinstance(run_at_utc, str):
        return None
    try:
        hour, minute = (int(part) for part in run_at_utc.split(":"))
        moment = now.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if schedule == DAILY:
        return moment if moment > now else moment + timedelta(days=1)
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        return None
    moment += timedelta(days=(weekday - moment.weekday()) % 7)
    return moment if moment > now else moment + timedelta(days=7)


def _rows(plan: Mapping[str, object] | None, positions: Snapshot | None) -> tuple[PlanRow, ...]:
    by_coin: dict[str, CoinRow] = {}
    entries = None if positions is None else positions.data.get("coins")
    for entry in entries if isinstance(entries, list) else ():
        row = coin_row(entry)
        if row.instrument_id is not None:
            by_coin[row.instrument_id] = row
    coins = None if plan is None else plan.get("coins")
    if not isinstance(coins, list):  # no plan yet: the positions alone, so a held coin still shows
        return tuple(
            PlanRow(key, row.base or base_of(key), "No plan yet", None, None, None, False, row)
            for key, row in by_coin.items()
        )
    rows: list[PlanRow] = []
    for item in coins:
        if not isinstance(item, Mapping) or not isinstance(item.get("instrument_id"), str):
            continue
        key = item["instrument_id"]
        weight = _decimal(item.get("weight"))
        position = by_coin.get(key)
        rows.append(
            PlanRow(
                key,
                (position.base if position is not None and position.base else None) or base_of(key),
                signal_words(item.get("eligible"), item.get("signal")),
                None if weight is None else (weight * 100).quantize(CENT),
                _cents(_decimal(item.get("target_notional"))),
                _decimal(item.get("target_qty")),
                item.get("failed") is True,
                position,
            )
        )
    return tuple(rows)


def _gross_leverage(rows: Sequence[PlanRow], equity: Decimal | None) -> Decimal | None:
    """The sum of |quantity x mark| over open positions, over equity; None when equity is not positive."""
    if equity is None or equity <= 0:
        return None
    notional = Decimal(0)
    for row in rows:
        position = row.position
        if position is None or position.status != "open":
            continue
        if position.quantity is None or position.mark is None:
            return None
        notional += abs(position.quantity * position.mark)
    return (notional / equity).quantize(CENT)


def last_command(
    results: Sequence[Entry] | None,
    commands: Sequence[Entry] | None,
    now: datetime,
    slot: TraderSlot = TraderSlot.PORTFOLIO,
) -> LastCommand | None:
    """The newest result of ``slot`` in a newest first ``XREVRANGE`` reply, named from ``commands``."""
    for _, fields in results or ():
        if fields is None or result_slot(fields) is not slot:
            continue
        try:
            result = result_from_fields(fields)
        except ControlFormatError:
            continue
        cmd = None
        for _, command in commands or ():
            if command is not None and field(command, "id") == str(result.id):
                cmd = field(command, "cmd")
                break
        text, level = outcome_words(result)
        return LastCommand(cmd, text, level, result.outcome_at, ago((now - result.outcome_at).total_seconds()))
    return None


def portfolio_view(
    raw: PortfolioRaw,
    realized: Decimal | None,
    now_ns: int,
    amber_s: float,
    red_s: float,
    slot: TraderSlot = TraderSlot.PORTFOLIO,
) -> PortfolioView:
    """The page for the raw snapshots of ``slot``'s trader and the run's realized P&L (None when unknown)."""
    now = datetime.fromtimestamp(now_ns / NS_PER_S, UTC)
    command = last_command(raw.results, raw.commands, now, slot)
    if raw.heartbeat is None:
        return PortfolioView("not_running", last_command=command)
    heartbeat = _decode(raw.heartbeat)
    if heartbeat is None:
        return PortfolioView("bad", last_command=command)
    age = age_state(now_ns, heartbeat.ts_ns, amber_s, red_s)
    if age.level == "bad":
        return PortfolioView("not_running", age=age, last_seen=_ns_time(heartbeat.ts_ns), last_command=command)
    plan_snapshot = _decode(raw.plan)
    plan = None if plan_snapshot is None else plan_snapshot.data
    account = _decode(raw.account)
    equity = None if account is None else _decimal(account.data.get("equity"))
    rows = _rows(plan, _decode(raw.positions))
    failed = plan.get("failed") if plan is not None else None
    pause = pause_view(raw.trader_state, now)
    schedule = None if plan is None else plan.get("schedule")
    return PortfolioView(
        "running",
        age=age,
        rows=rows,
        has_plan=plan is not None and isinstance(plan.get("coins"), list),
        equity=equity,
        gross_leverage=_gross_leverage(rows, equity),
        realized=realized,
        last_rebalance=None if plan is None else _ns_time(plan.get("last_rebalance_at")),
        last_reason=None if plan is None or not isinstance(plan.get("last_reason"), str) else str(plan["last_reason"]),
        next_rebalance=None
        if plan is None
        else next_rebalance(plan.get("run_at_utc"), plan.get("rebalance_weekday"), now, schedule),
        schedule=DAILY if schedule == DAILY else "weekly",
        failed=tuple(base_of(coin) for coin in failed if isinstance(coin, str)) if isinstance(failed, list) else (),
        state_text=pause.text or "running",
        state_level=pause.level,
        last_command=command,
    )
