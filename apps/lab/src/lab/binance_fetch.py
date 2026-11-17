"""``lab book binance-fetch`` (spec 0033 AC-12b): free Binance spot USDT hourly candles for the information runs.

Public, unauthenticated GETs to Binance's klines endpoint (interval 1h) through the lab's plain HTTP client,
for the 11 trend_vol coins other than HYPE, from 2017-08-17 to 2022-03-31 UTC. Each coin is one JSON file,
``<LAB_CATALOG_DIR>/binance/<SYMBOL>_1h.json``, holding every hourly close keyed by the hour's end in
nanoseconds; the lab data folder is git ignored, so nothing here is ever committed. ``load_binance_coin``
turns a file into the replay's ``CoinData``: the hourly closes are also the fills (there are no minute closes,
so a run fills at the next hourly close), with no funding.

Exit codes: 0 done, 1 a fetch failed or a row is bad, 64 bad input.
"""

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from lab.instruments import pinned_instrument
from lab.kraken_http import FetchFailedError, KrakenHttp
from lab.settings import LabSettings
from trading_core.lab.book_replay import CoinData, CoinSpec

log = logging.getLogger(__name__)

EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 64

# Every trend_vol coin Binance spot listed against USDT before 2022-04 (HYPE did not exist yet).
BINANCE_SYMBOLS: Final = {
    "PF_ADAUSD.KRAKEN": "ADAUSDT",
    "PF_DOGEUSD.KRAKEN": "DOGEUSDT",
    "PF_ETHUSD.KRAKEN": "ETHUSDT",
    "PF_LINKUSD.KRAKEN": "LINKUSDT",
    "PF_QNTUSD.KRAKEN": "QNTUSDT",
    "PF_SOLUSD.KRAKEN": "SOLUSDT",
    "PF_TRXUSD.KRAKEN": "TRXUSDT",
    "PF_UNIUSD.KRAKEN": "UNIUSDT",
    "PF_XBTUSD.KRAKEN": "BTCUSDT",
    "PF_XRPUSD.KRAKEN": "XRPUSDT",
    "PF_ZECUSD.KRAKEN": "ZECUSDT",
}
FETCH_FROM: Final = date(2017, 8, 17)
FETCH_TO: Final = date(2022, 3, 31)
KLINES_PATH: Final = "/api/v3/klines"
LIMIT: Final = 1000
HOUR_MS: Final = 3_600_000
MS_NS: Final = 1_000_000


class BadKlineError(ValueError):
    """A kline row is not one positive close on a whole hour, newer than the one before."""


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1000


def parse_klines(rows: Any, after_ms: int) -> list[tuple[int, Decimal]]:
    """``(hour end ns, close)`` for each kline row (open time first, close fifth), oldest first.

    Raises ``BadKlineError`` for a row that is not a list, an open time off the hour or not after
    ``after_ms`` and the row before, or a close that is not a positive number.
    """
    if not isinstance(rows, list):
        raise BadKlineError(f"klines body is not a list: {type(rows).__name__}")
    out: list[tuple[int, Decimal]] = []
    last = after_ms
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            raise BadKlineError(f"bad kline row: {row!r}")
        open_ms = int(row[0])
        if open_ms % HOUR_MS or open_ms < last:
            raise BadKlineError(f"kline open {open_ms} is off the hour or out of order")
        try:
            close = Decimal(str(row[4]))
        except InvalidOperation:
            raise BadKlineError(f"kline close {row[4]!r} is not a number") from None
        if close <= 0:
            raise BadKlineError(f"kline close {close} at {open_ms} is not above 0")
        out.append(((open_ms + HOUR_MS) * MS_NS, close))
        last = open_ms + HOUR_MS
    return out


def fetch_symbol(http: KrakenHttp, symbol: str, start: date, end: date) -> list[tuple[int, Decimal]]:
    """Every hourly close of ``symbol`` whose hour opens from ``start`` 00:00 to ``end`` 23:00 UTC."""
    cursor, stop = _ms(start), _ms(end + timedelta(days=1)) - HOUR_MS
    closes: list[tuple[int, Decimal]] = []
    while cursor <= stop:
        body = http.get_json(
            KLINES_PATH, {"symbol": symbol, "interval": "1h", "startTime": cursor, "endTime": stop, "limit": LIMIT}
        )
        batch = parse_klines(body, cursor)
        if not batch:
            break
        closes.extend(batch)
        cursor = batch[-1][0] // MS_NS
    return closes


def binance_file(folder: Path, coin: str) -> Path:
    """The JSON file of one coin's Binance hourly closes."""
    return folder / f"{BINANCE_SYMBOLS[coin]}_1h.json"


def write_closes(path: Path, symbol: str, closes: list[tuple[int, Decimal]]) -> None:
    """Write the closes atomically (a temporary file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"symbol": symbol, "interval": "1h", "hourly": [[ns, str(close)] for ns, close in closes]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_closes(path: Path) -> dict[int, Decimal]:
    """The hourly closes of one file, keyed by the hour's end in nanoseconds."""
    body = json.loads(path.read_text(encoding="utf-8"))
    return {int(ns): Decimal(close) for ns, close in body["hourly"]}


def load_binance_coin(folder: Path, coin: str) -> CoinData:
    """One coin's ``CoinData``: hourly closes that are also the fills, no funding, the pinned size figures."""
    hourly = read_closes(binance_file(folder, coin))
    instrument = pinned_instrument(coin)
    min_qty = None if instrument.min_quantity is None else instrument.min_quantity.as_decimal()
    return CoinData(hourly, hourly, {}, CoinSpec(instrument.size_increment.as_decimal(), min_qty))


def load_binance(folder: Path) -> Mapping[str, CoinData]:
    """Every coin whose file exists."""
    return {coin: load_binance_coin(folder, coin) for coin in BINANCE_SYMBOLS if binance_file(folder, coin).exists()}


def add_binance_parser[P: argparse.ArgumentParser](sub: argparse._SubParsersAction[P]) -> None:
    """Add ``lab book binance-fetch``."""
    fetch = sub.add_parser("binance-fetch", help="Binance spot hourly candles for the information runs (AC-12b)")
    fetch.add_argument("--coins", default=",".join(BINANCE_SYMBOLS), help="comma separated pinned ids (default 11)")


def run_binance_fetch(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book binance-fetch`` (module docstring)."""
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    unknown = [c for c in coins if c not in BINANCE_SYMBOLS]
    if unknown:
        print(f"lab book binance-fetch: no Binance symbol for {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    http = KrakenHttp(settings.lab_binance_url, settings.lab_kraken_pause_ms)
    folder = Path(settings.lab_catalog_dir) / "binance"
    for coin in coins:
        symbol = BINANCE_SYMBOLS[coin]
        try:
            closes = fetch_symbol(http, symbol, FETCH_FROM, FETCH_TO)
        except (FetchFailedError, BadKlineError) as exc:
            print(f"lab book binance-fetch: {symbol}: {exc}", file=sys.stderr)
            return EXIT_FAILED
        path = binance_file(folder, coin)
        write_closes(path, symbol, closes)
        first = datetime.fromtimestamp(closes[0][0] // 1_000_000_000, UTC) if closes else None
        print(f"{symbol}: {len(closes)} hours from {first} into {path}")
        log.info("lab_binance_fetched", extra={"fields": {"symbol": symbol, "hours": len(closes)}})
    return 0
