"""``lab book precheck <file>`` (spec 0033 AC-12, AC-12a, AC-12b): the pre registered pre check, run once.

The pre registration file (``studies/trend_vol_precheck.toml``) fixes the window, the midpoint, the cost, the
baseline cut hours, the Binance window, the pass rule as text, and every trial with its literal settings.
The run refuses any name already in the ledger (``studies/precheck_ledger.toml``), replays every baseline
and trial over the Kraken candles tree (and the flagged ones over the AC-12b Binance hourly closes),
judges them with ``trading_core.lab.precheck``, and writes ``precheck.md``, ``results.json``, and the
ledger with this run's names added under ``<LAB_REPORTS_DIR>/precheck/<name>/``. Copy those two files
into the spec folder and ``studies/``, and commit them: the committed ledger is what refuses a rerun.

Exit codes: 0 done (pass or fail), 2 candles or Binance closes missing for the window, 64 a bad file or a
name already in the ledger.
"""

import argparse
import json
import multiprocessing
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from lab.binance_fetch import FETCH_FROM, load_binance
from lab.book import BOOK_COINS, TREND_VOL, load_coin
from lab.candle_fetch import candle_tree
from lab.settings import LabSettings
from trading_core.lab.book_replay import (
    FILL_DELAY_NS,
    CoinData,
    Planner,
    ReplayConfig,
    cut_ns,
    replay_book,
    trend_vol_planner,
)
from trading_core.lab.book_trials import ScaleStop, TrialResult, ewma_planner, replay_trial, tranche_hours
from trading_core.lab.precheck import FAMILIES, ROLES, RunFigures, Verdict, precheck_verdicts, reference
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import PortfolioParams
from trading_core.strategy.trend_vol import BookVolParams

EXIT_MISSING: Final = 2
EXIT_USAGE: Final = 64
KRAKEN: Final = "kraken"
BINANCE: Final = "binance"


class PrecheckFileError(ValueError):
    """The pre registration file or the ledger is not usable; ``problems`` lists every reason."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class TrialSpec:
    """One pre registered trial: its name, family, role, cut hour, and the rule's literal settings."""

    name: str
    family: str
    role: str
    cut_hour: int = 0
    half_life: int | None = None
    tranches: int = 1
    ramp_days: int = 1
    stop_vols: Decimal | None = None
    binance: bool = False


@dataclass(frozen=True)
class PrecheckFile:
    """The pre registration file's values."""

    name: str
    start: date
    end: date
    midpoint: date
    cost: Decimal
    equity: Decimal
    baseline_hours: tuple[int, ...]
    binance_from: date
    binance_to: date
    pass_rule: str
    trials: tuple[TrialSpec, ...]

    def names(self) -> list[str]:
        """Every name this run spends: the pre check, each baseline, and each trial."""
        return [self.name, *(baseline_name(h) for h in self.baseline_hours), *(t.name for t in self.trials)]


def baseline_name(hour: int) -> str:
    """The ledger name of the baseline at a cut hour."""
    return f"baseline@{hour:02d}"


def _decimal(value: object, where: str, problems: list[str]) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        problems.append(f"{where} is not a number: {value!r}")
        return None


def _date(raw: Mapping[str, Any], key: str, where: str, problems: list[str]) -> date | None:
    value = raw.get(key)
    if not isinstance(value, date):
        problems.append(f"{where}{key} must be a TOML date, got {value!r}")
        return None
    return value


def _trial(raw: Mapping[str, Any], index: int, problems: list[str]) -> TrialSpec | None:
    where = f"trial {index}"
    name, family, role = raw.get("name"), raw.get("family"), raw.get("role")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}: name is missing")
        return None
    where = f"trial {name}"
    if family not in FAMILIES:
        problems.append(f"{where}: family must be one of {', '.join(FAMILIES)}")
    if role not in ROLES:
        problems.append(f"{where}: role must be one of {', '.join(ROLES)}")
    known = {"name", "family", "role", "cut_hour", "half_life", "tranches", "ramp_days", "stop_vols", "binance"}
    extra = sorted(set(raw) - known)
    if extra:
        problems.append(f"{where}: unknown keys {', '.join(extra)}")
    cut_hour = raw.get("cut_hour", 0)
    tranches = raw.get("tranches", 1)
    ramp_days = raw.get("ramp_days", 1)
    half_life = raw.get("half_life")
    binance = raw.get("binance", False)
    for key, value in (("cut_hour", cut_hour), ("tranches", tranches), ("ramp_days", ramp_days)):
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{where}: {key} must be an integer")
            return None
    if half_life is not None and (not isinstance(half_life, int) or half_life < 1):
        problems.append(f"{where}: half_life must be a positive integer")
    if not isinstance(binance, bool):
        problems.append(f"{where}: binance must be true or false")
    stop = raw.get("stop_vols")
    stop_vols = None if stop is None else _decimal(stop, f"{where}: stop_vols", problems)
    try:
        tranche_hours(cut_hour, tranches)
        ScaleStop(ramp_days, stop_vols)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    if family == "ewma" and half_life is None:
        problems.append(f"{where}: an ewma trial needs half_life")
    if family == "tranche" and tranches < 2:
        problems.append(f"{where}: a tranche trial needs tranches of 2 or more")
    if family == "scale" and (tranches != 1 or half_life is not None):
        problems.append(f"{where}: a scale trial runs one book with the plain vols")
    return TrialSpec(name, str(family), str(role), cut_hour, half_life, tranches, ramp_days, stop_vols, binance is True)


def load_precheck(path: Path) -> PrecheckFile:
    """The pre registration file, every problem listed in one ``PrecheckFileError``."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PrecheckFileError([f"{path}: {exc}"]) from None
    problems: list[str] = []
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        problems.append("name is missing")
    start, end, mid = (_date(raw, k, "", problems) for k in ("from", "to", "midpoint"))
    cost = _decimal(raw.get("cost"), "cost", problems)
    equity = _decimal(raw.get("equity"), "equity", problems)
    hours = raw.get("baseline_cut_hours")
    if not isinstance(hours, list) or not hours or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
        problems.append("baseline_cut_hours must be a list of hours 0 to 23")
        hours = []
    if 0 not in hours:
        problems.append("baseline_cut_hours must hold 0")
    binance = raw.get("binance", {})
    b_from = _date(binance, "from", "binance.", problems) if isinstance(binance, dict) else None
    b_to = _date(binance, "to", "binance.", problems) if isinstance(binance, dict) else None
    rule = raw.get("pass_rule")
    if not isinstance(rule, str) or not rule.strip():
        problems.append("pass_rule must be the pass rule as text")
    raw_trials = raw.get("trial")
    trials: list[TrialSpec] = []
    if not isinstance(raw_trials, list) or not raw_trials:
        problems.append("at least one [[trial]] is needed")
    else:
        for i, item in enumerate(raw_trials, start=1):
            trial = _trial(item, i, problems) if isinstance(item, dict) else None
            if trial is not None:
                trials.append(trial)
    seen: set[str] = set()
    for trial in trials:
        if trial.name in seen:
            problems.append(f"trial {trial.name} is listed twice")
        seen.add(trial.name)
    if start and end and mid and not start < mid <= end:
        problems.append(f"midpoint {mid} must fall after from {start} and no later than to {end}")
    if b_from and b_to and not FETCH_FROM < b_from <= b_to:
        problems.append(f"binance window {b_from}..{b_to} must start after {FETCH_FROM}")
    if equity is not None and equity <= 0:
        problems.append("equity must be above 0")
    if cost is not None and cost < 0:
        problems.append("cost must be at least 0")
    if problems:
        raise PrecheckFileError(problems)
    if (
        not isinstance(name, str)
        or start is None
        or end is None
        or mid is None
        or cost is None
        or equity is None
        or b_from is None
        or b_to is None
        or not isinstance(rule, str)
    ):
        raise PrecheckFileError(["the file is incomplete"])
    return PrecheckFile(name, start, end, mid, cost, equity, tuple(hours), b_from, b_to, rule.strip(), tuple(trials))


def load_ledger(path: Path) -> list[str]:
    """The names already run, from ``names = [...]``."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PrecheckFileError([f"{path}: {exc}"]) from None
    names = raw.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PrecheckFileError([f"{path}: names must be a list of strings"])
    return list(names)


def ledger_text(names: Sequence[str]) -> str:
    """The ledger file for ``names``, one per line."""
    lines = "".join(f'    "{n}",\n' for n in names)
    return (
        "# Every pre check name already run (spec 0033 AC-12). `lab book precheck` refuses a name listed here.\n"
        f"names = [\n{lines}]\n"
    )


@dataclass(frozen=True)
class Row:
    """One run's figures for the report."""

    name: str
    venue: str
    family: str
    role: str
    cut_hour: int
    sharpe_h1: Decimal | None
    sharpe_h2: Decimal | None
    sharpe_full: Decimal | None
    end_multiple: Decimal
    max_drawdown: Decimal
    avg_gross: Decimal
    avg_turnover: Decimal
    stopped_share: Decimal
    stops: int
    closed: int
    days: int
    funding_missing_days: int
    ruined: bool

    def figures(self) -> RunFigures:
        """The figures the pass rules read."""
        return RunFigures(
            self.name, self.family, self.role, self.cut_hour, self.sharpe_h1, self.sharpe_h2, self.sharpe_full
        )


@dataclass(frozen=True)
class Job:
    """One replay: the trial (None for a baseline), the venue, and the window."""

    name: str
    venue: str
    trial: TrialSpec | None
    cut_hour: int
    start: date
    end: date
    midpoint: date | None
    cost: Decimal
    equity: Decimal


# The coins each worker reads; set before the pool forks so the workers share them.
_DATA: dict[str, Mapping[str, CoinData]] = {}


def _params() -> tuple[PortfolioParams, BookVolParams]:
    d = TREND_VOL
    params = PortfolioParams(d.lookback_days, d.vol_days, d.vol_target, d.gross_cap, d.min_history_days)
    return params, BookVolParams(d.book_vol_target, d.vol_days, d.gross_cap, d.rebalance_band)


def planner_of(trial: TrialSpec | None) -> Planner:
    """The trial's planner: ``trend_vol`` as built, or with EWMA vols."""
    params, book = _params()
    if trial is not None and trial.half_life is not None:
        return ewma_planner(params, book, trial.half_life)
    return trend_vol_planner(params, book)


def run_job(job: Job) -> Row:
    """Replay one job; a baseline is also replayed by ``replay_book`` and must match it day for day."""
    coins = _DATA[job.venue]
    delay = FILL_DELAY_NS if job.venue == KRAKEN else HOUR_NS
    config = ReplayConfig(
        job.cut_hour,
        job.start,
        job.end,
        equity=job.equity,
        cost=job.cost,
        keep_days=TREND_VOL.seed_days,
        fill_delay_ns=delay,
    )
    trial = job.trial
    planner = planner_of(trial)
    rules = None
    if trial is not None and trial.family == "scale":
        rules = ScaleStop(trial.ramp_days, trial.stop_vols)
    result: TrialResult = replay_trial(
        coins, planner, config, tranches=1 if trial is None else trial.tranches, rules=rules, midpoint=job.midpoint
    )
    if trial is None:
        check = replay_book(coins, planner, config, job.midpoint)
        if check.days != result.replay.days:
            raise RuntimeError(f"{job.name} on {job.venue}: replay_trial and replay_book differ")
    replay = result.replay
    full = replay.full
    return Row(
        job.name,
        job.venue,
        "" if trial is None else trial.family,
        "baseline" if trial is None else trial.role,
        job.cut_hour,
        replay.halves[0].sharpe,
        replay.halves[1].sharpe,
        full.sharpe,
        full.end_multiple,
        full.max_drawdown,
        full.avg_gross,
        result.avg_turnover,
        result.stopped_share,
        result.stops,
        full.closed,
        full.days,
        replay.funding_missing_days,
        replay.ruined,
    )


def jobs_for(pre: PrecheckFile) -> list[Job]:
    """Every Kraken run, then the Binance information runs (the baseline at 00 and every flagged trial)."""
    out = [
        Job(baseline_name(h), KRAKEN, None, h, pre.start, pre.end, pre.midpoint, pre.cost, pre.equity)
        for h in pre.baseline_hours
    ]
    out += [
        Job(t.name, KRAKEN, t, t.cut_hour, pre.start, pre.end, pre.midpoint, pre.cost, pre.equity) for t in pre.trials
    ]
    out.append(Job(baseline_name(0), BINANCE, None, 0, pre.binance_from, pre.binance_to, None, pre.cost, pre.equity))
    out += [
        Job(t.name, BINANCE, t, 0, pre.binance_from, pre.binance_to, None, pre.cost, pre.equity)
        for t in pre.trials
        if t.binance
    ]
    return out


def _f(value: Decimal | None, places: str = "0.001") -> str:
    return "n/a" if value is None else str(value.quantize(Decimal(places)))


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'))}%"


def _table(rows: Sequence[Row], halves: bool) -> list[str]:
    head = (
        "| Run | Cut | Sharpe H1 | Sharpe H2 | Sharpe full | End | Max DD | Gross | Turnover a day | Stopped | Stops |"
    )
    if not halves:
        head = (
            "| Run | Cut | Sharpe full | Change vs baseline | End | Max DD | Gross | Turnover a day | Stopped | Stops |"
        )
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    base = next((r for r in rows if r.role == "baseline" and r.cut_hour == 0), None)
    for r in rows:
        common = (
            f"{_f(r.end_multiple, '0.01')}x | {_pct(r.max_drawdown)} | {_f(r.avg_gross, '0.01')} | "
            f"{_pct(r.avg_turnover)} | {_pct(r.stopped_share)} | {r.stops} |"
        )
        if halves:
            lines.append(
                f"| {r.name} | {r.cut_hour:02d} | {_f(r.sharpe_h1)} | {_f(r.sharpe_h2)} | {_f(r.sharpe_full)} | "
                + common
            )
        else:
            change = (
                None
                if base is None or base.sharpe_full is None or r.sharpe_full is None or r is base
                else r.sharpe_full - base.sharpe_full
            )
            lines.append(f"| {r.name} | {r.cut_hour:02d} | {_f(r.sharpe_full)} | {_f(change)} | " + common)
    return lines


def scale_label(verdicts: Sequence[Verdict], binance: Sequence[Row], main: str) -> str | None:
    """AC-12a's label: None unless the scale family passed; then "seen", or "seen, fragile" when the main
    trial's Binance information run did worse than the Binance baseline at 00."""
    passed = any(v.family == "scale" and v.passed for v in verdicts)
    if not passed:
        return None
    base = next((r for r in binance if r.role == "baseline"), None)
    run = next((r for r in binance if r.name == main), None)
    if base is None or run is None or base.sharpe_full is None or run.sharpe_full is None:
        return "seen, fragile (no Binance comparison)"
    return "seen, fragile" if run.sharpe_full < base.sharpe_full else "seen"


def render(pre: PrecheckFile, rows: Sequence[Row], verdicts: Sequence[Verdict], commit: str) -> str:
    """``precheck.md``: the inputs, both tables, the reference, every verdict, and the labels."""
    kraken = [r for r in rows if r.venue == KRAKEN]
    binance = [r for r in rows if r.venue == BINANCE]
    baselines = {r.cut_hour: r.figures() for r in kraken if r.role == "baseline"}
    ref = reference(baselines)
    scale_main = next((t.name for t in pre.trials if t.family == "scale" and t.role == "main" and t.binance), "")
    label = scale_label(verdicts, binance, scale_main)
    funding_missing = max((r.funding_missing_days for r in kraken), default=0)
    lines = [
        f"# Pre check: {pre.name} · spec 0033 AC-12, AC-12a, AC-12b",
        "",
        f"_Run once by `lab book precheck` at commit `{commit}`. Window {pre.start} to {pre.end}, second half from "
        f"{pre.midpoint}, cost {pre.cost} a side, starting equity {pre.equity}, 12 coins on Kraken candles. "
        f"Funding where Kraken has rates; the most days with a missing rate in any run: {funding_missing}._",
        "",
        "## Pass rule (as pre registered)",
        "",
        pre.pass_rule,
        "",
        "## Kraken runs",
        "",
        *_table(kraken, halves=True),
        "",
        f"Baseline reference (mean of the cut hours): H1 {_f(None if ref is None else ref[0])}, "
        f"H2 {_f(None if ref is None else ref[1])}.",
        "",
        "## Verdicts",
        "",
    ]
    for v in verdicts:
        lines.append(f"### {v.family}: {'PASS' if v.passed else 'FAIL'}")
        lines.append("")
        lines += [f"- {reason}" for reason in v.reasons]
        lines.append("")
    if label is not None:
        lines += [f"AC-12a label: **{label}** (found by a scratch replay that saw this window, 2026-11-17).", ""]
    lines += [
        "## Binance information runs (AC-12b, information only, other venue)",
        "",
        f"_Binance spot USDT hourly closes, {pre.binance_from} to {pre.binance_to}, cut hour 00, fills at the next "
        "hourly close, no funding, 11 coins (no HYPE). Nothing here decides a pass._",
        "",
        *_table(binance, halves=False),
        "",
    ]
    ruined = [f"{r.name} ({r.venue})" for r in rows if r.ruined]
    if ruined:
        lines += [f"Ruined runs (equity reached 0): {', '.join(ruined)}.", ""]
    return "\n".join(lines)


def _json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def add_precheck_parser[P: argparse.ArgumentParser](sub: argparse._SubParsersAction[P]) -> None:
    """Add ``lab book precheck``."""
    run = sub.add_parser("precheck", help="the pre registered pre check, run once (spec 0033 AC-12)")
    run.add_argument("file", type=Path, help="the pre registration file, studies/trend_vol_precheck.toml")
    run.add_argument("--ledger", type=Path, default=Path("studies/precheck_ledger.toml"), help="names already run")


def _missing_fills(coins: Mapping[str, CoinData], day: date, hours: Sequence[int], delay: int) -> list[str]:
    btc = coins.get("PF_XBTUSD.KRAKEN")
    if btc is None:
        return ["PF_XBTUSD.KRAKEN"]
    return [f"{day} {h:02d}:00" for h in hours if cut_ns(day, h) + delay not in btc.minutes]


def run_precheck(settings: LabSettings, args: argparse.Namespace) -> int:
    """``lab book precheck`` (module docstring)."""
    try:
        pre = load_precheck(args.file)
        spent = set(load_ledger(args.ledger))
    except PrecheckFileError as exc:
        for problem in exc.problems:
            print(f"lab book precheck: {problem}", file=sys.stderr)
        return EXIT_USAGE
    again = [n for n in pre.names() if n in spent]
    if again:
        print(f"lab book precheck: already run (in the ledger): {', '.join(again)}", file=sys.stderr)
        return EXIT_USAGE
    kraken = {coin: load_coin(candle_tree(settings.lab_catalog_dir, coin)) for coin in BOOK_COINS}
    hours = sorted({h for t in pre.trials for h in tranche_hours(t.cut_hour, t.tranches)} | set(pre.baseline_hours))
    missing = _missing_fills(kraken, pre.end, hours, FILL_DELAY_NS)
    binance = load_binance(Path(settings.lab_catalog_dir) / "binance")
    if not binance:
        missing.append("Binance closes (run lab book binance-fetch)")
    else:
        missing += _missing_fills(binance, pre.binance_to, [0], HOUR_NS)
    if missing:
        print(f"lab book precheck: no fill for {', '.join(missing)}; fetch the data first", file=sys.stderr)
        return EXIT_MISSING
    _DATA[KRAKEN], _DATA[BINANCE] = kraken, binance
    jobs = jobs_for(pre)
    with ProcessPoolExecutor(settings.lab_workers, mp_context=multiprocessing.get_context("fork")) as pool:
        rows = list(pool.map(run_job, jobs))
    kraken_rows = [r for r in rows if r.venue == KRAKEN]
    baselines = {r.cut_hour: r.figures() for r in kraken_rows if r.role == "baseline"}
    verdicts = precheck_verdicts(baselines, [r.figures() for r in kraken_rows if r.role != "baseline"])
    commit = os.environ.get("GIT_COMMIT", "unknown")
    out = Path(settings.lab_reports_dir) / "precheck" / pre.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "precheck.md").write_text(render(pre, rows, verdicts, commit), encoding="utf-8")
    body = {
        "precheck": pre.name,
        "commit": commit,
        "rows": [{k: _json(v) for k, v in asdict(r).items()} for r in rows],
        "verdicts": [{"family": v.family, "passed": v.passed, "reasons": list(v.reasons)} for v in verdicts],
    }
    (out / "results.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    (out / "precheck_ledger.toml").write_text(ledger_text([*sorted(spent), *pre.names()]), encoding="utf-8")
    for v in verdicts:
        print(f"{v.family}: {'PASS' if v.passed else 'FAIL'}")
    print(f"report {out / 'precheck.md'}; copy precheck_ledger.toml into studies/ and commit both")
    return 0
