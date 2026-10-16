"""covers: spec 0009 AC-3 (parsing Kraken's ticker and trade feeds), AC-4 (bad messages skipped and
counted once a minute), AC-5 (recent uid dedupe), AC-6 (backoff, outage lines), AC-16 (no credentials).

The fixture holds real messages from the Milestone 0 gate (2026-10-11). No liquidation came in that
window, so forced trade rows here are built from Kraken's documented ``trade`` shape.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from trading_core.marketdata.context import AggressorSide, LiquidationKind
from trading_core.marketdata.context_feed import (
    BadCounter,
    BadRow,
    ForcedTrade,
    LagWatch,
    OpenInterest,
    Outage,
    RecentIds,
    backoff_s,
    close_at_run_end,
    pair_outages,
    parse_message,
    parse_outages,
    product_id,
    subscribe_messages,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kraken_context" / "feed_2026-10-11.jsonl"
MESSAGES = FIXTURE.read_text().splitlines()
S = 1_000_000_000


def _documented_trade(**changes):
    """A ``trade`` row as Kraken documents it; ``type`` is ``fill``, ``liquidation``, ``termination``, or ``block``."""
    row = {
        "product_id": "PF_XBTUSD",
        "feed": "trade",
        "uid": "f1d6b4c2-0000-4000-8000-000000000001",
        "side": "sell",
        "type": "liquidation",
        "time": 1791758800123,
        "qty": 0.5,
        "price": 83512.0,
        "seq": 1221999,
    }
    row.update(changes)
    return row


def test_the_product_id_is_the_instrument_id_without_its_venue():
    assert product_id("PF_XBTUSD.KRAKEN") == "PF_XBTUSD"


def test_events_and_fills_give_nothing():
    for text in MESSAGES:
        message = json.loads(text)
        if "event" in message or message.get("feed") in ("trade", "trade_snapshot"):
            assert parse_message(text).records == ()
            assert parse_message(text).bad == ()


def test_each_real_ticker_gives_its_open_interest_exactly():
    tickers = [t for t in MESSAGES if json.loads(t).get("feed") == "ticker" and "event" not in json.loads(t)]

    parsed = [parse_message(t).records for t in tickers]

    assert parsed[0] == (OpenInterest("PF_XBTUSD", Decimal("2423.1378"), 1791758735536 * 1_000_000),)
    assert all(len(p) == 1 and isinstance(p[0], OpenInterest) and isinstance(p[0].value, Decimal) for p in parsed)


@pytest.mark.parametrize(
    ("kind", "side"),
    [("liquidation", "sell"), ("termination", "buy"), ("block", "buy")],
)
def test_a_forced_trade_row_gives_its_fields(kind, side):
    parsed = parse_message(json.dumps(_documented_trade(type=kind, side=side)))

    assert parsed.records == (
        ForcedTrade(
            product_id="PF_XBTUSD",
            trade_id="f1d6b4c2-0000-4000-8000-000000000001",
            kind=LiquidationKind(kind),
            aggressor_side=AggressorSide.BUY if side == "buy" else AggressorSide.SELL,
            price=Decimal("83512.0"),
            size=Decimal("0.5"),
            ts_event=1791758800123 * 1_000_000,
        ),
    )


def test_a_snapshot_skips_its_bad_rows_alone():
    rows = [_documented_trade(), _documented_trade(type="swap"), _documented_trade(type="fill"), "x"]
    message = json.dumps({"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": rows})

    parsed = parse_message(message)

    assert [r.trade_id for r in parsed.records if isinstance(r, ForcedTrade)] == [rows[0]["uid"]]
    assert parsed.bad == (
        BadRow("trade_snapshot", "unknown_trade_type"),
        BadRow("trade_snapshot", "not_an_object"),
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{not json", "bad_json"),
        ("[1, 2]", "not_an_object"),
        (json.dumps(_documented_trade(qty="0.5")), "bad_qty"),
        (json.dumps(_documented_trade(price=None)), "missing_price"),
        (json.dumps(_documented_trade(side="up")), "bad_side"),
        (json.dumps(_documented_trade(time=1.5)), "bad_time"),
        (json.dumps(_documented_trade(uid="")), "missing_uid"),
        (json.dumps(_documented_trade(qty=0)), "bad_qty"),
        (json.dumps({"feed": "ticker", "product_id": "PF_XBTUSD", "time": 1, "openInterest": "x"}), "bad_openInterest"),
        (json.dumps({"event": "error", "message": "Invalid product id"}), "feed_error"),
    ],
)
def test_a_bad_message_is_skipped_with_its_reason(text, reason):
    parsed = parse_message(text)

    assert parsed.records == ()
    assert [b.reason for b in parsed.bad] == [reason]


def test_numbers_never_pass_through_a_float():
    message = json.dumps({"feed": "ticker", "product_id": "PF_XBTUSD", "time": 1, "openInterest": 0})
    exact = message.replace(": 0}", ": 2423.13780000000000000001}")

    (record,) = parse_message(exact).records

    assert isinstance(record, OpenInterest)
    assert record.value == Decimal("2423.13780000000000000001")


def test_a_ticker_without_open_interest_gives_nothing():
    assert parse_message(json.dumps({"feed": "ticker", "product_id": "PF_XBTUSD", "time": 1})) == parse_message("{}")


def test_the_subscribe_messages_carry_no_key():
    messages = [json.loads(m) for m in subscribe_messages(["PF_XBTUSD"])]

    assert messages == [
        {"event": "subscribe", "feed": "ticker", "product_ids": ["PF_XBTUSD"]},
        {"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD"]},
    ]


def test_recent_ids_drop_a_repeat_and_forget_past_the_limit():
    recent = RecentIds(limit=3)

    assert [recent.add(i) for i in ["a", "b", "a", "c", "d"]] == [True, True, False, True, True]
    assert recent.add("a") is True  # "a" fell out after "d"


def test_a_bad_reason_is_logged_once_a_minute_with_the_count_since():
    counter = BadCounter(every_ns=60 * S)
    row = BadRow("trade", "bad_qty")

    assert counter.add(row, 0) == 1
    assert [counter.add(row, t * S) for t in (10, 20, 59)] == [None, None, None]
    assert counter.add(row, 60 * S) == 4
    assert counter.add(BadRow("trade", "bad_side"), 61 * S) == 1


def test_backoff_doubles_up_to_the_ceiling():
    assert [backoff_s(a, 60) for a in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff_s(1_000, 60) == 60


def test_outage_lines_round_trip_and_bad_lines_are_counted():
    good = Outage(10, 20, "idle")
    lines = [good.line(), "\n", '{"start_ns": 5}\n', '{"start_ns": 9, "end_ns": 3, "reason": "x"}\n', '{"start_ns": 1']

    outages, bad = parse_outages(lines)

    assert good.line() == '{"end_ns":20,"reason":"idle","start_ns":10}\n'
    assert (outages, bad) == ([good], 3)


def test_an_open_line_has_a_null_end_and_reads_back():
    """covers: spec 0009 AC-6 (amended: the open line on entering down)."""
    opened = Outage(10, None, "idle")
    lines = [
        opened.line(),
        '{"start_ns": 10, "end_ns": "x", "reason": "idle"}\n',
        '{"start_ns": true, "end_ns": null, "reason": "x"}\n',
    ]

    outages, bad = parse_outages(lines)

    assert opened.line() == '{"end_ns":null,"reason":"idle","start_ns":10}\n'
    assert (outages, bad) == ([opened], 2)
    assert opened.is_open
    assert not Outage(10, 20, "idle").is_open


def test_a_closed_line_removes_its_open_partner():
    """covers: spec 0009 AC-6, AC-7 (open and closed lines paired by start_ns)."""
    first, second = Outage(10, None, "idle"), Outage(50, None, "closed")

    closed, waiting = pair_outages([], [first, second, Outage(10, 30, "idle")])

    assert closed == [Outage(10, 30, "idle")]
    assert waiting == [second]


def test_a_closed_line_a_pass_later_still_pairs_and_an_old_closed_line_stands_alone():
    """covers: spec 0009 AC-7 (a closed line read in a later pass; files from before open lines)."""
    closed, waiting = pair_outages([Outage(50, None, "closed")], [Outage(50, 90, "closed"), Outage(5, 8, "idle")])

    assert closed == [Outage(50, 90, "closed"), Outage(5, 8, "idle")]
    assert waiting == []


def test_open_lines_close_at_the_run_end_unless_they_start_at_or_after_it():
    """covers: spec 0009 AC-7 (tail reconnect)."""
    still_open = [Outage(10, None, "idle"), Outage(100, None, "closed"), Outage(120, None, "idle")]

    assert close_at_run_end(still_open, 100) == [Outage(10, 100, "idle")]


def test_lag_over_the_limit_is_logged_once_a_minute_with_the_largest_lag_and_count():
    """covers: spec 0009 AC-18."""
    watch = LagWatch(warn_ns=10 * S, every_ns=60 * S)

    assert watch.add(10 * S, 0) is None  # exactly the limit is not late
    assert watch.add(-5 * S, 0) is None  # a negative lag counts as zero
    assert watch.add(20 * S, 1 * S) == (20.0, 1)
    assert [watch.add(lag, t * S) for lag, t in ((30_123_456_789, 10), (15 * S, 20))] == [None, None]
    assert watch.add(11 * S, 61 * S) == (30.123, 3)
    assert watch.add(12 * S, 62 * S) is None
