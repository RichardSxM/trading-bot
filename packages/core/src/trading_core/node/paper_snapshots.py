"""The ``position`` and ``account`` snapshot data the trader publishes every second (spec 0006 AC-16).

Money and prices are strings of ``Decimal`` so the dashboard never parses a float. Each
reader returns None until it knows something (no quote, no account yet), and the snapshot
writer skips that round.
"""

from collections.abc import Mapping
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency
from nautilus_trader.portfolio.portfolio import Portfolio

from trading_core.node.control import PaperStatus
from trading_core.node.placeholder import PlaceholderTimed
from trading_core.node.recording import position_facts
from trading_core.node.strategy import PauseGate
from trading_core.recording.mappers import weighted_avg
from trading_core.strategy.blocks import EntryBlocks, entries_blocked


def _mid(cache: Cache, instrument_id: InstrumentId) -> Decimal | None:
    quote = cache.quote_tick(instrument_id)
    if quote is None:
        return None
    mid: Decimal = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
    return mid


def _cents(value: Decimal | None) -> Decimal | None:
    """An average price for display, at cents: the division leaves more digits than any fill had."""
    return None if value is None else value.quantize(Decimal("0.01"))


def _unrealized(cache: Cache, instrument_id: InstrumentId, mark: Decimal) -> Decimal:
    instrument = cache.instrument(instrument_id)
    total = Decimal(0)
    if instrument is None:
        return total
    for position in cache.positions_open(instrument_id=instrument_id):
        total += position.unrealized_pnl(instrument.make_price(mark)).as_decimal()
    return total


def position_snapshot(
    cache: Cache, instrument_id: InstrumentId, strategy: PlaceholderTimed
) -> Mapping[str, object] | None:
    """``{env}:snapshot:position``: the open position with its stop and exit time, or flat with the mark."""
    mark = _mid(cache, instrument_id)
    if mark is None:
        return None
    positions = cache.positions_open(instrument_id=instrument_id)
    if not positions:
        return {"instrument_id": instrument_id.value, "side": "flat", "mark_px": str(mark)}
    position = positions[0]
    facts = position_facts(position)
    stop = strategy.stop_order()
    trigger = None if stop is None else getattr(stop, "trigger_price", None)
    return {
        "instrument_id": instrument_id.value,
        "side": "long" if position.side == PositionSide.LONG else "short",
        "quantity": str(facts.quantity),
        "avg_entry_px": str(_cents(weighted_avg(facts.opening))),
        "mark_px": str(mark),
        "unrealized_pnl": str(_unrealized(cache, instrument_id, mark)),
        "stop_px": None if trigger is None else str(trigger.as_decimal()),
        "stop_status": None if stop is None else stop.status.name.lower(),
        "opened_ns": position.ts_opened,
        "exit_due_ns": strategy.exit_due_ns,
        "currency": facts.currency,
    }


def paper_status(gate: PauseGate, blocks: EntryBlocks, backlog: int, strategy: PlaceholderTimed) -> PaperStatus:
    """The paper loop's ``trader_state`` fields: blocks plus pause, the recording backlog, the exit state."""
    return PaperStatus(
        entries_blocked=entries_blocked(blocks, gate.paused),
        recording_backlog=backlog,
        exiting=strategy.exiting,
        position_opened_ns=strategy.position_opened_ns,
    )


def account_snapshot(
    portfolio: Portfolio, cache: Cache, instrument_id: InstrumentId, currency: Currency
) -> Mapping[str, object] | None:
    """``{env}:snapshot:account``: balances in ``currency`` and equity (balance plus unrealized P&L)."""
    account = portfolio.account(instrument_id.venue)
    if account is None:
        return None
    total = account.balance_total(currency)
    if total is None:
        return None
    free = account.balance_free(currency)
    locked = account.balance_locked(currency)
    mark = _mid(cache, instrument_id)
    unrealized = Decimal(0) if mark is None else _unrealized(cache, instrument_id, mark)
    return {
        "balance_total": str(total.as_decimal()),
        "balance_free": str(Decimal(0) if free is None else free.as_decimal()),
        "margin_used": str(Decimal(0) if locked is None else locked.as_decimal()),
        "equity": str(total.as_decimal() + unrealized),
        "currency": currency.code,
    }
