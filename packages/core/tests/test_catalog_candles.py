"""covers: spec 0014 AC-1, AC-2, AC-4 (the candles tree: month files round trip, manifest written as JSON)."""

import json
from decimal import Decimal

import pytest
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity

from trading_core.catalog.candles import CandleTree
from trading_core.lab.candles import MINUTE_NS, CandleGap, CandleRow, FundingRow, Month, MonthManifest
from trading_core.lab.errors import CandleInputError
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
SEP = Month(2026, 9)
INSTRUMENT = CryptoPerpetual(
    instrument_id=InstrumentId.from_str("PF_XBTUSD.KRAKEN"),
    raw_symbol=Symbol("PF_XBTUSD"),
    base_currency=BTC,
    quote_currency=USD,
    settlement_currency=USD,
    is_inverse=False,
    price_precision=0,
    size_precision=4,
    price_increment=Price.from_str("1"),
    size_increment=Quantity.from_str("0.0001"),
    margin_init=D("0.02"),
    margin_maint=D("0.01"),
    maker_fee=D("0.0002"),
    taker_fee=D("0.0005"),
    ts_event=0,
    ts_init=0,
)


def test_bars_round_trip_with_the_quiet_flag(tmp_path):
    tree = CandleTree(tmp_path, INSTRUMENT)
    rows = [
        CandleRow(SEP.start_ns + MINUTE_NS, D(100), D(101), D(99), D(100), D("0.3566"), False),
        CandleRow(SEP.start_ns + 2 * MINUTE_NS, D(100), D(100), D(100), D(100), D(0), True),
    ]
    tree.write_bars(SEP, rows)
    assert tree.bar_file(SEP).name == "2026-09.parquet"
    assert "PF_XBTUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL" in str(tree.bar_file(SEP))
    assert tree.read_bars(SEP) == [
        CandleRow(r.ts_ns, r.open, r.high, r.low, r.close, r.volume.quantize(D("0.0001")), r.quiet) for r in rows
    ]


def test_an_empty_month_round_trips(tmp_path):
    tree = CandleTree(tmp_path, INSTRUMENT)
    tree.write_bars(SEP, [])
    assert tree.read_bars(SEP) == []


def test_funding_rows_keep_rate_period_and_source(tmp_path):
    tree = CandleTree(tmp_path, INSTRUMENT)
    rows = [
        FundingRow(SEP.start_ns, D("0.000012345678901234"), 4 * HOUR_NS, "export"),
        FundingRow(SEP.start_ns + HOUR_NS, D("-0.0000021"), HOUR_NS, "api"),
    ]
    tree.write_funding(SEP, rows)
    assert tree.read_funding(SEP) == rows
    assert tree.funding_months() == [SEP]
    assert tree.read_funding(Month(2026, 8)) == []


def test_gaps_and_manifest_round_trip(tmp_path):
    tree = CandleTree(tmp_path, INSTRUMENT)
    gaps = [CandleGap(SEP.start_ns, SEP.start_ns + 5 * MINUTE_NS, 5)]
    tree.write_gaps(SEP, gaps)
    assert tree.read_gaps(SEP) == gaps
    assert tree.read_manifest(SEP) is None
    manifest = MonthManifest("2026-09", 1, 0, 0, 0, 0, 0, [], 1, 2, 5, 1, 0, True, "2026-10-01T00:00:00Z")
    tree.write_manifest(manifest)
    assert tree.read_manifest(SEP) == manifest


def test_an_outdated_manifest_reads_as_none_and_a_broken_one_raises(tmp_path):
    tree = CandleTree(tmp_path, INSTRUMENT)
    tree.write_manifest(MonthManifest("2026-09", 1, 0, 0, 0, 0, 0, [], 1, 2, 5, 1, 0, True, "2026-10-01T00:00:00Z"))
    path = tree.manifest_file(SEP)
    data = json.loads(path.read_text())
    del data["price_rounded_rows"]
    path.write_text(json.dumps(data))
    assert tree.read_manifest(SEP) is None
    del data["bar_rows"]
    path.write_text(json.dumps(data))
    with pytest.raises(CandleInputError):
        tree.read_manifest(SEP)
