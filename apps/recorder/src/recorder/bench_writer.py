"""The buffered writer benchmark (spec 0012 AC-7): ``python -m recorder.bench_writer``.

Pushes synthetic Kraken like messages through ``BufferedFeatherWriter.write`` and its flush tick
into a temp folder: ``OrderBookDeltas`` for ``PF_XBTUSD.KRAKEN`` of 1 or 2 deltas alternating, and
one ``TradeTick`` per 10 delta messages, at about 1,500 messages per second of clock time. Prints
messages per second of wall time and exits 1 below ``--min-rate``.
"""

import argparse
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity

from recorder.main import RECORDED_TYPES
from recorder.writer import BufferedFeatherWriter
from trading_core.marketdata.hours import SECOND_NS

INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
# The measured BTC peak, in messages per second of clock time.
PEAK_RATE = 1_500
MIN_RATE = 10_000


def _instrument() -> CryptoPerpetual:
    return CryptoPerpetual(
        instrument_id=INSTRUMENT_ID,
        raw_symbol=Symbol("PF_XBTUSD"),
        base_currency=BTC,
        quote_currency=USD,
        settlement_currency=USD,
        is_inverse=False,
        price_precision=1,
        size_precision=4,
        price_increment=Price.from_str("0.5"),
        size_increment=Quantity.from_str("0.0001"),
        ts_event=0,
        ts_init=0,
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
    )


def _messages(count: int, start_ns: int) -> list[tuple[int, object]]:
    step = SECOND_NS // PEAK_RATE
    out: list[tuple[int, object]] = []
    for i in range(count):
        ts = start_ns + i * step
        if i % 11 == 10:
            out.append(
                (
                    ts,
                    TradeTick(
                        INSTRUMENT_ID,
                        Price(80_000 + i % 40 * 0.5, 1),
                        Quantity(0.0123, 4),
                        AggressorSide.BUYER if i % 2 else AggressorSide.SELLER,
                        TradeId(str(i)),
                        ts,
                        ts,
                    ),
                )
            )
            continue
        deltas = [
            OrderBookDelta(
                INSTRUMENT_ID,
                BookAction.UPDATE,
                BookOrder(
                    OrderSide.BUY if j else OrderSide.SELL, Price(80_000 + (i + j) % 40 * 0.5, 1), Quantity(1.5, 4), 0
                ),
                0,
                i,
                ts,
                ts,
            )
            for j in range(1 + i % 2)
        ]
        out.append((ts, OrderBookDeltas(INSTRUMENT_ID, deltas)))
    return out


def run(count: int, folder: Path) -> float:
    """Write ``count`` messages into ``folder``; return messages per second of wall time."""
    start_ns = 1_791_439_200 * SECOND_NS
    messages = _messages(count, start_ns)
    clock = TestClock()
    clock.set_time(start_ns)
    cache = Cache()
    cache.add_instrument(_instrument())
    writer = BufferedFeatherWriter(
        folder / "run",
        config_json=b"{}",
        cache=cache,
        clock=clock,
        include_types=RECORDED_TYPES,
        flush_max_messages=4_096,
        stop=lambda: None,
    )
    next_tick = start_ns + SECOND_NS
    began = time.perf_counter()
    for ts, obj in messages:
        clock.set_time(ts)
        if ts >= next_tick:
            writer.flush_all(queue_depth=0, tick_lag_ns=0)
            next_tick += SECOND_NS
        writer.write(obj)
    writer.close()
    took = time.perf_counter() - began
    if writer.tripped:
        raise SystemExit("the writer tripped during the benchmark")
    return count / took


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--messages", type=int, default=600_000)
    parser.add_argument("--min-rate", type=int, default=MIN_RATE)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        rate = run(args.messages, Path(tmp))
    print(f"{args.messages} messages, {rate:,.0f} messages per second (needs {args.min_rate:,})")
    if rate < args.min_rate:
        sys.exit(1)


if __name__ == "__main__":
    main()
