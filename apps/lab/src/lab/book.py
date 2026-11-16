"""``lab book``: book strategies over the candles tree with the pure book replay (spec 0033 AC-9).

``lab book replay`` reads each coin's 1 minute candles and funding from the lab candles tree (``lab
fetch-candles``), keeps the whole hour closes (the hourly close ending at each hour) and the closes ending five
minutes past each hour (the fills), and runs ``trading_core.lab.book_replay`` over them. It writes one JSON
report under ``<LAB_REPORTS_DIR>/book/`` and prints the figures. Files only, no database.

Exit codes: 0 done, 2 a coin has no candles in the range, 64 bad input.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
from nautilus_trader.model.data import Bar
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.candle_fetch import candle_tree
from lab.instruments import UnknownInstrumentError, pinned_instrument
from lab.settings import LabSettings
from trading_core.catalog.candles import CandleTree
from trading_core.lab.book_replay import (
    DEFAULT_COST,
    FILL_DELAY_NS,
    CoinData,
    CoinSpec,
    Measures,
    Planner,
    ReplayConfig,
    ReplayResult,
    first_ready_day,
    replay_book,
    trend_daily_planner,
    trend_vol_planner,
)
from trading_core.lab.candles import Month
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import PortfolioParams
from trading_core.strategy.trend_vol import BookVolParams

log = logging.getLogger(__name__)

EXIT_MISSING: Final = 2
EXIT_USAGE: Final = 64

# The 12 coins the VPS demo trades (``PAPER_INSTRUMENTS``, spec 0033 AC-3).
BOOK_COINS: Final = (
    "PF_ADAUSD.KRAKEN",
    "PF_DOGEUSD.KRAKEN",
    "PF_ETHUSD.KRAKEN",
    "PF_HYPEUSD.KRAKEN",
    "PF_LINKUSD.KRAKEN",
    "PF_QNTUSD.KRAKEN",
    "PF_SOLUSD.KRAKEN",
    "PF_TRXUSD.KRAKEN",
    "PF_UNIUSD.KRAKEN",
    "PF_XBTUSD.KRAKEN",
    "PF_XRPUSD.KRAKEN",
    "PF_ZECUSD.KRAKEN",
)


@dataclass(frozen=True)
class BookDefaults:
    """The trader's ``TV_*`` settings (spec 0033 AC-3), copied here because the lab does not import the trader."""

    lookback_days: int = 30
    vol_days: int = 30
    vol_target: Decimal = Decimal("0.155")
    gross_cap: Decimal = Decimal(5)
    min_history_days: int = 120
    book_vol_target: Decimal = Decimal("0.5")
    rebalance_band: Decimal = Decimal("0.3")
    seed_days: int = 150


TREND_VOL: Final = BookDefaults()


def add_book_parser[P: argparse.ArgumentParser](
    commands: argparse._SubParsersAction[P], day: Callable[[str], date]
) -> None:
    """Add ``lab book`` and its ``replay`` command to the ``lab`` parser."""
    book = commands.add_parser("book", help="book strategies over the candles tree (spec 0033)")
    sub = book.add_subparsers(dest="book_command", required=True, parser_class=type(book))
    run = sub.add_parser("replay", help="the pure book replay of one strategy over the candles tree")
    run.add_argument("--strategy", choices=("trend_vol", "trend_daily"), default="trend_vol")
    run.add_argument("--cut-hour", type=int, default=0, help="the UTC hour the plan is made at (default 0)")
    run.add_argument("--from", dest="from_", type=day, help="first traded UTC day (default: every coin ready)")
    run.add_argument("--to", type=day, help="last traded UTC day (default: the last day BTC has a fill)")
    run.add_argument("--midpoint", type=day, help="first day of the second half (default: the middle day)")
    run.add_argument("--band", type=Decimal, help=f"no trade band (default {TREND_VOL.rebalance_band})")
    run.add_argument("--book-vol-target", type=Decimal, help=f"annual (default {TREND_VOL.book_vol_target})")
    run.add_argument("--lookback-days", type=int, help=f"default {TREND_VOL.lookback_days}")
    run.add_argument("--equity", type=Decimal, default=Decimal(10000))
    run.add_argument("--cost", type=Decimal, default=DEFAULT_COST, help="a side, on traded notional (0.0007)")
    run.add_argument("--coins", default=",".join(BOOK_COINS), help="comma separated pinned ids (default the 12)")
    run.add_argument("--name", help="report file name (default from the strategy, cut hour, and band)")


def _months(tree: CandleTree) -> list[Month]:
    folder = tree.bar_file(Month(2000, 1)).parent
    return sorted(Month.parse(p.stem) for p in folder.glob("*.parquet")) if folder.is_dir() else []


def load_coin(tree: CandleTree) -> CoinData:
    """One coin's whole hour closes, ``H:05`` closes, and funding rates from the candles tree.

    Only the rows at a whole hour or five minutes past one are deserialized (the rest are dropped by their
    ``ts_init`` first). A missing minute is simply absent: the replay then counts a missing close or fill.
    """
    hourly: dict[int, Decimal] = {}
    minutes: dict[int, Decimal] = {}
    funding: dict[int, Decimal] = {}
    for month in _months(tree):
        table = pq.read_table(tree.bar_file(month))
        if table.num_rows:
            stamps = table.column("ts_init").to_pylist()
            keep = pa.array([t % HOUR_NS in (0, FILL_DELAY_NS) for t in stamps])
            picked = table.filter(keep).drop_columns(["quiet"])
            for bar in ArrowSerializer.deserialize(Bar, picked):
                ts = int(bar.ts_init)
                close = Decimal(str(bar.close))
                if ts % HOUR_NS == 0:
                    hourly[ts] = close
                else:
                    minutes[ts] = close
        for row in tree.read_funding(month):
            funding[row.charge_ns] = row.rate
    instrument = tree.instrument
    min_qty = None if instrument.min_quantity is None else instrument.min_quantity.as_decimal()
    return CoinData(hourly, minutes, funding, CoinSpec(instrument.size_increment.as_decimal(), min_qty))


def planner_for(strategy: str, defaults: BookDefaults, args: argparse.Namespace) -> tuple[Planner, Decimal]:
    """The strategy's planner with the flags applied, and the band it trades with."""
    params = PortfolioParams(
        lookback_days=args.lookback_days or defaults.lookback_days,
        vol_days=defaults.vol_days,
        vol_target=defaults.vol_target,
        gross_cap=defaults.gross_cap,
        min_history_days=defaults.min_history_days,
    )
    if strategy == "trend_daily":
        return trend_daily_planner(params), Decimal(0)
    band = defaults.rebalance_band if args.band is None else args.band
    target = defaults.book_vol_target if args.book_vol_target is None else args.book_vol_target
    return trend_vol_planner(params, BookVolParams(target, defaults.vol_days, defaults.gross_cap, band)), band


def _text(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _text(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_text(v) for v in value]
    return value


def report(result: ReplayResult, inputs: Mapping[str, object]) -> dict[str, object]:
    """The JSON report: the inputs, the figures per period, the skip totals, and the daily equity."""
    days = result.days
    return {
        "inputs": _text(inputs),
        "midpoint": result.midpoint.isoformat(),
        "ruined": result.ruined,
        "funding_missing_days": result.funding_missing_days,
        "totals": {
            "fees": str(sum((d.fees for d in days), Decimal(0))),
            "funding": str(sum((d.funding for d in days), Decimal(0))),
            "traded": sum(d.traded for d in days),
            "band": sum(d.band for d in days),
            "small": sum(d.small for d in days),
            "close_missing": sum(d.close_missing for d in days),
            "no_fill": sum(d.no_fill for d in days),
            "unplanned_days": sum(1 for d in days if not d.planned),
        },
        "full": _text(asdict(result.full)),
        "halves": [_text(asdict(m)) for m in result.halves],
        "years": {str(y): _text(asdict(m)) for y, m in result.years.items()},
        "daily": [[d.day.isoformat(), str(d.equity), str(d.gross)] for d in days],
    }


def _fmt(value: Decimal | None, places: str = "0.01") -> str:
    return "n/a" if value is None else str(value.quantize(Decimal(places)))


def table(result: ReplayResult) -> str:
    """The figures as a plain text table, one row per period."""
    rows: list[tuple[str, Measures]] = [
        ("full", result.full),
        ("half 1", result.halves[0]),
        ("half 2", result.halves[1]),
    ]
    rows += [(str(year), m) for year, m in result.years.items()]
    lines = [
        "period   first       last        days  multiple  sharpe  sortino  calmar  max_dd  gross  closed  btc_corr"
    ]
    for label, m in rows:
        lines.append(
            f"{label:<8} {m.first} {m.last} {m.days:>5}  {_fmt(m.end_multiple, '0.001'):>8}  {_fmt(m.sharpe):>6}  "
            f"{_fmt(m.sortino):>7}  {_fmt(m.calmar):>6}  {_fmt(m.max_drawdown, '0.001'):>6}  "
            f"{_fmt(m.avg_gross):>5}  {m.closed:>6}  {_fmt(m.btc_corr):>8}"
        )
    return "\n".join(lines)


def _last_fill_day(data: CoinData, cut_hour: int) -> date | None:
    offset = cut_hour * HOUR_NS + FILL_DELAY_NS
    days = [ns for ns in data.minutes if ns % (24 * HOUR_NS) == offset]
    return datetime.fromtimestamp(max(days) // 1_000_000_000, UTC).date() if days else None


def run_book_replay(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book replay`` (module docstring)."""
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    try:
        trees = {coin: candle_tree(settings.lab_catalog_dir, coin) for coin in coins}
        for coin in coins:
            pinned_instrument(coin)
    except UnknownInstrumentError as exc:
        print(f"lab book replay: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not 0 <= args.cut_hour <= 23:
        print(f"lab book replay: --cut-hour must be 0 to 23, got {args.cut_hour}", file=sys.stderr)
        return EXIT_USAGE
    data = {coin: load_coin(tree) for coin, tree in trees.items()}
    empty = [coin for coin, d in data.items() if not d.hourly]
    if empty:
        print(f"lab book replay: no candles for {', '.join(empty)}; run lab fetch-candles first", file=sys.stderr)
        return EXIT_MISSING
    start = args.from_ or first_ready_day(data, args.cut_hour, TREND_VOL.min_history_days)
    btc = data.get("PF_XBTUSD.KRAKEN") or next(iter(data.values()))
    end = args.to or _last_fill_day(btc, args.cut_hour)
    if start is None or end is None:
        print("lab book replay: no day where every coin has enough history", file=sys.stderr)
        return EXIT_MISSING
    planner, band = planner_for(args.strategy, TREND_VOL, args)
    try:
        config = ReplayConfig(
            args.cut_hour, start, end, equity=args.equity, cost=args.cost, keep_days=TREND_VOL.seed_days
        )
        result = replay_book(data, planner, config, args.midpoint)
    except ValueError as exc:
        print(f"lab book replay: {exc}", file=sys.stderr)
        return EXIT_USAGE
    inputs = {
        "strategy": args.strategy,
        "coins": coins,
        "cut_hour": args.cut_hour,
        "from": start,
        "to": end,
        "band": band,
        "book_vol_target": args.book_vol_target or TREND_VOL.book_vol_target,
        "lookback_days": args.lookback_days or TREND_VOL.lookback_days,
        "equity": args.equity,
        "cost": args.cost,
    }
    name = args.name or f"{args.strategy}_h{args.cut_hour:02d}_band{band}_{start}_{end}"
    out = Path(settings.lab_reports_dir) / "book" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report(result, inputs), indent=2) + "\n", encoding="utf-8")
    print(table(result))
    totals = report(result, inputs)["totals"]
    print(f"funding missing days {result.funding_missing_days}, totals {totals}")
    print(f"report {out}")
    log.info("lab_book_replay_done", extra={"fields": {"report": str(out), "days": len(result.days)}})
    return 0
