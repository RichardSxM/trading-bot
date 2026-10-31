"""covers: spec 0024 AC-15 (the hourly ECB fetch: from the day after the newest rate or ``FX_HISTORY_FROM``, 404 as
zero rows, failures logged and left for the next hour, a differing rate kept) and AC-18 (the HTTP client is injected,
no test reaches the network), AC-17 (each step returns ``fx_rates_stale`` from the newest date after it), plus the
three ``FX_*`` settings."""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from alerter.fx import TIMEOUT_S, FxFetcher, amsterdam_today
from alerter.settings import AlerterSettings
from trading_core.alerts.model import CheckResult
from trading_core.recording.errors import RecordConflictError
from trading_core.recording.rows import FxRateRow

from .conftest import Clock

BASE = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
# Two lines of a real Data Portal csvdata answer, cut to the columns the parser and a reader need.
BODY = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\r\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,1.1298,A\r\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-02,1.1225,A\r\n"
)
# 2026-10-02 10:00 UTC, a Friday, before that day's rate is published.
FRIDAY = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


class FakeFxStore:
    """An in memory ``FxStore`` that compares like ``write_fx_rates``."""

    def __init__(self, rates: dict[date, Decimal] | None = None, stale_newest: date | None = None) -> None:
        self.rates = dict(rates or {})
        self.stale_newest = stale_newest  # answered instead of the real newest date, when set
        self.writes = 0
        self.written: list[FxRateRow] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise OperationalError("connect", {}, Exception("postgres is down"))

    def newest(self) -> date | None:
        self._check()
        if self.stale_newest is not None:
            return self.stale_newest
        return max(self.rates, default=None)

    def write(self, rows):
        self._check()
        self.writes += 1
        self.written.extend(rows)
        for row in rows:
            stored = self.rates.get(row.rate_date)
            if stored is not None and stored != row.rate:
                key = {"rate_date": row.rate_date, "base_currency": "EUR", "quote_currency": "USD"}
                raise RecordConflictError("fx_rates", key, {"rate": stored}, {"rate": row.rate})
        new = [row for row in rows if row.rate_date not in self.rates]
        self.rates.update({row.rate_date: row.rate for row in rows})
        return len(new)


class Ecb:
    """Answers every request with ``status`` and ``body`` and records the requests."""

    def __init__(self, status: int = 200, body: str = BODY, error: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, text=self.body)


def _step(
    ecb: Ecb, store: FakeFxStore, at: datetime = FRIDAY, history_from: date = date(2026, 10, 1)
) -> CheckResult | None:
    async def run() -> CheckResult | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(ecb)) as http:
            return await FxFetcher(http, store, BASE, history_from, Clock(at), stale_days=6).step()

    return asyncio.run(run())


def _codes(caplog) -> list[str]:
    return [r.fields["code"] for r in caplog.records if "fields" in r.__dict__]


def _record(caplog, code: str):
    [record] = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == code]
    return record


def test_an_empty_table_backfills_from_fx_history_from_up_to_today(caplog):
    caplog.set_level("INFO")
    ecb, store = Ecb(), FakeFxStore()

    _step(ecb, store)

    [request] = ecb.requests
    assert request.url.params["startPeriod"] == "2026-10-01"
    assert request.url.params["endPeriod"] == "2026-10-02"
    assert request.url.params["format"] == "csvdata"
    assert str(request.url).startswith(BASE + "?")
    assert store.rates == {date(2026, 10, 1): Decimal("1.1298"), date(2026, 10, 2): Decimal("1.1225")}
    fetched = _record(caplog, "fx_fetched")
    assert (fetched.fields["count"], fetched.fields["newest"]) == (2, "2026-10-02")


def test_the_next_fetch_starts_the_day_after_the_newest_rate():
    ecb = Ecb(404, "")
    store = FakeFxStore({date(2026, 9, 28): Decimal("1.1")})

    _step(ecb, store)

    [request] = ecb.requests
    assert request.url.params["startPeriod"] == "2026-09-29"


def test_nothing_is_fetched_once_todays_rate_is_stored():
    ecb = Ecb()

    _step(ecb, FakeFxStore({date(2026, 10, 2): Decimal("1.1225")}))

    assert ecb.requests == []


def test_today_is_the_amsterdam_date_not_the_utc_one():
    # 22:30 UTC on 1 October is 00:30 on 2 October in Amsterdam (CEST, UTC+2).
    late = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)
    ecb = Ecb(404, "")

    _step(ecb, FakeFxStore({date(2026, 10, 1): Decimal("1.1298")}), at=late)

    assert amsterdam_today(late) == date(2026, 10, 2)
    [request] = ecb.requests
    assert (request.url.params["startPeriod"], request.url.params["endPeriod"]) == ("2026-10-02", "2026-10-02")


def test_a_404_is_zero_rows_written_and_nothing_logged(caplog):
    caplog.set_level("INFO")
    store = FakeFxStore({date(2026, 10, 1): Decimal("1.1298")})

    _step(Ecb(404, "No results found."), store)

    assert store.writes == 0
    assert _codes(caplog) == []


def test_fetching_days_already_stored_with_equal_rates_is_a_no_op(caplog):
    caplog.set_level("INFO")
    store = FakeFxStore()
    _step(Ecb(), store)
    store.stale_newest = date(2026, 9, 30)  # a stale read, so the fetch covers the stored days

    _step(Ecb(), store)

    assert store.rates == {date(2026, 10, 1): Decimal("1.1298"), date(2026, 10, 2): Decimal("1.1225")}
    assert [r.fields["count"] for r in caplog.records if r.__dict__.get("fields", {}).get("code") == "fx_fetched"] == [
        2,
        0,
    ]


@pytest.mark.parametrize(
    ("ecb", "reason"),
    [
        (Ecb(500, "oops"), "status"),
        (Ecb(error=httpx.ReadTimeout("slow")), "http"),
        (Ecb(error=httpx.ConnectError("no route")), "http"),
        (Ecb(200, "TIME_PERIOD,OBS_VALUE\r\n2026-10-01,1.1298\r\n2026-10-02,NaN\r\n"), "parse"),
        (Ecb(200, "<html>maintenance</html>"), "parse"),
        (Ecb(200, "TIME_PERIOD,OBS_VALUE\r\n2026-11-01,1.1298\r\n"), "parse"),
    ],
    ids=["500", "timeout", "connect", "bad rate", "html", "date outside"],
)
def test_a_failed_fetch_writes_nothing_and_logs_why(caplog, ecb, reason):
    caplog.set_level("INFO")
    store = FakeFxStore()

    _step(ecb, store)

    assert store.writes == 0
    assert store.rates == {}
    record = _record(caplog, "fx_fetch_failed")
    assert record.levelname == "WARNING"
    assert record.fields["reason"] == reason
    assert "fx_fetched" not in _codes(caplog)


def test_a_failed_fetch_simply_tries_again_next_hour():
    store = FakeFxStore()
    _step(Ecb(503, "busy"), store)

    _step(Ecb(), store)

    assert date(2026, 10, 2) in store.rates


def test_a_differing_rate_keeps_the_stored_one_and_logs_an_error(caplog):
    caplog.set_level("INFO")
    # A stale read, so the fetch covers the stored day.
    store = FakeFxStore({date(2026, 10, 2): Decimal("1.2000")}, stale_newest=date(2026, 9, 30))

    _step(Ecb(), store)

    assert store.rates[date(2026, 10, 2)] == Decimal("1.2000")
    conflict = _record(caplog, "fx_rate_conflict")
    assert conflict.levelname == "ERROR"
    assert conflict.fields == {
        "code": "fx_rate_conflict",
        "rate_date": "2026-10-02",
        "stored_rate": "1.2000",
        "fetched_rate": "1.1225",
    }
    assert "fx_fetched" not in _codes(caplog)


def test_postgres_down_logs_a_failure_and_fetches_nothing(caplog):
    caplog.set_level("INFO")
    ecb, store = Ecb(), FakeFxStore()
    store.down = True

    _step(ecb, store)

    assert ecb.requests == []
    assert _record(caplog, "fx_fetch_failed").fields["reason"] == "database"


def test_the_request_times_out_after_20_s():
    seen: list[dict[str, float]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(404)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            await FxFetcher(http, FakeFxStore(), BASE, date(2026, 10, 1), Clock(FRIDAY)).step()

    asyncio.run(run())

    assert TIMEOUT_S == 20.0
    assert seen[0]["read"] == 20.0


def test_fx_settings_default_to_the_ecb_and_2026(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")

    settings = AlerterSettings()

    assert settings.fx_ecb_url == BASE
    assert settings.fx_history_from == date(2026, 1, 1)
    assert settings.fx_stale_days == 6


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FX_ECB_URL", "http://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"),
        ("FX_ECB_URL", "https://data-api.ecb.europa.eu/x?format=csvdata"),
        ("FX_ECB_URL", "not a url"),
        ("FX_HISTORY_FROM", "2999-01-01"),
        ("FX_HISTORY_FROM", "January"),
        ("FX_STALE_DAYS", "0"),
        ("FX_STALE_DAYS", "15"),
    ],
)
def test_bad_fx_settings_refuse(monkeypatch, name, value):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        AlerterSettings()


def test_every_row_is_a_decimal_eur_usd_ecb_row():
    store = FakeFxStore()

    _step(Ecb(), store)

    assert {(r.base_currency, r.quote_currency, r.source) for r in store.written} == {("EUR", "USD", "ECB")}
    assert all(isinstance(r.rate, Decimal) for r in store.written)


def test_a_step_judges_staleness_from_the_rows_it_just_stored():
    result = _step(Ecb(), FakeFxStore(), at=datetime(2026, 10, 9, 10, 0, tzinfo=UTC))

    assert result is not None
    assert (result.rule, result.ok, result.fields["newest"], result.fields["age_days"]) == (
        "fx_rates_stale",
        False,
        "2026-10-02",
        7,
    )


def test_a_failed_fetch_still_judges_the_stored_rows():
    store = FakeFxStore({date(2026, 9, 30): Decimal("1.1")})

    result = _step(Ecb(status=500), store)

    assert result is not None
    assert (result.ok, result.fields["newest"], result.fields["age_days"]) == (True, "2026-09-30", 2)


def test_an_empty_table_after_a_404_is_stale():
    result = _step(Ecb(status=404), FakeFxStore())

    assert result is not None
    assert (result.ok, result.fields.get("missing")) == (False, True)


def test_postgres_down_gives_no_verdict():
    store = FakeFxStore()
    store.down = True

    assert _step(Ecb(), store) is None
