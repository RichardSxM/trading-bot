"""covers: spec 0024 AC-3 (one line per journal row, in order, header only when empty), AC-4 (EUR from the row's rate,
rounded once), AC-5 (a missing or unpublished rate refuses), AC-6 (an inverse instrument refuses), AC-7 to AC-9 (the
summary's totals, balances, and check over Postgres), AC-11 (the summary equals the journal.csv sums), AC-14 (month
chunks give the same rows in the same order)."""

import csv
import io
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from trading_core.journal.bounds import parse_range
from trading_core.journal.export import JOURNAL_COLUMNS, JournalRefusedError, eur_cents
from trading_core.journal.summary import CheckStatus
from trading_core.recording import models, repository
from trading_core.recording.enums import Env, FundingSource, Liquidity, OrderOrigin, OrderSide, PositionStatus, RunMode
from trading_core.recording.journal_export import JournalCsv, journal_refusal, journal_summary
from trading_core.recording.queries import balances_at, journal_check_positions, journal_rows
from trading_core.recording.rows import AccountSnapshotRow, FundingRow, FxRateRow, RunRow, column_values

from .conftest import INSTRUMENT, World, instrument_row

# 2031: far from the other tests' clocks. 1 January 2031 is a Wednesday, so the 6th is a Monday.
TODAY = date(2031, 3, 1)


def _at(day: int, hour: int = 12, month: int = 1) -> datetime:
    return datetime(2031, month, day, hour, tzinfo=UTC)


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _rates(session: Session, rates: dict[date, str]) -> None:
    for day, rate in rates.items():
        session.execute(insert(models.FxRate).values(column_values(FxRateRow(rate_date=day, rate=Decimal(rate)))))


def _trade(session: Session, world: World, at: datetime, side: OrderSide = OrderSide.BUY, **fill: object) -> None:
    instrument_id = fill.get("instrument_id", INSTRUMENT)
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION, side=side, instrument_id=instrument_id)
    repository.upsert_order(session, order)
    repository.record_fill(session, world.fill(order.client_order_id, side=side, filled_ns=_ns(at), **fill))


def _funding(session: Session, world: World, at: datetime, amount: str, position_id: UUID | None = None) -> None:
    row = FundingRow(
        position_id=position_id,
        env=Env.DEMO,
        run_id=world.run_id,
        instrument_id=INSTRUMENT,
        amount=Decimal(amount),
        currency="USD",
        source=FundingSource.KRAKEN_LOG,
        external_id=f"LOG-{uuid7().hex[-8:]}",
        paid_ns=_ns(at),
    )
    repository.record_funding(session, row)


def _csv(session: Session, start: str, end: str) -> tuple[list[dict[str, str]], JournalCsv, str]:
    export = JournalCsv(session, Env.DEMO, parse_range(start, end, "paper", TODAY))
    text = "".join(export)
    return list(csv.DictReader(io.StringIO(text, newline=""))), export, text


def _week(session: Session) -> None:
    """Rates Friday the 3rd, then Monday to Friday of the next week."""
    _rates(
        session,
        {
            date(2031, 1, 3): "1.2000",
            date(2031, 1, 6): "1.2500",
            date(2031, 1, 7): "1.2600",
            date(2031, 1, 8): "1.2700",
            date(2031, 1, 9): "1.2800",
            date(2031, 1, 10): "1.2900",
        },
    )


def test_a_week_exports_every_row_with_its_rate_and_rounded_eur(session: Session, world: World):
    _week(session)
    _trade(session, world, _at(6, 9), quantity=Decimal("0.002"), price=Decimal("60000.5"), fee=Decimal("-0.01"))
    _funding(session, world, _at(7, 16), "-0.0123")
    _trade(
        session,
        world,
        _at(8, 9),
        OrderSide.SELL,
        quantity=Decimal("0.002"),
        price=Decimal("60100"),
        fee=Decimal("0.0601"),
    )

    assert journal_refusal(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY)) is None
    lines, export, text = _csv(session, "2031-01-06", "2031-01-09")

    assert text.splitlines()[0] == ",".join(JOURNAL_COLUMNS)
    assert export.rows == len(lines) == 5
    assert [(r["kind"], r["amount"], r["fx_rate"], r["fx_rate_date"]) for r in lines] == [
        ("fill", "-120.0010", "1.2500", "2031-01-06"),
        ("fee", "0.01", "1.2500", "2031-01-06"),  # a rebate is money in
        ("funding", "-0.0123", "1.2600", "2031-01-07"),
        ("fill", "120.200", "1.2700", "2031-01-08"),  # the exact product, no digit added
        ("fee", "-0.0601", "1.2700", "2031-01-08"),
    ]
    assert [r["eur_amount"] for r in lines] == ["-96.00", "0.01", "-0.01", "94.65", "-0.05"]
    for line in lines:
        assert Decimal(line["eur_amount"]) == eur_cents(Decimal(line["amount"]), Decimal(line["fx_rate"]))
    first = lines[0]
    assert (first["env"], first["mode"], first["side"], first["liquidity"], first["strategy"]) == (
        "demo",
        "paper",
        "buy",
        Liquidity.MAKER.value,
        "tracer",
    )
    assert first["at_utc"] == "2031-01-06T09:00:00.000000Z"
    assert first["at_amsterdam"] == "2031-01-06T10:00:00.000000+01:00"
    assert first["run_id"] == str(world.run_id)
    assert lines[2]["side"] == lines[2]["quantity"] == lines[2]["liquidity"] == ""


def test_a_range_with_no_rows_is_the_header_only(session: Session, world: World):
    _week(session)

    lines, export, text = _csv(session, "2031-01-06", "2031-01-09")

    assert (lines, export.rows) == ([], 0)
    assert text == ",".join(JOURNAL_COLUMNS) + "\r\n"


def test_a_fill_before_any_rate_refuses_naming_its_date(session: Session, world: World):
    _rates(session, {date(2031, 1, 6): "1.25", date(2031, 1, 9): "1.28"})
    _trade(session, world, _at(2), fee=Decimal(0))
    _trade(session, world, _at(7), fee=Decimal(0))

    refusal = journal_refusal(session, Env.DEMO, parse_range("2031-01-01", "2031-01-09", "paper", TODAY))

    assert refusal is not None
    assert refusal.reason == "fx_missing"
    assert str(refusal).startswith("No ECB rate for 2031-01-02 yet.")


def test_an_end_past_the_newest_rate_refuses(session: Session, world: World):
    _rates(session, {date(2031, 1, 6): "1.25", date(2031, 1, 7): "1.26"})
    _trade(session, world, _at(7), fee=Decimal(0))

    refusal = journal_refusal(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY))

    assert refusal is not None
    assert str(refusal).startswith("No ECB rate for 2031-01-09 yet.")


def test_a_btc_fee_refuses_naming_the_currency(session: Session, world: World):
    _week(session)
    _trade(session, world, _at(7), fee=Decimal("0.0000001"), fee_currency="BTC")

    refusal = journal_refusal(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY))

    assert refusal is not None
    assert str(refusal) == "Currency BTC has no EUR rate."


def test_an_inverse_instrument_refuses(session: Session, world: World):
    _week(session)
    inverse = replace(instrument_row(), instrument_id="FI_XBTUSD.KRAKEN", symbol="FI_XBTUSD", is_inverse=True)
    repository.upsert_instrument(session, inverse)
    _trade(session, world, _at(8), instrument_id=inverse.instrument_id)

    refusal = journal_refusal(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY))

    assert refusal is not None
    assert refusal.reason == "inverse_instrument"
    assert "FI_XBTUSD" in str(refusal)


def test_month_chunks_give_the_same_rows_in_the_same_order(session: Session, world: World):
    _rates(session, {date(2031, 1, 28): "1.25", date(2031, 2, 3): "1.26", date(2031, 3, 3): "1.27"})
    # 23:30 UTC on 31 January is 00:30 on 1 February in Amsterdam: the February chunk.
    for at in (_at(29), _at(31, 22), datetime(2031, 1, 31, 23, 30, tzinfo=UTC), _at(2, month=2), _at(27, month=2)):
        _trade(session, world, at, fee=Decimal(0))
    journal_range = parse_range("2031-01-29", "2031-02-27", "paper", date(2031, 3, 4))

    assert journal_refusal(session, Env.DEMO, journal_range) is None
    export = JournalCsv(session, Env.DEMO, journal_range)
    chunks = list(export)
    whole = journal_rows(session, Env.DEMO, journal_range.start_utc, journal_range.end_utc)

    assert len(chunks) == 3  # the header, January, February
    lines = list(csv.DictReader(io.StringIO("".join(chunks), newline="")))
    assert [line["source_id"] for line in lines] == [str(row.source_id) for row in whole]
    assert lines[2]["at_amsterdam"] == "2031-02-01T00:30:00.000000+01:00"
    assert chunks[2].startswith("demo,fill,paper,2031-01-31T23:30:00")


@pytest.fixture(autouse=True)
def _no_crossing_positions_from_other_tests(session: Session) -> None:
    """Some tests commit positions that are still open, or close years later, and would cross every 2031 range.
    Close each at its own open time in this test's transaction, which rolls back at its end."""
    position = models.Position
    session.execute(update(position).values(status=PositionStatus.CLOSED, closed_at=position.opened_at))


def _position(session: Session, world: World, opened: datetime, closed: datetime | None, **values: object) -> UUID:
    status = PositionStatus.OPEN if closed is None else PositionStatus.CLOSED
    row = world.position(opened_ns=_ns(opened), closed_at=closed, status=status, **values)
    repository.upsert_position(session, row)
    return row.id


def _replay_run(session: Session, world: World) -> UUID:
    row = RunRow(
        env=Env.DEMO,
        mode=RunMode.REPLAY,
        strategy_version_id=world.version_id,
        trader_id=f"T-replay-{uuid7().hex[-8:]}",
        run_config={},
        started_at=_at(1),
    )
    session.execute(insert(models.StrategyRun).values(column_values(row)))
    return row.id


def _snapshot(session: Session, run_id: UUID | None, at: datetime, total: str, currency: str = "USD") -> None:
    repository.record_account_snapshot(
        session,
        AccountSnapshotRow(
            env=Env.DEMO,
            run_id=run_id,
            account_ref="KRAKEN-001",
            balance_total=Decimal(total),
            balance_free=Decimal(total),
            margin_used=Decimal(0),
            currency=currency,
            captured_ns=_ns(at),
        ),
    )


def test_the_summary_equals_the_csv_sums_and_matches_the_positions(session: Session, world: World):
    _week(session)
    position = _position(session, world, _at(6, 9), _at(8, 9), realized_pnl=Decimal("0.1489"), funding_total=Decimal(0))
    _trade(
        session,
        world,
        _at(6, 9),
        position_id=position,
        quantity=Decimal("0.002"),
        price=Decimal("60000.5"),
        fee=Decimal("-0.01"),
    )
    _funding(session, world, _at(7, 16), "-0.0123", position)
    _trade(
        session,
        world,
        _at(8, 9),
        OrderSide.SELL,
        position_id=position,
        quantity=Decimal("0.002"),
        price=Decimal("60100"),
        fee=Decimal("0.0601"),
    )
    _snapshot(session, world.run_id, _at(5), "1000")
    _snapshot(session, world.run_id, _at(9), "1000.1366")
    journal_range = parse_range("2031-01-06", "2031-01-09", "paper", TODAY)

    summary = journal_summary(session, Env.DEMO, journal_range)
    lines, _, _ = _csv(session, "2031-01-06", "2031-01-09")

    assert summary.rows == len(lines) == 5
    usd = summary.native["USD"]
    for item, kind in (("trading", "fill"), ("fees", "fee"), ("funding", "funding")):
        assert getattr(usd, item) == sum(Decimal(r["amount"]) for r in lines if r["kind"] == kind)
        assert getattr(summary.eur, item) == sum(Decimal(r["eur_amount"]) for r in lines if r["kind"] == kind)
    assert summary.eur.net == sum(Decimal(r["eur_amount"]) for r in lines)
    assert usd.net == Decimal("0.1366")
    check = summary.check
    assert (check.status, check.dashboard_net, check.counted, check.crossing) == (
        CheckStatus.MATCH,
        Decimal("0.1366"),
        1,
        0,
    )
    [start] = summary.balance_start
    [end] = summary.balance_end
    assert start.snapshot is not None
    assert start.snapshot.balance_total == Decimal("1000")
    assert start.eur == eur_cents(Decimal("1000"), Decimal("1.2000"))  # the 3rd's rate for a Sunday 23:00 UTC bound
    assert end.snapshot is not None
    assert end.snapshot.captured_at == _at(9)
    assert end.eur == eur_cents(Decimal("1000.1366"), Decimal("1.2800"))


def test_a_changed_realized_pnl_is_a_mismatch(session: Session, world: World):
    _week(session)
    position = _position(session, world, _at(6, 9), _at(6, 10), realized_pnl=Decimal("0.05"))
    _trade(session, world, _at(6, 9), position_id=position, fee=Decimal(0))

    check = journal_summary(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY)).check

    assert check.status is CheckStatus.MISMATCH
    assert check.difference == Decimal("-60.0505")


def test_a_fill_without_a_position_makes_the_check_not_comparable(session: Session, world: World):
    _week(session)
    _trade(session, world, _at(7), fee=Decimal(0))

    check = journal_summary(session, Env.DEMO, parse_range("2031-01-06", "2031-01-09", "paper", TODAY)).check

    assert (check.status, check.reason, check.unlinked_rows) == (CheckStatus.NOT_COMPARABLE, "unlinked", 1)
    assert check.unlinked_net == Decimal("-60.0005")


def test_the_summary_refuses_like_the_csv(session: Session, world: World):
    _rates(session, {date(2031, 1, 6): "1.25"})
    _trade(session, world, _at(7), fee=Decimal(0))

    with pytest.raises(JournalRefusedError, match="No ECB rate for 2031-01-07"):
        journal_summary(session, Env.DEMO, parse_range("2031-01-06", "2031-01-07", "paper", TODAY))


def test_positions_crossing_either_edge_are_counted_apart(session: Session, world: World):
    start, end = _at(6, 0), _at(9, 0)
    _position(session, world, _at(5), _at(6, 12))  # open at the start
    _position(session, world, _at(8), None)  # open at the end
    _position(session, world, _at(8), _at(9, 0))  # closed exactly at the end: open at it, crosses
    _position(session, world, _at(2), _at(6, 0))  # closed exactly at the start: in range, does not cross
    _position(session, world, _at(6, 1), _at(7), realized_pnl=Decimal("1.5"), funding_total=Decimal("-0.25"))
    _position(session, world, _at(1), _at(3), realized_pnl=Decimal(100))  # before the range
    _position(session, world, _at(7), _at(7, 1), run_id=_replay_run(session, world), realized_pnl=Decimal(9))

    figures = journal_check_positions(session, Env.DEMO, {RunMode.PAPER}, start, end)

    assert figures.crossing == 3
    assert figures.counted == 3  # closed at the start, at 12:00 on the 6th, and on the 7th
    assert figures.dashboard_net == Decimal("1.25")


def test_balances_take_the_latest_snapshot_of_the_mode_on_or_before_the_bound(session: Session, world: World):
    replay = _replay_run(session, world)
    _snapshot(session, world.run_id, _at(3), "900")
    _snapshot(session, world.run_id, _at(5), "950")
    _snapshot(session, world.run_id, _at(5), "12", currency="EUR")
    _snapshot(session, world.run_id, _at(6, 1), "999")  # after the bound
    _snapshot(session, replay, _at(5, 12), "1")  # another mode
    _snapshot(session, None, _at(5, 13), "2")  # NULL run counts as live, only in the live env

    found = balances_at(session, Env.DEMO, {RunMode.PAPER}, _at(5, 23))

    assert [(s.currency, s.balance_total, s.captured_at) for s in found] == [
        ("EUR", Decimal("12"), _at(5)),
        ("USD", Decimal("950"), _at(5)),
    ]
    assert balances_at(session, Env.DEMO, {RunMode.PAPER}, _at(5)) == found  # captured at the bound counts
    assert balances_at(session, Env.DEMO, {RunMode.PAPER}, _at(2)) == []
