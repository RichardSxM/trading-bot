"""covers: spec 0024 AC-15 (``write_fx_rates`` is idempotent, a differing rate conflicts and keeps the stored row)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.errors import RecordConflictError
from trading_core.recording.queries import newest_fx_date
from trading_core.recording.rows import FxRateRow

# Far from the other tests' dates.
DAY = date(2033, 3, 1)


def _stored(session: Session) -> list[tuple[date, Decimal]]:
    fx = models.FxRate
    return [(row.rate_date, row.rate) for row in session.execute(select(fx).where(fx.rate_date >= DAY)).scalars()]


def test_new_rows_are_inserted_and_counted(session: Session):
    rows = [
        FxRateRow(rate_date=DAY, rate=Decimal("1.1298")),
        FxRateRow(rate_date=date(2033, 3, 2), rate=Decimal("1.12")),
    ]

    assert repository.write_fx_rates(session, rows) == 2
    assert sorted(_stored(session)) == [(DAY, Decimal("1.1298")), (date(2033, 3, 2), Decimal("1.12"))]


def test_writing_the_same_rows_again_is_a_no_op(session: Session):
    rows = [FxRateRow(rate_date=DAY, rate=Decimal("1.1298"))]
    repository.write_fx_rates(session, rows)

    assert repository.write_fx_rates(session, rows) == 0
    assert repository.write_fx_rates(session, [FxRateRow(rate_date=DAY, rate=Decimal("1.12980"))]) == 0  # equal value
    assert _stored(session) == [(DAY, Decimal("1.1298"))]


def test_a_differing_rate_raises_and_keeps_the_stored_row(session: Session):
    repository.write_fx_rates(session, [FxRateRow(rate_date=DAY, rate=Decimal("1.1298"))])

    with pytest.raises(RecordConflictError) as raised:
        repository.write_fx_rates(session, [FxRateRow(rate_date=DAY, rate=Decimal("1.1300"))])

    assert raised.value.table == "fx_rates"
    assert raised.value.stored["rate"] == Decimal("1.1298")
    assert raised.value.incoming["rate"] == Decimal("1.1300")
    assert _stored(session) == [(DAY, Decimal("1.1298"))]


def test_newest_fx_date_is_the_latest_eur_usd_row(session: Session):
    before = newest_fx_date(session)
    repository.write_fx_rates(
        session,
        [FxRateRow(rate_date=DAY, rate=Decimal("1.1")), FxRateRow(rate_date=date(2033, 3, 4), rate=Decimal("1.2"))],
    )
    repository.write_fx_rates(
        session, [FxRateRow(rate_date=date(2033, 3, 9), quote_currency="GBP", rate=Decimal("0.8"))]
    )

    assert before is None or before < DAY
    assert newest_fx_date(session) == date(2033, 3, 4)  # the GBP row does not count
