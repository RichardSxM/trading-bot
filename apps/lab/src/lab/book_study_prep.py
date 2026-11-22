"""``lab book bootstrap`` and ``lab book benchmark`` (spec 0033 AC-14 to AC-16, AC-19a): the study's target.

``bootstrap`` times one rerun, builds the AC-15a block bootstrap paths for the 11 coins other than HYPE,
reruns ``trend_vol`` on every path at each AC-16 target, picks the largest target whose 95th percentile max
drawdown is strictly under 10%, checks the pick on real data with HYPE (stepping down a level when HYPE
deepens the in sample drawdown), repeats the pick's percentiles with 60 and 180 day blocks, and counts the
candidate's closed positions in the holdout. ``benchmark`` writes the AC-14a table at a given target. Both
write a Markdown section and JSON under ``<LAB_REPORTS_DIR>/study_prep/``; the sections go into
``precheck.md``. Files only.

Exit codes: 0 done (including "no target fits", which is recorded), 2 candles missing, 64 bad input.
"""

import argparse
import json
import multiprocessing
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from lab.book import BOOK_COINS, TREND_VOL, load_coin
from lab.candle_fetch import candle_tree
from lab.settings import LabSettings
from trading_core.lab.book_bootstrap import (
    CoinHistory,
    bootstrap_paths,
    daily_closes,
    daily_funding,
    hold_planner,
    nearest_rank,
    path_coins,
    pool_runs,
    scaled_days,
    unit_gross,
)
from trading_core.lab.book_replay import (
    CoinData,
    Measures,
    Planner,
    ReplayConfig,
    ReplayResult,
    first_ready_day,
    measures,
    replay_book,
    trend_daily_planner,
    trend_vol_planner,
)
from trading_core.strategy.trend_portfolio import PortfolioParams
from trading_core.strategy.trend_vol import BookVolParams

EXIT_MISSING: Final = 2
EXIT_USAGE: Final = 64
HYPE: Final = "PF_HYPEUSD.KRAKEN"
BTC: Final = "PF_XBTUSD.KRAKEN"
IN_SAMPLE_END: Final = date(2025, 10, 18)  # the day before the gate's holdout_start
HOLDOUT_START: Final = date(2025, 10, 19)
FULL_END: Final = date(2026, 11, 16)  # the AC-12 window's `to`
TARGETS: Final = (Decimal("0.04"), Decimal("0.06"), Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
DD_CAP: Final = Decimal("0.10")
BLOCK: Final = 120
SENSITIVITY_BLOCKS: Final = (60, 180)
SEED: Final = 7
FULL_PATHS: Final = 1000
FEW_PATHS: Final = 500
BUDGET_S: Final = 8 * 3600
WARMUP_DAYS: Final = 120
EQUITY: Final = Decimal(10000)


def trend_vol_at(target: Decimal) -> Planner:
    """``trend_vol`` as built with ``book_vol_target`` set to ``target``."""
    d = TREND_VOL
    params = PortfolioParams(d.lookback_days, d.vol_days, d.vol_target, d.gross_cap, d.min_history_days)
    return trend_vol_planner(params, BookVolParams(target, d.vol_days, d.gross_cap, d.rebalance_band))


def config(start: date, end: date) -> ReplayConfig:
    """The AC-9 replay at cut hour 00 with the trader's 150 kept closes."""
    return ReplayConfig(0, start, end, equity=EQUITY, keep_days=TREND_VOL.seed_days)


@dataclass(frozen=True)
class PathRun:
    """One rerun's figures."""

    target: Decimal
    path: int
    block: int
    sharpe: Decimal | None
    max_drawdown: Decimal
    ruined: bool
    unplanned: int
    small: int


# The paths and histories each worker reads; set before the pool forks.
_STATE: dict[str, object] = {}


def _run_path(job: tuple[Decimal, int, int]) -> PathRun:
    target, index, block = job
    histories: Mapping[str, CoinHistory] = _STATE["histories"]  # type: ignore[assignment]
    warmup: Sequence[date] = _STATE["warmup"]  # type: ignore[assignment]
    days: Sequence[date] = _STATE["days"]  # type: ignore[assignment]
    paths: Mapping[int, Sequence[Sequence[date]]] = _STATE["paths"]  # type: ignore[assignment]
    coins = path_coins(histories, warmup, days, paths[block][index])
    result = replay_book(coins, trend_vol_at(target), config(days[0], days[-1]))
    return PathRun(
        target,
        index,
        block,
        result.full.sharpe,
        Decimal(1) if result.ruined else result.full.max_drawdown,
        result.ruined,
        sum(1 for d in result.days if not d.planned),
        sum(d.small for d in result.days),
    )


def _percentiles(runs: Sequence[PathRun]) -> dict[str, Decimal | None]:
    sharpes = [r.sharpe for r in runs if r.sharpe is not None]
    dds = [r.max_drawdown for r in runs]
    out: dict[str, Decimal | None] = {}
    for p, label in ((Decimal("0.05"), "p5"), (Decimal("0.5"), "p50"), (Decimal("0.95"), "p95")):
        out[f"sharpe_{label}"] = nearest_rank(sharpes, p) if sharpes else None
        out[f"dd_{label}"] = nearest_rank(dds, p)
    return out


def _f(value: Decimal | None, places: str = "0.001") -> str:
    return "n/a" if value is None else str(value.quantize(Decimal(places)))


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{(value * 100).quantize(Decimal('0.1'))}%"


def load_kraken(settings: LabSettings) -> dict[str, CoinData]:
    """The 12 coins from the candles tree."""
    return {coin: load_coin(candle_tree(settings.lab_catalog_dir, coin)) for coin in BOOK_COINS}


def study_from(kraken: Mapping[str, CoinData]) -> date | None:
    """AC-19b: the first day the 11 coins other than HYPE all have ``min_history_days`` closes at 00 UTC."""
    return first_ready_day({c: d for c, d in kraken.items() if c != HYPE}, 0, TREND_VOL.min_history_days)


def run_bootstrap(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book bootstrap`` (module docstring)."""
    kraken = load_kraken(settings)
    start = study_from(kraken)
    if start is None:
        print("lab book bootstrap: the 11 coins never all have enough history", file=sys.stderr)
        return EXIT_MISSING
    eleven = {c: d for c, d in kraken.items() if c != HYPE}
    closes = {c: daily_closes(d) for c, d in eleven.items()}
    first_shared = max(min(c) for c in closes.values()) + timedelta(days=1)
    histories: dict[str, CoinHistory] = {}
    no_rate = 0
    for coin, data in eleven.items():
        days_with_close = sorted(closes[coin])
        funding, missing = daily_funding(data, [d for d in days_with_close if first_shared <= d <= IN_SAMPLE_END])
        no_rate += missing
        histories[coin] = CoinHistory(closes[coin], funding, data.spec)
    runs = pool_runs(closes, first_shared, IN_SAMPLE_END)
    pool_days = sum(len(r) for r in runs)
    warmup = [start - timedelta(days=WARMUP_DAYS - i) for i in range(WARMUP_DAYS)]
    days = [start + timedelta(days=i) for i in range((IN_SAMPLE_END - start).days + 1)]
    _STATE.update(histories=histories, warmup=warmup, days=days, paths={})
    timing_paths = bootstrap_paths(runs, BLOCK, len(days), 3, SEED)
    _STATE["paths"] = {BLOCK: timing_paths}
    seconds = []
    for i in range(3):
        t0 = time.monotonic()
        _run_path((Decimal("0.08"), i, BLOCK))
        seconds.append(time.monotonic() - t0)
    one = statistics.median(seconds)
    estimate = one * (len(TARGETS) * FULL_PATHS + len(SENSITIVITY_BLOCKS) * FULL_PATHS)
    count = FULL_PATHS if estimate <= BUDGET_S else FEW_PATHS
    print(f"one rerun {one:.1f} s, estimate {estimate / 3600:.1f} h at {FULL_PATHS} paths: using {count} paths")
    paths = {block: bootstrap_paths(runs, block, len(days), count, SEED) for block in (BLOCK, *SENSITIVITY_BLOCKS)}
    _STATE["paths"] = paths
    jobs = [(t, i, BLOCK) for t in TARGETS for i in range(count)]
    with ProcessPoolExecutor(settings.lab_workers, mp_context=multiprocessing.get_context("fork")) as pool:
        results = list(pool.map(_run_path, jobs, chunksize=8))
        by_target = {t: [r for r in results if r.target == t] for t in TARGETS}
        table = {t: _percentiles(rs) for t, rs in by_target.items()}
        fits = [t for t in TARGETS if (table[t]["dd_p95"] or Decimal(1)) < DD_CAP]
        pick = max(fits) if fits else None
        steps: list[str] = []
        if pick is not None:
            while True:
                with_hype = replay_book(kraken, trend_vol_at(pick), config(start, IN_SAMPLE_END))
                without = replay_book(eleven, trend_vol_at(pick), config(start, IN_SAMPLE_END))
                worse = with_hype.full.max_drawdown > without.full.max_drawdown
                steps.append(
                    f"{_pct(pick)}: in sample max drawdown with HYPE {_pct(with_hype.full.max_drawdown)}, "
                    f"without {_pct(without.full.max_drawdown)}, {'worse, step down' if worse else 'ok'}"
                )
                lower = [t for t in TARGETS if t < pick]
                if not worse or not lower:
                    break
                pick = max(lower)
        sensitivity: dict[int, dict[str, Decimal | None]] = {}
        if pick is not None:
            more = [(pick, i, block) for block in SENSITIVITY_BLOCKS for i in range(count)]
            extra = list(pool.map(_run_path, more, chunksize=8))
            sensitivity = {b: _percentiles([r for r in extra if r.block == b]) for b in SENSITIVITY_BLOCKS}
    holdout_closed = None
    if pick is not None:
        holdout = replay_book(kraken, trend_vol_at(pick), config(HOLDOUT_START, FULL_END))
        holdout_closed = holdout.full.closed
    lines = [
        "## Study target (task 9: AC-15, AC-15a, AC-16, AC-17, AC-19a)",
        "",
        f"_Block bootstrap of `trend_vol` (lookback 30, band 0.3, cap 5) on the 11 coins other than HYPE. Study "
        f"`from` {start} (AC-19b), in sample to {IN_SAMPLE_END} ({len(days)} days), warmup the 120 real closes "
        f"before it. Pool: {pool_days} days in {len(runs)} runs from {first_shared}. {count} paths per target "
        f"(one rerun {one:.1f} s; {estimate / 3600:.1f} h estimated at {FULL_PATHS}), {BLOCK} day blocks, seed "
        f"{SEED}. Coin days with no stored funding rate (counted as 0): {no_rate}._",
        "",
        "| Target | Sharpe p5 | Sharpe p50 | Sharpe p95 | Max DD p5 | Max DD p50 | Max DD p95 | Ruined | "
        "Unplanned days | Small skips |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in TARGETS:
        r, rs = table[t], by_target[t]
        lines.append(
            f"| {_pct(t)} | {_f(r['sharpe_p5'])} | {_f(r['sharpe_p50'])} | {_f(r['sharpe_p95'])} | "
            f"{_pct(r['dd_p5'])} | {_pct(r['dd_p50'])} | {_pct(r['dd_p95'])} | {sum(x.ruined for x in rs)} | "
            f"{sum(x.unplanned for x in rs)} | {sum(x.small for x in rs)} |"
        )
    lines.append("")
    if pick is None:
        lines += ["**No target has a 95th percentile max drawdown under 10%: no study is created (AC-16).**", ""]
    else:
        lines += ["12 coin check on real in sample data (AC-16):", "", *(f"- {s}" for s in steps), ""]
        lines += [f"**Study target: `book_vol_target` {pick} ({_pct(pick)} a year).**", ""]
        for b, r in sensitivity.items():
            lines.append(
                f"- Sensitivity, {b} day blocks at {_pct(pick)}: max DD p95 {_pct(r['dd_p95'])}, Sharpe p50 "
                f"{_f(r['sharpe_p50'])} (report only)"
            )
        lines += [
            "",
            "Candidate (AC-17): `trend_vol` (no `trend_vol_r2`, the AC-12 pre check failed).",
            "",
            f"Holdout closed positions (AC-19a, {HOLDOUT_START} to {FULL_END}, from flat, lookback 30): "
            f"**{holdout_closed}** against `min_holdout_trades` 60, "
            f"{'enough' if holdout_closed is not None and holdout_closed >= 60 else 'TOO FEW: no study'}.",
            "",
        ]
    out = Path(settings.lab_reports_dir) / "study_prep"
    out.mkdir(parents=True, exist_ok=True)
    (out / "bootstrap.md").write_text("\n".join(lines), encoding="utf-8")
    body = {
        "study_from": start.isoformat(),
        "paths": count,
        "one_rerun_s": one,
        "pick": None if pick is None else str(pick),
        "holdout_closed": holdout_closed,
        "table": {str(t): {k: None if v is None else str(v) for k, v in r.items()} for t, r in table.items()},
        "sensitivity": {
            str(b): {k: None if v is None else str(v) for k, v in r.items()} for b, r in sensitivity.items()
        },
    }
    (out / "bootstrap.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def _row(label: str, result: ReplayResult, scale: Decimal, start_equity: Decimal) -> tuple[str, Measures]:
    return label, measures(scaled_days(result.days, start_equity, scale), start_equity)


def run_benchmark(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book benchmark --target`` (module docstring)."""
    target: Decimal = args.target
    if not Decimal(0) < target <= Decimal(1):
        print(f"lab book benchmark: --target must be above 0 and at most 1, got {target}", file=sys.stderr)
        return EXIT_USAGE
    kraken = load_kraken(settings)
    start = study_from(kraken)
    if start is None:
        print("lab book benchmark: the 11 coins never all have enough history", file=sys.stderr)
        return EXIT_MISSING
    params = PortfolioParams(30, 30, Decimal("0.155"), Decimal(5), 120)  # the trader's TD_* settings
    lines = [
        f"## Benchmarks (task 9: AC-14, AC-14a) at `book_vol_target` {target}",
        "",
        "_Each benchmark runs at gross 1 with costs and funding, then its daily returns are scaled to the "
        "candidate's average gross over the same window. 12 coins, HYPE from its 120th close._",
        "",
    ]
    body: dict[str, object] = {}
    for label, end in (("in sample", IN_SAMPLE_END), ("full", FULL_END)):
        cfg = config(start, end)
        cand = replay_book(kraken, trend_vol_at(target), cfg)
        scale = cand.full.avg_gross
        rows = [
            ("trend_vol (candidate)", cand.full),
            _row("hold BTC", replay_book(kraken, hold_planner(120, [BTC]), cfg), scale, EQUITY),
            _row("hold all 12 equally", replay_book(kraken, hold_planner(120), cfg), scale, EQUITY),
            _row("trend_daily", replay_book(kraken, unit_gross(trend_daily_planner(params)), cfg), scale, EQUITY),
        ]
        lines += [
            f"### {label}: {start} to {end}, average gross {_f(scale, '0.01')}",
            "",
            "| Run | End | Sharpe | Sortino | Calmar | Max DD | Gross | Closed | BTC corr |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, m in rows:
            lines.append(
                f"| {name} | {_f(m.end_multiple, '0.01')}x | {_f(m.sharpe)} | {_f(m.sortino)} | {_f(m.calmar)} | "
                f"{_pct(m.max_drawdown)} | {_f(m.avg_gross, '0.01')} | {m.closed} | {_f(m.btc_corr)} |"
            )
        lines.append("")
        body[label] = {name: {"end": str(m.end_multiple), "sharpe": str(m.sharpe)} for name, m in rows}
    out = Path(settings.lab_reports_dir) / "study_prep"
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "benchmark.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def add_study_prep_parsers[P: argparse.ArgumentParser](sub: argparse._SubParsersAction[P]) -> None:
    """Add ``lab book bootstrap`` and ``lab book benchmark``."""
    sub.add_parser("bootstrap", help="block bootstrap reruns and the study target (spec 0033 AC-15, AC-16)")
    bench = sub.add_parser("benchmark", help="the benchmark table at a target (spec 0033 AC-14)")
    bench.add_argument("--target", type=Decimal, required=True, help="the AC-16 book_vol_target, e.g. 0.08")
