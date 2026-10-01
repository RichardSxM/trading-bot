"""covers: spec 0002 AC-14 (journal rows: signed cash flows, EUR amounts, range and filters)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import Env, FundingSource, OrderOrigin, OrderSide, RunMode
from trading_core.recording.errors import UnsupportedInstrumentError
from trading_core.recording.queries import JournalKind, journal_rows
from trading_core.recording.rows import FundingRow, FxRateRow, column_values

from .conftest import INSTRUMENT, World, instrument_row

# Far from the other tests' clocks: 2031-01-01 is a Wednesday, so the 3rd is a Friday.
JAN = {day: datetime(2031, 1, day, tzinfo=UTC) for day in range(1, 32)}


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _rate(session: Session, day: datetime, rate: str) -> None:
    session.execute(insert(models.FxRate).values(column_values(FxRateRow(rate_date=day.date(), rate=Decimal(rate)))))


def _trade(session: Session, world: World, at: datetime, side: OrderSide = OrderSide.BUY, **fill: object) -> None:
    """An order and one fill on it at ``at``."""
    instrument_id = fill.get("instrument_id", INSTRUMENT)
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION, side=side, instrument_id=instrument_id)
    repository.upsert_order(session, order)
    repository.record_fill(session, world.fill(order.client_order_id, side=side, filled_ns=_ns(at), **fill))


def _journal(session: Session, start: datetime = JAN[1], end: datetime = JAN[31]):
    return journal_rows(session, Env.DEMO, start, end)


def test_a_flat_round_trip_sums_to_the_net_result(session: Session, world: World):
    _rate(session, JAN[6], "1.25")
    buy_at, sell_at = JAN[6] + timedelta(hours=9), JAN[6] + timedelta(hours=15)
    _trade(session, world, buy_at, quantity=Decimal("0.002"), price=Decimal("60000.5"), fee=Decimal("0.06"))
    _trade(
        session,
        world,
        sell_at,
        OrderSide.SELL,
        quantity=Decimal("0.002"),
        price=Decimal("60100"),
        fee=Decimal("0.0601"),
    )
    funding = FundingRow(
        env=Env.DEMO,
        run_id=world.run_id,
        instrument_id=INSTRUMENT,
        amount=Decimal("-0.0123"),
        currency="USD",
        source=FundingSource.KRAKEN_LOG,
        external_id=f"LOG-{uuid7().hex[-8:]}",
        paid_ns=_ns(JAN[6] + timedelta(hours=12)),
    )
    repository.record_funding(session, funding)

    rows = _journal(session)

    assert [r.kind for r in rows] == ["fill", "fee", "funding", "fill", "fee"]
    assert [r.amount for r in rows] == [
        Decimal("-120.0010"),
        Decimal("-0.06"),
        Decimal("-0.0123"),
        Decimal("120.2000"),
        Decimal("-0.0601"),
    ]
    gross = Decimal("0.002") * (Decimal("60100") - Decimal("60000.5"))
    assert sum(r.amount for r in rows) == gross - Decimal("0.06") - Decimal("0.0601") + Decimal("-0.0123")
    assert all(r.currency == "USD" and not r.fx_missing for r in rows)
    assert rows[0].eur_amount == Decimal("-120.0010") / Decimal("1.25")
    assert (rows[0].side, rows[0].quantity, rows[0].price) == (OrderSide.BUY, Decimal("0.002"), Decimal("60000.5"))
    assert (rows[1].side, rows[1].quantity, rows[1].price) == (None, None, None)
    assert rows[1].source_id == rows[0].source_id
    assert rows[1].at == rows[0].at == buy_at


def test_fee_rows_follow_the_fee_sign_and_currency(session: Session, world: World):
    _rate(session, JAN[6], "1.25")
    _trade(session, world, JAN[6] + timedelta(hours=1), fee=Decimal("-0.01"))
    _trade(session, world, JAN[6] + timedelta(hours=2), fee=Decimal(0))
    _trade(session, world, JAN[6] + timedelta(hours=3), fee=Decimal("0.0000001"), fee_currency="BTC")

    rows = _journal(session)

    assert [(r.kind, r.amount, r.currency) for r in rows if r.kind == JournalKind.FEE] == [
        ("fee", Decimal("0.01"), "USD"),
        ("fee", Decimal("-0.0000001"), "BTC"),
    ]
    assert [r.kind for r in rows] == ["fill", "fee", "fill", "fill", "fee"]
    btc_fee = rows[-1]
    assert (btc_fee.eur_amount, btc_fee.fx_missing) == (None, True)


def test_eur_uses_the_latest_rate_on_or_before_the_utc_date(session: Session, world: World):
    _rate(session, JAN[3], "1.20")  # Friday
    _rate(session, JAN[6], "1.30")  # Monday
    _trade(session, world, JAN[2] + timedelta(hours=12), fee=Decimal(0))  # Thursday, before any rate
    _trade(session, world, JAN[4] + timedelta(hours=23, minutes=59), fee=Decimal(0))  # Saturday
    _trade(session, world, JAN[6], fee=Decimal(0))  # Monday 00:00 UTC

    thursday, saturday, monday = _journal(session)

    assert (thursday.eur_amount, thursday.fx_missing) == (None, True)
    assert saturday.eur_amount == saturday.amount / Decimal("1.20")
    assert monday.eur_amount == monday.amount / Decimal("1.30")


def test_the_range_is_half_open_and_only_paper_and_live_runs_count(session: Session, world: World):
    backtest_id = repository.start_run(
        session,
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        version_id=world.version_id,
        trader_id=f"BACKTESTER-{uuid7().hex[-6:]}",
        run_config={},
        started_ns=_ns(JAN[1]),
    )
    _trade(session, replace(world, run_id=backtest_id), JAN[10], fee=Decimal(0))
    _trade(session, world, JAN[10], fee=Decimal(0))  # at start: included
    _trade(session, world, JAN[11], fee=Decimal(0))  # at end: excluded
    stub = world.fill("O-never-seen", filled_ns=_ns(JAN[10] + timedelta(hours=1)), fee=Decimal(0))
    repository.record_fill(session, stub)

    rows = _journal(session, JAN[10], JAN[11])

    assert {r.run_id for r in rows} == {world.run_id}
    assert [r.at for r in rows] == [JAN[10], JAN[10] + timedelta(hours=1)]


def test_bounds_must_be_utc_aware(session: Session, world: World):
    with pytest.raises(ValueError, match="start"):
        _journal(session, start=datetime(2031, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="end"):
        _journal(session, end=datetime(2031, 1, 31, tzinfo=timezone(timedelta(hours=1))))


def test_an_inverse_instrument_in_range_refuses_the_whole_journal(session: Session, world: World):
    inverse = replace(instrument_row(), instrument_id="FI_XBTUSD.KRAKEN", symbol="FI_XBTUSD", is_inverse=True)
    repository.upsert_instrument(session, inverse)
    _trade(session, world, JAN[6])
    _trade(session, world, JAN[7], instrument_id=inverse.instrument_id)

    with pytest.raises(UnsupportedInstrumentError, match="FI_XBTUSD"):
        _journal(session)
    assert len(_journal(session, JAN[6], JAN[7])) == 2
