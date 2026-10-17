"""covers: spec 0014 AC-1, AC-2, AC-2a, AC-3, AC-4, AC-5 (fetch-candles and import-funding, happy path and refusals)."""

import io
import json
import logging
import zipfile
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from lab.candle_fetch import CANDLES_PATH, FUNDING_PATH, candle_tree, fetch_candles
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


def _candle(month, minute, close="100", volume="1.5"):
    return {
        "time": (month.start_ns + minute * MINUTE_NS) // 1_000_000,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": close,
        "volume": volume,
    }


def _iso(ts_ns):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts_ns / SECOND_NS, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeKraken:
    """Serves candles in pages of ``PAGE`` and a funding body, recording every URL."""

    def __init__(self, candles, rates):
        self.candles = candles
        self.rates = rates
        self.urls = []

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        self.urls.append(url.path)
        query = {k: int(v[0]) if k != "symbol" else v[0] for k, v in parse_qs(url.query).items()}
        if url.path == FUNDING_PATH:
            body = {"result": "success", "rates": self.rates}
        else:
            assert url.path == CANDLES_PATH
            inside = [c for c in self.candles if query["from"] * 1000 <= c["time"] < query["to"] * 1000]
            body = {"candles": inside[:PAGE], "more_candles": len(inside) > PAGE}
        return io.BytesIO(json.dumps(body).encode())


def _rate(stamp_ns, rel):
    return {"timestamp": _iso(stamp_ns), "fundingRate": 1, "relativeFundingRate": float(rel)}


@pytest.fixture
def settings(tmp_path):
    return lab_settings(tmp_path / "catalog", tmp_path / "reports")


def _run(settings, kraken, months):
    http = KrakenHttp("https://kraken.test", 0, opener=kraken)
    return fetch_candles(settings, months, http=http, clock=lambda: NOW)


def test_fetch_writes_bars_funding_gaps_and_manifest_then_skips(settings):
    candles = [_candle(SEP, m) for m in range(7)] + [_candle(SEP, 20, volume="0.3565999999999999")]
    candles.append(_candle(SEP, 21, volume="0"))  # volume 0 but not flat: a zero volume trade row
    rates = [_rate(SEP.start_ns + h * HOUR_NS, "0.0000125") for h in range(3)]
    kraken = FakeKraken(candles, rates)

    assert _run(settings, kraken, [SEP]) == 0

    tree = candle_tree(settings.lab_catalog_dir)
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
    manifest = candle_tree(settings.lab_catalog_dir).read_manifest(OCT)
    assert manifest is not None
    assert not manifest.complete


def test_a_bad_volume_writes_no_month_and_keeps_the_ones_before(settings):
    candles = [_candle(AUG, 0), _candle(SEP, 0, volume="0.12345")]
    assert _run(settings, FakeKraken(candles, []), [AUG, SEP]) == 1
    tree = candle_tree(settings.lab_catalog_dir)
    assert tree.read_manifest(AUG) is not None
    assert tree.read_manifest(SEP) is None
    assert not tree.bar_file(SEP).exists()


def test_a_failed_request_exits_1(settings):
    def broken(request, timeout):
        raise OSError("connection refused")

    assert _run(settings, broken, [SEP]) == 1


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
    tree = candle_tree(settings.lab_catalog_dir)
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
    assert [r.rate for r in candle_tree(settings.lab_catalog_dir).read_funding(SEP)] == [D("0.0001")]


def test_the_kraken_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        LabSettings(trading_env="demo", lab_kraken_futures_url="http://futures.kraken.com")
    assert LabSettings(trading_env="demo").lab_kraken_futures_url == "https://futures.kraken.com"
