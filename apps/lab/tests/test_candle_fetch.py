"""covers: spec 0014 AC-1, AC-2, AC-2a, AC-3, AC-4, AC-5 (fetch-candles and import-funding, happy path and refusals);
spec 0022 AC-2 (``--instrument``: ETH's own symbol, tree, export member, and the unpinned id refusal)."""

import io
import json
import logging
import urllib.error
import zipfile
from dataclasses import replace
from decimal import Decimal
from email.message import Message
from urllib.parse import parse_qs, urlsplit

import pytest

from lab.candle_fetch import FUNDING_PATH, candle_tree, candles_path, fetch_candles
from lab.funding_import import import_funding
from lab.kraken_http import KrakenHttp
from lab.settings import LabSettings
from trading_core.lab.candles import FUNDING_HOURLY_FROM_NS, MINUTE_NS, Month
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS

from .conftest import lab_settings

D = Decimal
AUG, SEP, OCT = Month(2026, 8), Month(2026, 9), Month(2026, 10)
NOW = OCT.start_ns + 17 * 24 * HOUR_NS  # 2026-10-18, so August and September are complete
PAGE = 3
CANDLES_PATH = candles_path("PF_XBTUSD")


def _candle(month, minute, close="100", volume="1.5", high="101", low="99"):
    return {
        "time": (month.start_ns + minute * MINUTE_NS) // 1_000_000,
        "open": "100",
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _iso(ts_ns):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts_ns / SECOND_NS, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeKraken:
    """Serves candles in pages of ``PAGE`` and a funding body, recording every URL."""

    def __init__(self, candles, rates, symbol="PF_XBTUSD"):
        self.candles = candles
        self.rates = rates
        self.symbol = symbol
        self.urls = []

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        self.urls.append(url.path)
        query = {k: int(v[0]) if k != "symbol" else v[0] for k, v in parse_qs(url.query).items()}
        if url.path == FUNDING_PATH:
            assert query["symbol"] == self.symbol
            body = {"result": "success", "rates": self.rates}
        else:
            assert url.path == candles_path(self.symbol)
            inside = [c for c in self.candles if query["from"] * 1000 <= c["time"] < query["to"] * 1000]
            body = {"candles": inside[:PAGE], "more_candles": len(inside) > PAGE}
        return io.BytesIO(json.dumps(body).encode())


def _rate(stamp_ns, rel):
    return {"timestamp": _iso(stamp_ns), "fundingRate": 1, "relativeFundingRate": float(rel)}


@pytest.fixture
def settings(tmp_path):
    return lab_settings(tmp_path / "catalog", tmp_path / "reports")


def _run(settings, kraken, months, *, refetch=False, waits=None, clock=lambda: NOW, instrument_id=None):
    sleep = (waits if waits is not None else []).append
    http = KrakenHttp("https://kraken.test", 0, opener=kraken, sleep=sleep)
    coin = {} if instrument_id is None else {"instrument_id": instrument_id}
    return fetch_candles(settings, months, refetch=refetch, http=http, clock=clock, **coin)


class Flaky:
    """Fails the first requests with the given errors, then serves ``kraken``."""

    def __init__(self, kraken, errors):
        self.kraken = kraken
        self.errors = list(errors)
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        if self.errors:
            error = self.errors.pop(0)
            if isinstance(error, int):
                raise urllib.error.HTTPError(request.full_url, error, "nope", Message(), None)
            raise error
        return self.kraken(request, timeout)


def test_fetch_writes_bars_funding_gaps_and_manifest_then_skips(settings):
    candles = [_candle(SEP, m) for m in range(7)] + [_candle(SEP, 20, volume="0.3565999999999999")]
    candles.append(_candle(SEP, 21, volume="0"))  # volume 0 but not flat: a zero volume trade row
    rates = [_rate(SEP.start_ns + h * HOUR_NS, "0.0000125") for h in range(3)]
    kraken = FakeKraken(candles, rates)

    assert _run(settings, kraken, [SEP]) == 0

    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    manifest = tree.read_manifest(SEP)
    assert manifest is not None
    assert (manifest.bar_rows, manifest.volume_rounded_rows, manifest.zero_volume_trade_rows) == (9, 1, 1)
    assert manifest.complete
    assert manifest.first_ts_ns == SEP.start_ns + MINUTE_NS
    assert manifest.gap_count == 2  # minutes 7 to 19, then 22 to the month end
    # Charge time = stamp + 1 h; the API rate is stored as is.
    assert [r.charge_ns for r in tree.read_funding(SEP)] == [SEP.start_ns + h * HOUR_NS for h in (1, 2, 3)]
    assert {r.rate for r in tree.read_funding(SEP)} == {D("0.0000125")}
    assert manifest.funding_rows == 3
    assert manifest.funding_sources == ["api"]
    assert kraken.urls.count(CANDLES_PATH) == 3  # 9 candles in pages of 3

    again = FakeKraken(candles, rates)
    assert _run(settings, again, [SEP]) == 0
    assert again.urls == [FUNDING_PATH]  # the complete month is skipped


def test_the_running_month_is_not_complete(settings):
    assert _run(settings, FakeKraken([_candle(OCT, 0)], []), [OCT]) == 0
    manifest = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_manifest(OCT)
    assert manifest is not None
    assert not manifest.complete


def test_a_bad_volume_writes_no_month_and_keeps_the_ones_before(settings):
    candles = [_candle(AUG, 0), _candle(SEP, 0, volume="0.12345")]
    assert _run(settings, FakeKraken(candles, []), [AUG, SEP]) == 1
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    assert tree.read_manifest(AUG) is not None
    assert tree.read_manifest(SEP) is None
    assert not tree.bar_file(SEP).exists()


def test_a_failed_request_exits_1(settings):
    def broken(request, timeout):
        raise OSError("connection refused")

    waits: list[float] = []
    assert _run(settings, broken, [SEP], waits=waits) == 1
    assert waits == [1, 2, 4, 8, 16]  # a connection error is retried five times first


def _quiet(month, minute, price):
    return _candle(month, minute) | {"open": price, "high": price, "low": price, "close": price, "volume": "0"}


def test_429_and_503_are_retried_with_backoff(settings, caplog):
    caplog.set_level(logging.WARNING)
    kraken = Flaky(FakeKraken([_candle(SEP, 0)], []), [429, 503])
    waits: list[float] = []
    assert _run(settings, kraken, [SEP], waits=waits) == 0
    assert waits == [1, 2]
    retries = [r.fields for r in caplog.records if r.getMessage() == "lab_fetch_retry"]
    assert [(f["status"], f["attempt"], f["wait_s"]) for f in retries] == [(429, 1, 1), (503, 2, 2)]
    assert retries[0]["url"] == "https://kraken.test" + FUNDING_PATH  # no query string
    assert candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_manifest(SEP) is not None


def test_a_429_storm_exits_1_and_keeps_the_months_before(settings, caplog):
    caplog.set_level(logging.ERROR)
    calls = {"n": 0}
    kraken = FakeKraken([_candle(AUG, 0), _candle(SEP, 0)], [])

    def storm_after_august(request, timeout):
        calls["n"] += 1
        if calls["n"] > 2:  # funding, then August's one page
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", Message(), None)
        return kraken(request, timeout)

    waits: list[float] = []
    assert _run(settings, storm_after_august, [AUG, SEP], waits=waits) == 1
    assert waits == [1, 2, 4, 8, 16]
    [failed] = [r.fields for r in caplog.records if r.getMessage() == "lab_fetch_failed"]
    assert (failed["url"], failed["status"]) == ("https://kraken.test" + CANDLES_PATH, 429)
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    assert tree.read_manifest(AUG) is not None
    assert tree.read_manifest(SEP) is None


def test_another_4xx_or_a_bad_body_fails_without_a_retry(settings):
    waits: list[float] = []
    assert _run(settings, Flaky(FakeKraken([], []), [404]), [SEP], waits=waits) == 1
    assert waits == []

    def not_json(request, timeout):
        return io.BytesIO(b"<html>maintenance</html>")

    assert _run(settings, not_json, [SEP], waits=waits) == 1
    assert waits == []


def test_the_catalog_cap_exits_3_before_the_month_is_requested(settings):
    kraken = FakeKraken([_candle(SEP, 0)], [])
    small = settings.model_copy(update={"lab_catalog_cap_gb": D("0.05")})  # under one 0.1 GB month
    assert _run(small, kraken, [SEP]) == 3
    assert kraken.urls == [FUNDING_PATH]
    assert candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_manifest(SEP) is None


def test_refetch_fetches_a_complete_month_again(settings):
    candles = [_candle(SEP, m) for m in range(3)]
    assert _run(settings, FakeKraken(candles, []), [SEP]) == 0
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    first, rows = tree.read_manifest(SEP), tree.read_bars(SEP)

    later = FakeKraken(candles, [])
    assert _run(settings, later, [SEP], refetch=True, clock=lambda: NOW + HOUR_NS) == 0
    assert later.urls == [FUNDING_PATH, CANDLES_PATH]
    again = tree.read_manifest(SEP)
    assert first is not None
    assert again is not None
    assert again.fetched_at != first.fetched_at
    assert again == replace(first, fetched_at=again.fetched_at)
    assert tree.read_bars(SEP) == rows


def test_a_quiet_first_row_is_checked_against_the_previous_month(settings, caplog):
    caplog.set_level(logging.WARNING)
    assert _run(settings, FakeKraken([_quiet(SEP, 0, "100"), _candle(SEP, 1)], []), [SEP]) == 0
    assert [r.fields for r in caplog.records if r.getMessage() == "lab_candles_quiet_unchecked"] == [
        {"month": "2026-09"}
    ]

    # With August stored (last close 100), a quiet September start at 99 is a bad row.
    candles = [_candle(AUG, 0, close="100"), _quiet(SEP, 0, "99"), _candle(SEP, 1)]
    assert _run(settings, FakeKraken(candles, []), [AUG, SEP], refetch=True) == 1
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    assert tree.read_manifest(SEP) is not None  # the committed September stays as it was
    assert tree.read_bars(SEP)[0].open == D(100)


def test_a_refetch_that_breaks_the_next_months_quiet_rows_exits_1_but_commits(settings, caplog):
    assert _run(settings, FakeKraken([_candle(AUG, 0, close="100"), _quiet(SEP, 0, "100")], []), [AUG, SEP]) == 0

    caplog.set_level(logging.ERROR)
    moved = FakeKraken([_candle(AUG, 0) | {"high": "102", "close": "101"}], [])
    assert _run(settings, moved, [AUG], refetch=True) == 1
    [bad] = [r.fields for r in caplog.records if r.getMessage() == "lab_candles_bad_rows"]
    assert bad["scope"] == "2026-09"
    assert [row["field"] for row in bad["rows"]] == ["quiet"]
    assert candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_bars(AUG)[-1].close == D(
        101
    )  # August still committed


def test_main_takes_refetch():
    from lab.main import parser

    assert parser().parse_args(["fetch-candles", "2026-09", "2026-09", "--refetch"]).refetch
    assert not parser().parse_args(["fetch-candles", "2026-09", "2026-09"]).refetch


def _export(tmp_path, rows, header="timestamp,tradeable,absolute_rate,relative_rate", member="exports/PF_XBTUSD.csv"):
    path = tmp_path / "export.zip"
    text = "\n".join([header, *rows]) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, text)
    return path


def _stamp(ns):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ns / SECOND_NS, UTC).strftime("%Y-%m-%d %H:%M:%S")


def test_import_merges_four_hour_and_hourly_rows_and_twice_adds_nothing(settings, tmp_path, caplog):
    four_h = FUNDING_HOURLY_FROM_NS - 4 * HOUR_NS
    rows = [
        f"{_stamp(four_h)},PF_XBTUSD,0.5,0.00002",
        f"{_stamp(FUNDING_HOURLY_FROM_NS)},PF_XBTUSD,0.1,0.000005",
    ]
    path = _export(tmp_path, rows)

    assert import_funding(settings, path) == 0
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    stored = tree.read_funding(Month(2022, 9))
    # The 4 h row is charged 4 h after its stamp, at its own rate (gate G-4: no x 4).
    assert [(r.charge_ns, r.rate, r.source) for r in stored] == [
        (FUNDING_HOURLY_FROM_NS, D("0.00002"), "export"),
        (FUNDING_HOURLY_FROM_NS + HOUR_NS, D("0.000005"), "export"),
    ]
    assert stored[0].next_funding_ns == FUNDING_HOURLY_FROM_NS + 4 * HOUR_NS

    caplog.clear()
    caplog.set_level(logging.INFO)
    assert import_funding(settings, path) == 0
    [record] = [r for r in caplog.records if r.getMessage() == "lab_funding_imported"]
    assert record.fields == {"rows_added": 0, "rows_present": 2, "months_touched": 0}


def test_import_refuses_a_bad_file_before_writing(settings, tmp_path):
    assert import_funding(settings, tmp_path / "missing.zip") == 64
    assert import_funding(settings, _export(tmp_path, [], member="exports/PF_ETHUSD.csv")) == 64
    assert import_funding(settings, _export(tmp_path, [], header="timestamp,tradeable,rate")) == 64
    other = _export(tmp_path, ["2026-09-01 00:00:00,PF_ETHUSD,0,0.0001"])
    assert import_funding(settings, other) == 64
    assert not (settings.lab_catalog_dir / "candles").exists()


def test_import_bad_rows_and_disagreements_exit_1_with_nothing_written(settings, tmp_path):
    off_boundary = _export(tmp_path, ["2022-05-01 01:00:00,PF_XBTUSD,0,0.0001"])  # 4 h era, not on a 4 h boundary
    assert import_funding(settings, off_boundary) == 1
    assert not (settings.lab_catalog_dir / "candles").exists()

    assert import_funding(settings, _export(tmp_path, ["2026-09-01 00:00:00,PF_XBTUSD,0,0.0001"])) == 0
    assert import_funding(settings, _export(tmp_path, ["2026-09-01 00:00:00,PF_XBTUSD,0,0.0002"])) == 1
    assert [r.rate for r in candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_funding(SEP)] == [
        D("0.0001")
    ]


def test_the_kraken_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        LabSettings(trading_env="demo", lab_kraken_futures_url="http://futures.kraken.com")
    assert LabSettings(trading_env="demo").lab_kraken_futures_url == "https://futures.kraken.com"


def test_off_tick_extremes_commit_rounded_and_are_logged(settings, caplog):
    caplog.set_level(logging.INFO)
    candles = [_candle(SEP, 0, high="101.7"), _candle(SEP, 1, low="98.99999999999"), _candle(SEP, 2)]
    assert _run(settings, FakeKraken(candles, []), [SEP]) == 0
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    manifest = tree.read_manifest(SEP)
    assert manifest is not None
    assert manifest.price_rounded_rows == 2
    assert [r.high for r in tree.read_bars(SEP)] == [D(101)] * 3
    [fields] = [r.fields for r in caplog.records if r.getMessage() == "lab_candles_price_rounded"]
    assert fields == {
        "month": "2026-09",
        "price_rounded_rows": 2,
        "roundings": [{"minute": "2026-09-01T00:00:00Z", "field": "high", "raw": "101.7", "stored": "101"}],
    }


def test_a_month_with_an_outdated_manifest_is_fetched_again_without_refetch(settings):
    candles = [_candle(SEP, m) for m in range(3)]
    assert _run(settings, FakeKraken(candles, []), [SEP]) == 0
    tree = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    path = tree.manifest_file(SEP)
    data = json.loads(path.read_text())
    del data["price_rounded_rows"]
    path.write_text(json.dumps(data))

    again = FakeKraken(candles, [])
    assert _run(settings, again, [SEP]) == 0
    assert again.urls == [FUNDING_PATH, CANDLES_PATH]
    manifest = tree.read_manifest(SEP)
    assert manifest is not None
    assert manifest.price_rounded_rows == 0


ETH = "PF_ETHUSD.KRAKEN"


def test_an_eth_fetch_uses_its_own_symbol_tick_and_tree_and_leaves_btc_alone(settings):
    candles = [
        _candle(SEP, 0, close="2500.20000000001", high="2501.04", low="2499.96") | {"open": "2500"},
        _candle(SEP, 1, close="2500.3", high="2500.5", low="2500.1") | {"open": "2500.2"},
    ]
    rates = [_rate(SEP.start_ns, "0.00001")]
    kraken = FakeKraken(candles, rates, symbol="PF_ETHUSD")

    assert _run(settings, kraken, [SEP], instrument_id=ETH) == 0

    assert kraken.urls == [FUNDING_PATH, candles_path("PF_ETHUSD")]
    eth = candle_tree(settings.lab_catalog_dir, ETH)
    bars = eth.read_bars(SEP)
    # ETH's pinned tick is 0.1: a close within noise snaps to it, an off tick high rounds down
    # and an off tick low up (spec 0014 AC-1).
    assert [(b.close, b.high, b.low) for b in bars] == [
        (D("2500.2"), D("2501.0"), D("2500.0")),
        (D("2500.3"), D("2500.5"), D("2500.1")),
    ]
    assert eth.read_manifest(SEP) is not None
    assert [r.rate for r in eth.read_funding(SEP)] == [D("0.00001")]
    btc = candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN")
    assert btc.read_manifest(SEP) is None
    assert btc.read_funding(SEP) == []


def test_an_eth_import_reads_only_its_member_and_tradeable(settings, tmp_path):
    rows = ["2026-09-01 00:00:00,PF_ETHUSD,0,0.00003"]
    path = _export(tmp_path, rows, member="exports/PF_ETHUSD.csv")

    assert import_funding(settings, path) == 64  # the BTC default looks for exports/PF_XBTUSD.csv
    assert import_funding(settings, path, ETH) == 0
    assert [r.rate for r in candle_tree(settings.lab_catalog_dir, ETH).read_funding(SEP)] == [D("0.00003")]
    assert candle_tree(settings.lab_catalog_dir, "PF_XBTUSD.KRAKEN").read_funding(SEP) == []

    btc_rows = _export(tmp_path, ["2026-09-01 00:00:00,PF_XBTUSD,0,0.0001"], member="exports/PF_ETHUSD.csv")
    assert import_funding(settings, btc_rows, ETH) == 64


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch-candles", "2026-09", "2026-09", "--instrument", "PF_DOGEUSD.KRAKEN"],
        ["import-funding", "/nonexistent/export.zip", "--instrument", "PF_DOGEUSD.KRAKEN"],
        ["import-funding", "/nonexistent/export.zip", "--instrument", "../runs/donchian_4h"],
    ],
    ids=["fetch-candles", "import-funding", "a path"],
)
def test_an_unpinned_instrument_exits_64_before_booting(monkeypatch, capsys, argv):
    from lab.main import main

    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setattr("lab.main.boot", lambda *a, **k: pytest.fail("booted"))
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    assert exit_.value.code == 64
    assert "no pinned instrument" in capsys.readouterr().err


def test_the_instrument_flag_defaults_to_btc():
    from lab.main import parser

    assert parser().parse_args(["fetch-candles", "2026-09", "2026-09"]).instrument == "PF_XBTUSD.KRAKEN"
    assert parser().parse_args(["import-funding", "x.zip"]).instrument == "PF_XBTUSD.KRAKEN"
    assert parser().parse_args(["import-funding", "x.zip", "--instrument", ETH]).instrument == ETH
