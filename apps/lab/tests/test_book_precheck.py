"""covers: spec 0033 AC-12, AC-12a, AC-12b, the pre check runner: the pre registration file is checked with every
problem listed, a name in the ledger is refused, the ledger round trips, the Binance klines are parsed and paged,
and the report carries the verdicts and the AC-12a label.
"""

import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lab.binance_fetch import (
    BadKlineError,
    binance_file,
    fetch_symbol,
    load_binance_coin,
    parse_klines,
    read_closes,
    write_closes,
)
from lab.book_precheck import (
    PrecheckFile,
    PrecheckFileError,
    Row,
    TrialSpec,
    jobs_for,
    ledger_text,
    load_ledger,
    load_precheck,
    render,
    scale_label,
)
from lab.kraken_http import KrakenHttp
from trading_core.lab.precheck import Verdict

GOOD = """
name = "trend_vol_precheck"
from = 2025-05-24
to = 2026-11-16
midpoint = 2026-02-19
cost = "0.0007"
equity = "10000"
baseline_cut_hours = [0, 6, 12, 18]
pass_rule = "as spec 0033 AC-12 and AC-12a"

[binance]
from = 2018-01-01
to = 2022-03-31

[[trial]]
name = "ewma20"
family = "ewma"
role = "main"
half_life = 20
binance = true

[[trial]]
name = "tranche4_off01"
family = "tranche"
role = "offset"
tranches = 4
cut_hour = 1

[[trial]]
name = "scale3_stop8@06"
family = "scale"
role = "main"
cut_hour = 6
ramp_days = 3
stop_vols = "8"

[[trial]]
name = "scale3_nostop"
family = "scale"
role = "control"
ramp_days = 3
binance = true
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pre.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_good_file_loads(tmp_path):
    pre = load_precheck(write(tmp_path, GOOD))
    assert (pre.start, pre.end, pre.midpoint) == (date(2025, 5, 24), date(2026, 11, 16), date(2026, 2, 19))
    assert pre.baseline_hours == (0, 6, 12, 18)
    scale = pre.trials[2]
    assert (scale.cut_hour, scale.ramp_days, scale.stop_vols) == (6, 3, Decimal(8))
    assert pre.trials[3].stop_vols is None
    assert pre.names()[:2] == ["trend_vol_precheck", "baseline@00"]


def test_every_problem_is_listed(tmp_path):
    bad = GOOD.replace('family = "ewma"', 'family = "turtle"').replace("midpoint = 2026-02-19", "midpoint = 2027-01-01")
    bad = bad.replace("tranches = 4\ncut_hour = 1", "tranches = 4\ncut_hour = 7").replace("half_life = 20\n", "")
    with pytest.raises(PrecheckFileError) as info:
        load_precheck(write(tmp_path, bad))
    text = " ".join(info.value.problems)
    assert "family must be one of" in text
    assert "midpoint" in text
    assert "passes 23:00" in text


def test_duplicate_names_and_unknown_keys_refused(tmp_path):
    bad = GOOD.replace('name = "scale3_nostop"', 'name = "ewma20"').replace(
        "binance = true\n\n[[trial]]", "speed = 1\n\n[[trial]]", 1
    )
    with pytest.raises(PrecheckFileError) as info:
        load_precheck(write(tmp_path, bad))
    text = " ".join(info.value.problems)
    assert "listed twice" in text
    assert "unknown keys speed" in text


def test_ledger_round_trip_and_bad_ledger(tmp_path):
    path = tmp_path / "ledger.toml"
    path.write_text(ledger_text(["a", "b@00"]), encoding="utf-8")
    assert load_ledger(path) == ["a", "b@00"]
    path.write_text("names = 3\n", encoding="utf-8")
    with pytest.raises(PrecheckFileError):
        load_ledger(path)


def test_jobs_cover_kraken_and_the_flagged_binance_runs(tmp_path):
    pre = load_precheck(write(tmp_path, GOOD))
    jobs = jobs_for(pre)
    kraken = [j.name for j in jobs if j.venue == "kraken"]
    binance = [j.name for j in jobs if j.venue == "binance"]
    assert kraken == ["baseline@00", "baseline@06", "baseline@12", "baseline@18", *(t.name for t in pre.trials)]
    assert binance == ["baseline@00", "ewma20", "scale3_nostop"]
    assert all(j.cut_hour == 0 for j in jobs if j.venue == "binance")


def row(name, venue="kraken", role="main", family="scale", hour=0, full="1"):
    one = Decimal(1)
    return Row(
        name,
        venue,
        family,
        role,
        hour,
        one,
        one,
        Decimal(full),
        one,
        Decimal("0.2"),
        one,
        Decimal("0.05"),
        Decimal(0),
        0,
        3,
        10,
        0,
        False,
    )


def test_scale_label():
    passed = [Verdict("scale", True, ())]
    binance = [row("baseline@00", "binance", "baseline", "", full="1"), row("scale3_stop8@00", "binance", full="1.2")]
    assert scale_label(passed, binance, "scale3_stop8@00") == "seen"
    binance[1] = row("scale3_stop8@00", "binance", full="0.8")
    assert scale_label(passed, binance, "scale3_stop8@00") == "seen, fragile"
    assert scale_label([Verdict("scale", False, ())], binance, "scale3_stop8@00") is None


def test_render_holds_the_rule_tables_and_verdicts():
    pre = PrecheckFile(
        "p",
        date(2025, 5, 24),
        date(2026, 11, 16),
        date(2026, 2, 19),
        Decimal("0.0007"),
        Decimal(10000),
        (0,),
        date(2018, 1, 1),
        date(2022, 3, 31),
        "the rule",
        (TrialSpec("ewma20", "ewma", "main"),),
    )
    rows = [
        row("baseline@00", role="baseline", family=""),
        row("ewma20", family="ewma"),
        row("baseline@00", "binance", "baseline", ""),
    ]
    text = render(pre, rows, [Verdict("ewma", False, ("ewma20 beats the reference: FAIL",))], "abc123")
    assert "the rule" in text
    assert "### ewma: FAIL" in text
    assert "`abc123`" in text
    assert "Binance information runs" in text


def kline(open_ms, close):
    return [open_ms, "1", "1", "1", close, "1", open_ms + 3_599_999]


H = 3_600_000
START = 1514764800000  # 2018-01-01


class FakeBinance:
    """Serves klines in pages of 2 from the hour list."""

    def __init__(self, hours):
        self.hours = hours
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.full_url).query).items()}
        start, end = int(query["startTime"]), int(query["endTime"])
        inside = [kline(t, c) for t, c in self.hours if start <= t <= end]
        return io.BytesIO(json.dumps(inside[:2]).encode())


def test_fetch_pages_and_keys_by_hour_end():
    hours = [(START + i * H, str(100 + i)) for i in range(5)]
    fake = FakeBinance(hours)
    http = KrakenHttp("https://binance.test", 0, opener=fake, sleep=lambda s: None)
    closes = fetch_symbol(http, "BTCUSDT", date(2018, 1, 1), date(2018, 1, 1))
    assert [c for _, c in closes] == [Decimal(100 + i) for i in range(5)]
    assert closes[0][0] == (START + H) * 1_000_000
    assert fake.calls == 4  # three full or partial pages, then an empty one


def test_parse_refuses_bad_rows():
    with pytest.raises(BadKlineError, match="off the hour"):
        parse_klines([kline(START + 1, "1")], START)
    with pytest.raises(BadKlineError, match="above 0"):
        parse_klines([kline(START, "0")], START)
    with pytest.raises(BadKlineError, match="out of order"):
        parse_klines([kline(START + H, "1"), kline(START, "1")], START)


def test_files_round_trip_into_coin_data(tmp_path):
    coin = "PF_XBTUSD.KRAKEN"
    closes = [((START + H) * 1_000_000, Decimal("13500.5"))]
    write_closes(binance_file(tmp_path, coin), "BTCUSDT", closes)
    assert read_closes(binance_file(tmp_path, coin)) == dict(closes)
    data = load_binance_coin(tmp_path, coin)
    assert data.hourly == data.minutes == dict(closes)
    assert data.funding == {}
