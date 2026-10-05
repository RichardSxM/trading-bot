"""Read queries over the recorded business data (spec 0002).

``journal_rows`` turns fills, fees, and funding of paper and live runs into signed cash
flows (positive is money in, negative is money out), each with a EUR amount from
``fx_rates``. Feature 18 exports them to CSV. ``round_trips_page`` and ``fills_page`` are the
Trades page lists (spec 0006 AC-19), paper runs only.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import Date, ScalarSelect, String, cast, func, literal, select, true
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from trading_core.recording import models
from trading_core.recording.enums import Env, Intent, OrderSide, OrderType, PositionSide, PositionStatus, RunMode
from trading_core.recording.errors import UnsupportedInstrumentError
from trading_core.strategy.placeholder import ExitReason, exit_reason

# Rows per page on the Trades page (spec 0006 AC-19).
PAGE_SIZE: Final = 50
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_TRADING_MODES: Final = (RunMode.PAPER, RunMode.LIVE)
# Products and negation of exact Decimals stay exact: no digit is ever rounded away.
_EXACT: Final = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
_FX_CURRENCY: Final = "USD"


class JournalKind(StrEnum):
    """What a journal row records; ``fee`` rows share the id of their fill."""

    FILL = "fill"
    FEE = "fee"
    FUNDING = "funding"


_KIND_ORDER: Final = {JournalKind.FILL: 0, JournalKind.FEE: 1, JournalKind.FUNDING: 2}


@dataclass(frozen=True, kw_only=True)
class JournalRow:
    """One signed cash flow of the trade journal; ``side``, ``quantity``, ``price`` on fill rows only."""

    kind: JournalKind
    at: datetime
    source_id: UUID
    run_id: UUID
    instrument_id: str
    position_id: UUID | None
    side: OrderSide | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal
    currency: str
    eur_amount: Decimal | None
    fx_missing: bool


def _utc_ns(name: str, value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC aware datetime, got {value!r}")
    return (value - _EPOCH) // timedelta(microseconds=1) * 1000


def _eur_rate(at: InstrumentedAttribute[datetime]) -> ScalarSelect[Decimal]:
    """USD per EUR on the event's UTC date, or the latest earlier date (weekends, holidays)."""
    fx = models.FxRate
    return (
        select(fx.rate)
        .where(
            fx.base_currency == "EUR",
            fx.quote_currency == _FX_CURRENCY,
            fx.rate_date <= cast(func.timezone("UTC", at), Date),
        )
        .order_by(fx.rate_date.desc())
        .limit(1)
        .scalar_subquery()
    )


def _eur(amount: Decimal, currency: str, rate: Decimal | None) -> tuple[Decimal | None, bool]:
    if currency != _FX_CURRENCY or rate is None:
        return None, True
    return amount / rate, False


def _fill_rows(fill: models.Fill, settle_currency: str, multiplier: Decimal, rate: Decimal | None) -> list[JournalRow]:
    value = _EXACT.multiply(_EXACT.multiply(fill.quantity, fill.price), multiplier)
    amount = _EXACT.minus(value) if fill.side == OrderSide.BUY else value
    eur_amount, fx_missing = _eur(amount, settle_currency, rate)
    trade = JournalRow(
        kind=JournalKind.FILL,
        at=fill.filled_at,
        source_id=fill.id,
        run_id=fill.run_id,
        instrument_id=fill.instrument_id,
        position_id=fill.position_id,
        side=OrderSide(fill.side),
        quantity=fill.quantity,
        price=fill.price,
        amount=amount,
        currency=settle_currency,
        eur_amount=eur_amount,
        fx_missing=fx_missing,
    )
    if fill.fee == 0:
        return [trade]
    fee = _EXACT.minus(fill.fee)
    eur_fee, fee_fx_missing = _eur(fee, fill.fee_currency, rate)
    return [
        trade,
        replace(
            trade,
            kind=JournalKind.FEE,
            side=None,
            quantity=None,
            price=None,
            amount=fee,
            currency=fill.fee_currency,
            eur_amount=eur_fee,
            fx_missing=fee_fx_missing,
        ),
    ]


def _funding_row(funding: models.FundingPayment, rate: Decimal | None) -> JournalRow:
    eur_amount, fx_missing = _eur(funding.amount, funding.currency, rate)
    return JournalRow(
        kind=JournalKind.FUNDING,
        at=funding.paid_at,
        source_id=funding.id,
        run_id=funding.run_id,
        instrument_id=funding.instrument_id,
        position_id=funding.position_id,
        amount=funding.amount,
        currency=funding.currency,
        eur_amount=eur_amount,
        fx_missing=fx_missing,
    )


def journal_rows(session: Session, env: Env, start: datetime, end: datetime) -> Sequence[JournalRow]:
    """Every fill, fee, and funding payment of ``env``'s paper and live runs in ``[start, end)``.

    Rows are signed cash flows ordered by ``at``, then ``source_id``, then kind (``fill``
    before ``fee``). A fee row appears only when the fee is not zero. ``eur_amount`` is the
    USD amount divided by the ECB rate of the event's UTC date (or the latest earlier one);
    any other currency, or no rate, gives ``None`` and ``fx_missing``. Raises ``ValueError``
    for a naive or non UTC bound, and ``UnsupportedInstrumentError`` when a fill in range is
    on an inverse instrument.
    """
    start_ns, end_ns = _utc_ns("start", start), _utc_ns("end", end)
    fill, funding, run, instrument = models.Fill, models.FundingPayment, models.StrategyRun, models.Instrument

    fills = session.execute(
        select(
            fill, instrument.settle_currency, instrument.multiplier, instrument.is_inverse, _eur_rate(fill.filled_at)
        )
        .join(run, run.id == fill.run_id)
        .join(instrument, instrument.instrument_id == fill.instrument_id)
        .where(fill.env == env, run.mode.in_(_TRADING_MODES), fill.filled_ns >= start_ns, fill.filled_ns < end_ns)
    ).all()
    inverse = sorted({row.instrument_id for row, _, _, is_inverse, _ in fills if is_inverse})
    if inverse:
        raise UnsupportedInstrumentError(f"the journal supports linear contracts only; inverse in range: {inverse}")

    payments = session.execute(
        select(funding, _eur_rate(funding.paid_at))
        .join(run, run.id == funding.run_id)
        .where(funding.env == env, run.mode.in_(_TRADING_MODES), funding.paid_ns >= start_ns, funding.paid_ns < end_ns)
    ).all()

    rows = [r for f, currency, multiplier, _, rate in fills for r in _fill_rows(f, currency, multiplier, rate)]
    rows += [_funding_row(payment, rate) for payment, rate in payments]
    return sorted(rows, key=lambda r: (r.at, r.source_id, _KIND_ORDER[r.kind]))


@dataclass(frozen=True, kw_only=True)
class RoundTrip:
    """One closed position of a paper run, as the Trades page lists it (spec 0006 AC-19).

    ``side`` is the opening side from the earliest fill; it and ``exit_reason`` are ``None``
    when no fill is linked (``exit_reason`` is still ``abandoned`` for an abandoned one).
    """

    position_id: UUID
    opened_at: datetime
    closed_at: datetime | None
    side: PositionSide | None
    size: Decimal
    avg_entry_px: Decimal
    avg_exit_px: Decimal | None
    realized_pnl: Decimal
    fees: Decimal
    currency: str
    exit_reason: ExitReason | None


@dataclass(frozen=True, kw_only=True)
class FillLine:
    """One fill of a paper run, as the Trades page lists it (spec 0006 AC-19)."""

    fill_id: UUID
    filled_at: datetime
    instrument_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str


_OPENING_SIDE: Final = {OrderSide.BUY: PositionSide.LONG, OrderSide.SELL: PositionSide.SHORT}


def _offset(page: int, size: int) -> int:
    if page < 1 or size < 1:
        raise ValueError(f"page and size must be at least 1, got page {page}, size {size}")
    return (page - 1) * size


def _exit_reason(abandoned: bool, order_type: str | None, intent: str | None) -> ExitReason | None:
    if not abandoned and order_type is None:
        return None
    return exit_reason(
        abandoned, None if order_type is None else OrderType(order_type), None if intent is None else Intent(intent)
    )


def round_trips_page(session: Session, env: Env, page: int, size: int = PAGE_SIZE) -> Sequence[RoundTrip]:
    """Page ``page`` (from 1) of ``env``'s closed paper positions, newest ``closed_at`` first, then ``id``.

    The side comes from the position's earliest fill (``filled_ns``, then ``id``): ``buy`` is long,
    ``sell`` is short. The exit reason is ``abandoned`` when a ``risk_events`` row keyed
    ``abandoned:<position_id>`` exists, otherwise ``exit_reason`` on the order (and its decision)
    of the position's last fill. A page past the end is empty. Raises ``ValueError`` for a page
    or size below 1.
    """
    offset = _offset(page, size)
    position, run = models.Position, models.StrategyRun
    first, last, order, decision = aliased(models.Fill), aliased(models.Fill), models.Order, models.Decision
    opening = (
        select(first.side)
        .where(first.position_id == position.id, first.env == position.env)
        .order_by(first.filled_ns, first.id)
        .limit(1)
        .lateral("opening")
    )
    closing = (
        select(order.order_type, decision.intent)
        .select_from(last)
        .join(order, order.id == last.order_id)
        .outerjoin(decision, decision.id == order.decision_id)
        .where(last.position_id == position.id, last.env == position.env)
        .order_by(last.filled_ns.desc(), last.id.desc())
        .limit(1)
        .lateral("closing")
    )
    abandoned = (
        select(models.RiskEvent.id)
        .where(
            models.RiskEvent.env == position.env,
            models.RiskEvent.event_key == literal("abandoned:") + cast(position.id, String),
        )
        .exists()
    )
    rows = session.execute(
        select(position, opening.c.side, closing.c.order_type, closing.c.intent, abandoned)
        .join(run, run.id == position.run_id)
        .outerjoin(opening, true())
        .outerjoin(closing, true())
        .where(
            position.env == env,
            run.env == env,
            run.mode == RunMode.PAPER,
            position.status == PositionStatus.CLOSED,
        )
        .order_by(position.closed_at.desc(), position.id.desc())
        .limit(size)
        .offset(offset)
    ).all()
    return [
        RoundTrip(
            position_id=p.id,
            opened_at=p.opened_at,
            closed_at=p.closed_at,
            side=None if side is None else _OPENING_SIDE[OrderSide(side)],
            size=p.peak_qty,
            avg_entry_px=p.avg_entry_px,
            avg_exit_px=p.avg_exit_px,
            realized_pnl=p.realized_pnl,
            fees=p.fees_total,
            currency=p.currency,
            exit_reason=_exit_reason(is_abandoned, order_type, intent),
        )
        for p, side, order_type, intent, is_abandoned in rows
    ]


def fills_page(session: Session, env: Env, page: int, size: int = PAGE_SIZE) -> Sequence[FillLine]:
    """Page ``page`` (from 1) of ``env``'s paper fills, newest ``filled_at`` first, then ``id``.

    A page past the end is empty. Raises ``ValueError`` for a page or size below 1.
    """
    offset = _offset(page, size)
    fill, run = models.Fill, models.StrategyRun
    fills = session.scalars(
        select(fill)
        .join(run, run.id == fill.run_id)
        .where(fill.env == env, run.env == env, run.mode == RunMode.PAPER)
        .order_by(fill.filled_at.desc(), fill.id.desc())
        .limit(size)
        .offset(offset)
    ).all()
    return [
        FillLine(
            fill_id=f.id,
            filled_at=f.filled_at,
            instrument_id=f.instrument_id,
            side=OrderSide(f.side),
            quantity=f.quantity,
            price=f.price,
            fee=f.fee,
            fee_currency=f.fee_currency,
        )
        for f in fills
    ]


def control_command_known(session: Session, command_id: UUID) -> bool:
    """True when the trader already recorded the control command ``command_id`` (spec 0005 AC-6)."""
    return session.get(models.ControlCommand, command_id) is not None
