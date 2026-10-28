"""covers: spec 0014 AC-6, AC-9 (the candle stream: trade then quote, quiet minutes, funding, final quote, warmup)."""

from decimal import Decimal

from nautilus_trader.model.data import FundingRateUpdate, QuoteTick, TradeTick

from lab.candle_fetch import candle_tree
from lab.candle_stream import CandleStream, SideFlag
from trading_core.lab.candle_path import MS_NS
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
SEP = Month(2026, 9)
T0 = SEP.start_ns + HOUR_NS  # the range starts at 01:00


def _row(minute, o, h, lo, c, v="1", quiet=False):
    return CandleRow(T0 + (minute + 1) * MINUTE_NS, D(o), D(h), D(lo), D(c), D(v), quiet)


def _tree(tmp_path, rows, funding):
    tree = candle_tree(tmp_path, "PF_XBTUSD.KRAKEN")
    tree.write_bars(SEP, rows)
    if funding:
        tree.write_funding(SEP, funding)
    return tree


def _kinds(chunk):
    return [type(e).__name__[0] for e in chunk]  # T trade, Q quote, F funding


def test_quiet_start_is_skipped_then_trade_before_quote_and_a_final_quote(tmp_path):
    rows = [
        _row(-1, 99, 99, 99, 99, "0", quiet=True),  # before the range
        _row(0, 100, 100, 100, 100, "0", quiet=True),  # quiet at the range start: skipped
        _row(1, 100, 110, 95, 105),
        _row(2, 105, 105, 105, 105, "0", quiet=True),
        _row(3, 105, 106, 104, 106),
    ]
    funding = [
        FundingRow(T0 - HOUR_NS, D("0.0001"), HOUR_NS, "api"),  # warmup
        FundingRow(T0 + MINUTE_NS + MS_NS, D("0.0002"), HOUR_NS, "api"),  # same ns as minute 1's open point
        FundingRow(T0 + 30 * MINUTE_NS, D("0.0003"), HOUR_NS, "api"),  # after the final quote: the run has ended
    ]
    end = T0 + 40 * MINUTE_NS
    stream = CandleStream(_tree(tmp_path, rows, funding), T0, end, D(2), SideFlag())

    chunks = list(stream.chunks())

    assert [_kinds(c) for c in chunks] == [
        ["F", "T", "Q", "T", "Q", "T", "Q", "T", "Q"],
        ["Q"],
        ["T", "Q", "T", "Q", "T", "Q", "T", "Q"],
        ["Q"],
    ]
    assert stream.first_trade_ns == T0 + MINUTE_NS
    first_trade = chunks[0][1]
    assert isinstance(first_trade, TradeTick)
    assert first_trade.ts_init == T0 + MINUTE_NS + MS_NS
    assert str(first_trade.trade_id) == f"{T0 + MINUTE_NS + MS_NS}-0"
    quote = chunks[0][2]
    assert isinstance(quote, QuoteTick)
    assert (quote.bid_price.as_decimal(), quote.ask_price.as_decimal()) == (D(99), D(101))
    final = chunks[-1][-1]
    assert isinstance(final, QuoteTick)
    assert final.ts_init == T0 + 4 * MINUTE_NS - MS_NS + 2 * MS_NS  # minute 3's close point + 2 ms
    assert stream.final_ts_ns == final.ts_init
    assert stream.events == sum(len(c) for c in chunks)
    [warm] = stream.warmup()
    assert isinstance(warm, FundingRateUpdate)
    assert (warm.ts_init, warm.rate) == (T0 - HOUR_NS, D("0.0001"))


def test_the_side_is_read_as_each_minute_is_generated(tmp_path):
    rows = [_row(0, 100, 110, 95, 105), _row(1, 100, 110, 95, 105)]
    flag = SideFlag()
    stream = CandleStream(_tree(tmp_path, rows, []), T0, T0 + 10 * MINUTE_NS, D(0), flag)
    chunks = stream.chunks()

    flat = next(chunks)
    flag.side = -1
    short = next(chunks)

    def extremes(chunk):
        return [t.price.as_decimal() for t in chunk if isinstance(t, TradeTick)][1:3]

    assert extremes(flat) == [D(95), D(110)]  # flat: the low is nearer the open
    assert extremes(short) == [D(110), D(95)]  # short: the high first


def test_no_warmup_row_outside_the_carry_limit(tmp_path):
    funding = [FundingRow(T0 - 4 * HOUR_NS, D("0.0001"), HOUR_NS, "api")]
    stream = CandleStream(_tree(tmp_path, [], funding), T0, T0 + HOUR_NS, D(0), SideFlag())
    assert stream.warmup() == []
    assert list(stream.chunks()) == []
