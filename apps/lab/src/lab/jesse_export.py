"""``lab book jesse-export``: the inputs of the Jesse cross check (spec 0033 AC-11), as plain files.

Jesse runs in its own Python 3.13 container (Compose profile ``jesse``) and cannot read the Nautilus parquet of
the candles tree, so this command, in the lab, writes for each coin a folder under
``<LAB_REPORTS_DIR>/jesse/<coin>_<from>_<to>/`` with:

- ``candles.csv``: every 1 minute candle from ``WARMUP_DAYS`` before the window up to the window's last fill,
  in Jesse's column order (open time in ms, open, close, high, low, volume). A missing minute is filled flat
  at the last close with volume 0 (Jesse needs every minute), and the fills are counted.
- ``reference.json``: the pure replay of the same single coin book (``sign_planner``: the 30 day sign rule at
  gross 1, held until it flips, the same cost a side, no funding because Jesse books none), with the side and
  equity on every traded day.

``apps/lab/jesse/cross_check.py`` reads both. Files only, no database. Exit 0 done, 2 a coin has no candles in
the window, 64 bad input.
"""

import argparse
import csv
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from nautilus_trader.model.data import Bar
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.book import TREND_VOL, load_coin
from lab.candle_fetch import candle_tree
from lab.instruments import UnknownInstrumentError
from lab.settings import LabSettings
from trading_core.catalog.candles import CandleTree
from trading_core.lab.book_replay import CoinData, ReplayConfig, ReplayResult, replay_book, sign_planner
from trading_core.lab.candles import MINUTE_NS, Month

log = logging.getLogger(__name__)

EXIT_MISSING: Final = 2
EXIT_USAGE: Final = 64
# The Jesse strategy needs ``min_history_days`` daily closes before its first decision; 150 gives room.
WARMUP_DAYS: Final = 150
JESSE_COINS: Final = ("PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN")
MS_PER_NS: Final = 1_000_000


def _day_ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def minute_rows(tree: CandleTree, start_ns: int, end_ns: int) -> tuple[list[list[str]], int]:
    """Jesse rows for every minute whose candle opens in ``[start_ns, end_ns)``, and how many were filled flat.

    The tree keys a candle by its end (``ts_init``); Jesse by its open, one minute earlier. Before the first
    stored candle nothing is written, so the first row is a real one.
    """
    bars: dict[int, Bar] = {}
    month = Month.of_ns(start_ns)
    last = Month.of_ns(end_ns - 1)
    while month <= last:
        path = tree.bar_file(month)
        if path.is_file():
            table = pq.read_table(path)
            if table.num_rows:
                for bar in ArrowSerializer.deserialize(Bar, table.drop_columns(["quiet"])):
                    open_ns = int(bar.ts_init) - MINUTE_NS
                    if start_ns <= open_ns < end_ns:
                        bars[open_ns] = bar
        month = month.next()
    rows: list[list[str]] = []
    filled = 0
    close: str | None = None
    for open_ns in range(start_ns, end_ns, MINUTE_NS):
        bar = bars.get(open_ns)
        if bar is None:
            if close is None:
                continue
            rows.append([str(open_ns // MS_PER_NS), close, close, close, close, "0"])
            filled += 1
            continue
        close = str(bar.close)
        rows.append([str(open_ns // MS_PER_NS), str(bar.open), close, str(bar.high), str(bar.low), str(bar.volume)])
    return rows, filled


def reference(result: ReplayResult, coin: str, cost: Decimal, inputs: dict[str, str]) -> dict[str, object]:
    """The pure replay's figures for the cross check: the side and equity each day, the return, and the return
    after closing the last position at the last fill (Jesse closes every open position at its end)."""
    days = result.days
    last = days[-1]
    # One coin, so the gross is its notional over equity at the last fill.
    close_cost = last.equity * last.gross * cost
    start = result.start_equity
    return {
        "inputs": inputs,
        "days": [[d.day.isoformat(), _side(d.positions.get(coin, Decimal(0))), str(d.equity), d.traded] for d in days],
        "return": str(last.equity / start - 1),
        "return_closed": str((last.equity - close_cost) / start - 1),
        "trades": sum(d.traded for d in days),
        "fees": str(sum((d.fees for d in days), Decimal(0))),
    }


def _side(qty: Decimal) -> int:
    return 1 if qty > 0 else -1 if qty < 0 else 0


def run_jesse_export(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book jesse-export`` (module docstring)."""
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    try:
        trees = {coin: candle_tree(settings.lab_catalog_dir, coin) for coin in coins}
    except UnknownInstrumentError as exc:
        print(f"lab book jesse-export: {exc}", file=sys.stderr)
        return EXIT_USAGE
    first: date = args.from_
    last: date = args.to
    if last < first:
        print(f"lab book jesse-export: --to {last} is before --from {first}", file=sys.stderr)
        return EXIT_USAGE
    planner = sign_planner(TREND_VOL.lookback_days, TREND_VOL.min_history_days)
    for coin, tree in trees.items():
        data = load_coin(tree)
        if not data.hourly:
            print(f"lab book jesse-export: no candles for {coin}; run lab fetch-candles first", file=sys.stderr)
            return EXIT_MISSING
        single = {coin: CoinData(data.hourly, data.minutes, {}, data.spec)}
        try:
            config = ReplayConfig(0, first, last, equity=args.equity, cost=args.cost, keep_days=TREND_VOL.seed_days)
            result = replay_book(single, planner, config)
        except ValueError as exc:
            print(f"lab book jesse-export: {coin}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        start_ns = _day_ns(first - timedelta(days=WARMUP_DAYS))
        # Jesse decides at the close of day ``last - 1`` (00:00 of ``last``), the pure replay at 00:05 of ``last``.
        rows, filled = minute_rows(tree, start_ns, _day_ns(last))
        if not rows:
            print(f"lab book jesse-export: no minute candles for {coin} in the window", file=sys.stderr)
            return EXIT_MISSING
        folder = Path(settings.lab_reports_dir) / "jesse" / f"{coin.split('.')[0]}_{first}_{last}"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "candles.csv").open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        inputs = {
            "coin": coin,
            "from": first.isoformat(),
            "to": last.isoformat(),
            "equity": str(args.equity),
            "cost": str(args.cost),
            "lookback_days": str(TREND_VOL.lookback_days),
            "min_history_days": str(TREND_VOL.min_history_days),
            "candles_from": datetime.fromtimestamp(start_ns // 1_000_000_000, UTC).date().isoformat(),
            "minutes": str(len(rows)),
            "minutes_filled": str(filled),
        }
        ref = reference(result, coin, args.cost, inputs)
        (folder / "reference.json").write_text(json.dumps(ref, indent=2) + "\n", encoding="utf-8")
        print(
            f"{coin}: {len(rows)} minutes ({filled} filled flat), {len(result.days)} days, "
            f"{ref['trades']} trades, return {Decimal(str(ref['return'])):.4%}  folder {folder}"
        )
        log.info("lab_jesse_export_done", extra={"fields": {"coin": coin, "folder": str(folder), "filled": filled}})
    return 0
