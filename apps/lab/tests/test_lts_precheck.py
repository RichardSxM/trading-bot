"""covers: spec 0031 AC-12 (depth from the runner's book), AC-14 (a cut scan), AC-15 (markouts), AC-18 (forced
variant and matching), AC-19 (byte identical reports, the file name)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model.data import (
    BookOrder,
    CustomData,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import AggressorSide as NautilusSide
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from lab.lts_precheck import (
    ALL_TRADES,
    LIQUIDATIONS_REMOVED,
    DayScan,
    build_report,
    report_path,
    run_precheck,
    scan_events,
)
from lab.lts_runfile import load_lts_run
from trading_core.marketdata.context import AggressorSide, LiquidationKind
from trading_core.node.context_types import LiquidationTick, context_data_type
from trading_core.strategy.lts_sweep import LtsParams

from .conftest import lab_settings

IID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
FROZEN = Path(__file__).parents[1] / "runs" / "lts_precheck.toml"
SEC = 1_000_000_000
MS = 1_000_000
F_LAST = 128
WARM_S = 3700
W = WARM_S * SEC
PARAMS = LtsParams(Decimal(1), 60, 1, 1, 5, 3000, 90, 15)
_ids = iter(range(10**9))


def _px(value):
    return Price.from_str(str(value))


def _qty(value):
    return Quantity.from_str(f"{value}.0000")


def _delta(row, ts, flags=0):
    action, side, price, size = row
    return OrderBookDelta(IID, action, BookOrder(side, _px(price), _qty(size), 0), flags, 0, ts, ts)


def _batch(ts, *rows):
    built = [_delta(row, ts) for row in rows[:-1]]
    built.append(_delta(rows[-1], ts, F_LAST))
    return OrderBookDeltas(IID, built)


def _quote(ts, bid, ask):
    return QuoteTick(IID, _px(bid), _px(ask), _qty(1), _qty(1), ts, ts)


def _trade(ts, price, side, size=1, trade_id=None):
    return TradeTick(IID, _px(price), _qty(size), side, TradeId(trade_id or f"t{next(_ids)}"), ts, ts)


def _warm():
    events = [
        _batch(0, (BookAction.ADD, OrderSide.BUY, 99_999, 5), (BookAction.ADD, OrderSide.SELL, 100_001, 5)),
        _quote(0, 99_999, 100_001),
    ]
    for second in range(WARM_S):
        size = second % 100 + 1
        events.append(_trade(second * SEC + 10 * MS, 100_000, NautilusSide.SELLER, size))
        events.append(_trade(second * SEC + 20 * MS, 100_000, NautilusSide.BUYER, size))
    return events


def _tail(start_ms=0, big_id="big", big_size=500):
    """A failed breakdown: depth falls from 5 to 1, part comes back, the mid crosses back above the level."""
    at = W + start_ms * MS
    return [
        _trade(at + 100 * MS, 99_999, NautilusSide.SELLER, big_size, big_id),
        _batch(at + 200 * MS, (BookAction.UPDATE, OrderSide.BUY, 99_999, 1)),
        _batch(
            at + 300 * MS,
            (BookAction.ADD, OrderSide.BUY, 100_000, 2),
            (BookAction.DELETE, OrderSide.SELL, 100_001, 5),
            (BookAction.ADD, OrderSide.SELL, 100_002, 5),
        ),
        _quote(at + 300 * MS, 100_000, 100_002),
    ]


def _after_signal():
    """Mids after the signal at W + 300 ms: the 5 s mark sees 100001, the 15 s mark 100011, and 30 s is past the end."""
    signal = W + 300 * MS
    at_6 = signal + 6 * SEC
    at_20 = signal + 20 * SEC
    return [
        _batch(at_6, (BookAction.UPDATE, OrderSide.SELL, 100_002, 6)),
        _batch(
            at_6 + 1,
            (BookAction.DELETE, OrderSide.BUY, 100_000, 2),
            (BookAction.ADD, OrderSide.BUY, 100_010, 2),
            (BookAction.DELETE, OrderSide.SELL, 100_002, 6),
            (BookAction.ADD, OrderSide.SELL, 100_012, 5),
        ),
        _quote(at_6 + 1, 100_010, 100_012),
        _batch(
            at_20,
            (BookAction.DELETE, OrderSide.BUY, 100_010, 2),
            (BookAction.ADD, OrderSide.BUY, 100_020, 2),
            (BookAction.DELETE, OrderSide.SELL, 100_012, 5),
            (BookAction.ADD, OrderSide.SELL, 100_022, 5),
        ),
        _quote(at_20, 100_020, 100_022),
    ]


def _scan(events, forced=False):
    return scan_events(
        events,
        params=PARAMS,
        instrument_id=IID.value,
        contract_size=Decimal(1),
        band_bps=5,
        with_forced_variant=forced,
    )


def test_depth_comes_from_the_book_the_runner_built():
    rows = _scan(_warm() + _tail()).variants[ALL_TRADES].signals

    assert len(rows) == 1
    row = rows[0]
    assert (row.depth_before, row.depth_min, row.depth_after) == (5, 1, 3)
    assert row.replenishment_ratio == Decimal("0.4")
    assert row.recovery_ns is None
    assert row.signal.level == 100_000


def test_markouts_use_the_signal_mid_and_a_late_horizon_has_none():
    row = _scan(_warm() + _tail() + _after_signal()).variants[ALL_TRADES].signals[0]

    assert row.markouts[5] == 0
    assert row.markouts[15] == Decimal(10) / Decimal(100_001) * 10_000
    assert row.markouts[30] is None


def test_a_cut_scan_gives_the_same_earlier_signals():
    events = _warm() + _tail() + _after_signal()
    full = _scan(events).variants[ALL_TRADES].signals
    signal_ts = full[0].signal.ts_init_ns

    before = _scan([e for e in events if e.ts_init < signal_ts]).variants[ALL_TRADES].signals
    at = _scan([e for e in events if e.ts_init <= signal_ts]).variants[ALL_TRADES].signals

    assert before == ()
    assert [r.signal for r in at] == [full[0].signal]


def _liquidation(ts, trade_id):
    tick = LiquidationTick(
        IID, trade_id, LiquidationKind.LIQUIDATION, AggressorSide.SELL, _px(99_999), _qty(50), ts, ts
    )
    return CustomData(context_data_type(LiquidationTick, IID), tick)


def test_the_forced_variant_loses_a_liquidation_print_known_first():
    events = [*_warm(), _liquidation(W + 50 * MS, "big"), *_tail()]

    scan = _scan(events, forced=True)

    assert len(scan.variants[ALL_TRADES].signals) == 1
    assert scan.variants[LIQUIDATIONS_REMOVED].signals == ()
    assert scan.liquidations == 1
    assert scan.matched_liquidations == 1


def test_a_repeated_trade_id_counts_once():
    duplicate = _trade(W + 10 * MS, 100_000, NautilusSide.BUYER, 1, "same")
    again = _trade(W + 20 * MS, 100_000, NautilusSide.BUYER, 1, "same")

    assert _scan([*_warm(), duplicate, again]).duplicates == 1


def _day_scan(scan, day):
    return DayScan(day, 1, 24, {}, 23 * 3600 * SEC, Decimal("0.2"), (scan,))


def _report(results, started, finished, source="tardis"):
    return build_report(
        load_lts_run(FROZEN, date(2025, 10, 19)), source, "a" * 40, results, started_at=started, finished_at=finished
    )


def test_two_reports_differ_only_in_the_time_fields():
    events = _warm() + _tail() + _after_signal()
    first = _report([_day_scan(_scan(events), date(2022, 6, 1))], "s1", "e1")
    second = _report([_day_scan(_scan(events), date(2022, 6, 1))], "s2", "e2")

    assert first != second
    assert json.dumps(first | {"started_at": "", "finished_at": ""}, sort_keys=True) == json.dumps(
        second | {"started_at": "", "finished_at": ""}, sort_keys=True
    )


def test_a_tardis_report_has_a_verdict_and_every_signal_field():
    report = _report([_day_scan(_scan(_warm() + _tail() + _after_signal()), date(2022, 6, 1))], "s", "e")

    assert report["verdict"]["verdict"] == "fail"
    assert report["cross_check"] is None
    assert report["days"][0]["variants"][ALL_TRADES]["past_segment_end"] == {"5": 0, "15": 0, "30": 1}
    signal = report["signals"][0]
    assert signal["markouts_bps"]["30"] is None
    assert signal["side"] == "long"
    assert signal["depth_before"] == "5"
    for key in ("mid", "level", "extreme", "buy_notional_1s", "sell_notional_1s", "impact_bps", "efficiency"):
        assert isinstance(signal[key], str)


def test_an_own_report_has_no_verdict_and_labels_no_matches_a_no_op():
    report = _report(
        [_day_scan(_scan(_warm() + _tail() + _after_signal(), forced=True), date(2026, 10, 1))], "s", "e", "own"
    )

    assert report["verdict"] is None
    assert report["cross_check"]["label"] == "no_op"
    assert report["cross_check"]["matched_trade_ids"] == 0


def test_the_report_path_names_source_hash_and_commit():
    path = report_path(Path("/r"), "own", "b" * 64, "c" * 40)

    assert path == Path("/r/lts-precheck") / f"own_{'b' * 12}_{'c' * 12}.json"


def test_a_missing_manifest_exits_2_before_reading(tmp_path, capsys):
    loaded = load_lts_run(FROZEN, date(2025, 10, 19))
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")

    code = run_precheck(settings, loaded, "tardis", loaded.run.days, datetime.now(UTC))

    assert code == 2
    assert "2022-06-01" in capsys.readouterr().out
    assert not (tmp_path / "reports").exists()
