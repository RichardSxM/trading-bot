"""The ``positions`` and ``account`` snapshot data the trader publishes every second (spec 0006 AC-16).

Money and prices are strings of ``Decimal`` so the dashboard never parses a float. ``positions``
holds one entry per coin (spec 0025 AC-21); a coin without a quote yet is ``unknown`` and never
blanks the others. ``account`` returns None until the account exists, and the snapshot writer
skips that round.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final, Protocol

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.orders import Order
from nautilus_trader.portfolio.portfolio import Portfolio

from trading_core.node.control import PaperStatus
from trading_core.node.recording import position_facts
from trading_core.node.strategy import PauseGate
from trading_core.recording.mappers import weighted_avg
from trading_core.strategy.blocks import EntryBlocks, entries_blocked


class PaperStrategyView(Protocol):
    """What the snapshots read from the paper strategy: ``PlaceholderTimed`` itself, or a view of another one."""

    @property
    def exiting(self) -> bool:
        """True while the exit loop runs."""
        ...

    @property
    def position_opened_ns(self) -> int | None:
        """When the open position opened (UTC ns), None while flat."""
        ...

    @property
    def exit_due_ns(self) -> int | None:
        """When the timed exit is due (UTC ns), None when there is none."""
        ...

    def stop_order(self) -> Order | None:
        """The protective stop of the current round trip, if one was submitted."""
        ...


def _mid(cache: Cache, instrument_id: InstrumentId) -> Decimal | None:
    quote = cache.quote_tick(instrument_id)
    if quote is None:
        return None
    mid: Decimal = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
    return mid


CENT: Final = Decimal("0.01")


def _rounded(value: Decimal | None, tick: Decimal) -> Decimal | None:
    """An average price for display, at the smaller of a cent and the tick.

    The division leaves more digits than any fill had. BTC stays at cents; PEPE, priced far
    below a cent, keeps its digits (spec 0025 AC-21).
    """
    return None if value is None else value.quantize(min(CENT, tick))


def _base(cache: Cache, instrument_id: InstrumentId) -> str | None:
    instrument = cache.instrument(instrument_id)
    return None if instrument is None else instrument.base_currency.code


def _tick(cache: Cache, instrument_id: InstrumentId) -> Decimal:
    instrument = cache.instrument(instrument_id)
    return CENT if instrument is None else instrument.price_increment.as_decimal()


def _unrealized(cache: Cache, instrument_id: InstrumentId, mark: Decimal) -> Decimal:
    instrument = cache.instrument(instrument_id)
    total = Decimal(0)
    if instrument is None:
        return total
    for position in cache.positions_open(instrument_id=instrument_id):
        total += position.unrealized_pnl(instrument.make_price(mark)).as_decimal()
    return total


def position_snapshot(cache: Cache, instrument_id: InstrumentId, strategy: PaperStrategyView) -> Mapping[str, object]:
    """One coin's ``positions`` entry: open with its stop and exit time, flat with the mark, or ``unknown``.

    ``unknown`` means no quote yet; the entry then holds only ``instrument_id``, ``base``, and ``side``.
    """
    base = _base(cache, instrument_id)
    mark = _mid(cache, instrument_id)
    if mark is None:
        return {"instrument_id": instrument_id.value, "base": base, "side": "unknown"}
    positions = cache.positions_open(instrument_id=instrument_id)
    if not positions:
        return {"instrument_id": instrument_id.value, "base": base, "side": "flat", "mark_px": str(mark)}
    position = positions[0]
    facts = position_facts(position)
    stop = strategy.stop_order()
    trigger = None if stop is None else getattr(stop, "trigger_price", None)
    return {
        "instrument_id": instrument_id.value,
        "base": base,
        "side": "long" if position.side == PositionSide.LONG else "short",
        "quantity": str(facts.quantity),
        "avg_entry_px": str(_rounded(weighted_avg(facts.opening), _tick(cache, instrument_id))),
        "mark_px": str(mark),
        "unrealized_pnl": str(_unrealized(cache, instrument_id, mark)),
        "stop_px": None if trigger is None else str(trigger.as_decimal()),
        "stop_status": None if stop is None else stop.status.name.lower(),
        "opened_ns": position.ts_opened,
        "exit_due_ns": strategy.exit_due_ns,
        "currency": facts.currency,
    }


def positions_snapshot(cache: Cache, coins: Sequence[tuple[InstrumentId, PaperStrategyView]]) -> Mapping[str, object]:
    """``{env}:snapshot:positions``: one entry per coin, in the order of ``coins`` (sorted by the caller)."""
    return {"coins": [position_snapshot(cache, instrument_id, view) for instrument_id, view in coins]}


def paper_status(
    gate: PauseGate, blocks: EntryBlocks, backlog: int, strategies: Sequence[PaperStrategyView]
) -> PaperStatus:
    """The paper loop's ``trader_state`` fields: blocks plus pause, the recording backlog, the exit state.

    ``exiting`` is true while any coin exits; ``position_opened_ns`` is the earliest open position's.
    """
    opened = [ns for ns in (view.position_opened_ns for view in strategies) if ns is not None]
    return PaperStatus(
        entries_blocked=entries_blocked(blocks, gate.paused, gate.halted),
        recording_backlog=backlog,
        exiting=any(view.exiting for view in strategies),
        position_opened_ns=min(opened, default=None),
        entries_blocked_since=blocks.since(),
    )


def account_snapshot(
    portfolio: Portfolio, cache: Cache, instrument_ids: Sequence[InstrumentId], currency: Currency
) -> Mapping[str, object] | None:
    """``{env}:snapshot:account``: balances in ``currency`` and equity (balance plus unrealized P&L).

    The unrealized P&L sums every coin's open positions, each at its own coin's mid; a coin with no
    mid yet adds nothing. All coins share one venue, so the account is one.
    """
    account = portfolio.account(instrument_ids[0].venue)
    if account is None:
        return None
    total = account.balance_total(currency)
    if total is None:
        return None
    free = account.balance_free(currency)
    locked = account.balance_locked(currency)
    unrealized = Decimal(0)
    for instrument_id in instrument_ids:
        mark = _mid(cache, instrument_id)
        if mark is not None:
            unrealized += _unrealized(cache, instrument_id, mark)
    return {
        "balance_total": str(total.as_decimal()),
        "balance_free": str(Decimal(0) if free is None else free.as_decimal()),
        "margin_used": str(Decimal(0) if locked is None else locked.as_decimal()),
        "equity": str(total.as_decimal() + unrealized),
        "currency": currency.code,
    }
