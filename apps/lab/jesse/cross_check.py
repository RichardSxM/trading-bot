"""The Jesse cross check of single coin legs (spec 0033 AC-11), run in the ``jesse`` Compose profile.

    docker compose --profile jesse run --rm --no-deps jesse python /jesse/cross_check.py \\
        /lab-reports/jesse/PF_XBTUSD_2024-01-01_2024-06-30 /lab-reports/jesse/PF_ETHUSD_2024-01-01_2024-06-30

Each folder comes from ``lab book jesse-export`` and holds ``candles.csv`` (our candles tree as Jesse rows) and
``reference.json`` (the pure replay of the same single coin book). The script backtests the 30 day sign rule at
gross 1 with ``jesse.research.backtest`` over those candles, on the daily timeframe with the same fee, and
compares: the side must match on every traded day, and the total net return must be within ``MAX_GAP_PP``.

The two engines differ in ways this check accepts: Jesse fills at the daily close (00:00 UTC), the pure
replay five minutes later at the 00:05 close; Jesse sizes from its cash balance in floats, the pure replay from
marked equity rounded to the size step; neither books funding. Jesse closes the last position at its end, so
it is compared with the reference's ``return_closed``.

Only the standard library is imported at the top, so the comparison can be tested without Jesse; Jesse and
numpy are imported inside ``run_jesse``. Writes ``jesse.json`` beside the inputs. Exit 0 pass, 1 fail, 64 bad
input.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

MAX_GAP_PP: Final = Decimal(2)
EXCHANGE: Final = "Bybit USDT Perpetual"  # a label Jesse knows; the fee comes from the config, not the venue
LEVERAGE: Final = 2  # room for the margin check at gross 1 plus fees
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 64


@dataclass(frozen=True)
class Trade:
    """One Jesse trade: its side (1 long, -1 short), Jesse's open and close stamps in ms (the fill times, at the
    close of a daily candle), and whether Jesse closed it only because the backtest ended."""

    side: int
    opened_ms: int
    closed_ms: int
    at_end: bool


@dataclass(frozen=True)
class Verdict:
    """The comparison of one coin: pass, days compared, the days whose side differs, both returns, the gap."""

    passed: bool
    days: int
    side_misses: list[tuple[str, int, int]]
    reference_return: Decimal
    jesse_return: Decimal
    gap_pp: Decimal


def _day(ms: int) -> date:
    return datetime.fromtimestamp(ms // 1000, UTC).date()


def daily_sides(trades: Sequence[Trade], first: date, last: date) -> dict[date, int]:
    """Jesse's side on each pure replay day from ``first`` to ``last``.

    Jesse decides at the close of daily candle ``D`` and stamps the fill with that time, 00:00 of ``D + 1``,
    which is the pure replay's trade on day ``D + 1``. So a trade stamped open on day ``O`` and closed on day
    ``C`` is held on days ``O`` to ``C - 1``, and to ``last`` when Jesse closed it only because the backtest ended.
    """
    sides = {first + timedelta(days=i): 0 for i in range((last - first).days + 1)}
    for trade in trades:
        held_from = _day(trade.opened_ms)
        held_to = last if trade.at_end else _day(trade.closed_ms) - timedelta(days=1)
        day = max(held_from, first)
        while day <= min(held_to, last):
            sides[day] = trade.side
            day += timedelta(days=1)
    return sides


def compare(reference: Mapping[str, Any], sides: Mapping[date, int], jesse_return: Decimal) -> Verdict:
    """The AC-11 pass rule: the same side on every reference day, and net returns within ``MAX_GAP_PP``."""
    misses: list[tuple[str, int, int]] = []
    for day_text, side, _equity, _traded in reference["days"]:
        other = sides.get(date.fromisoformat(day_text), 0)
        if other != side:
            misses.append((day_text, side, other))
    ref_return = Decimal(reference["return_closed"])
    gap = abs(jesse_return - ref_return) * 100
    return Verdict(not misses and gap <= MAX_GAP_PP, len(reference["days"]), misses, ref_return, jesse_return, gap)


def read_candles(path: Path) -> list[list[float]]:
    """The Jesse rows of ``candles.csv`` (open time ms, open, close, high, low, volume)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return [[float(v) for v in row] for row in csv.reader(fh)]


def run_jesse(rows: list[list[float]], reference: Mapping[str, Any]) -> tuple[list[Trade], Decimal, int]:
    """Backtest the sign rule with ``jesse.research.backtest``; the trades, the net return, and the trade count."""
    # Jesse reads ``.env`` from the working directory and then connects to its database; the research backtest
    # needs none, so run it from an empty folder (the entrypoint's .env lives in the project folder).
    os.chdir(tempfile.mkdtemp(prefix="jesse-research-"))
    import jesse.helpers as jh
    import numpy as np
    from jesse.research import backtest
    from jesse.strategies import Strategy

    inputs = reference["inputs"]
    lookback = int(inputs["lookback_days"])
    min_history = int(inputs["min_history_days"])
    first = date.fromisoformat(inputs["from"])
    last = date.fromisoformat(inputs["to"])

    class SignRule(Strategy):  # type: ignore[misc]  # Jesse has no type hints
        """The 30 day sign rule at gross 1: long above the close 30 days back, short below, flat when equal."""

        def _sign(self) -> int:
            closes = self.candles[:, 2]
            day = _day(int(self.current_candle[0]))
            if len(closes) < min_history or not first - timedelta(days=1) <= day <= last - timedelta(days=1):
                return 0
            change = closes[-1] / closes[-1 - lookback] - 1
            return 1 if change > 0 else -1 if change < 0 else 0

        def should_long(self) -> bool:
            return self._sign() == 1

        def should_short(self) -> bool:
            return self._sign() == -1

        def go_long(self) -> None:
            self.buy = self.balance / self.price, self.price

        def go_short(self) -> None:
            self.sell = self.balance / self.price, self.price

        def update_position(self) -> None:
            if self._sign() != (1 if self.is_long else -1):
                self.liquidate()

        def should_cancel_entry(self) -> bool:
            return False

    symbol = "COIN-USDT"  # a label; the candles are the coin's own
    config = {
        "starting_balance": float(inputs["equity"]),
        "fee": float(inputs["cost"]),
        "type": "futures",
        "futures_leverage": LEVERAGE,
        "futures_leverage_mode": "cross",
        "exchange": EXCHANGE,
        "warm_up_candles": 0,
    }
    routes = [{"exchange": EXCHANGE, "strategy": SignRule, "symbol": symbol, "timeframe": "1D"}]
    candles = {jh.key(EXCHANGE, symbol): {"exchange": EXCHANGE, "symbol": symbol, "candles": np.array(rows)}}
    result = backtest(config, routes, [], candles)
    raw = sorted(result.get("trades", []), key=lambda t: int(t["opened_at"]))
    # Only the newest trade can be one Jesse closed because the backtest ended, stamped at the close of the last
    # daily candle (the day after its open). A flip there also closes a trade at that time, but not the newest.
    end_day = _day(int(rows[-1][0])) + timedelta(days=1)
    trades = [
        Trade(
            1 if t["type"] == "long" else -1,
            int(t["opened_at"]),
            int(t["closed_at"]),
            i == len(raw) - 1 and _day(int(t["closed_at"])) >= end_day,
        )
        for i, t in enumerate(raw)
    ]
    finishing = Decimal(str(result["metrics"]["finishing_balance"]))
    return trades, finishing / Decimal(inputs["equity"]) - 1, len(trades)


def check(folder: Path) -> Verdict:
    """Run the check for one ``lab book jesse-export`` folder and write ``jesse.json`` beside it."""
    reference = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    rows = read_candles(folder / "candles.csv")
    trades, jesse_return, count = run_jesse(rows, reference)
    first = date.fromisoformat(reference["inputs"]["from"])
    last = date.fromisoformat(reference["inputs"]["to"])
    verdict = compare(reference, daily_sides(trades, first, last), jesse_return)
    out = {
        "coin": reference["inputs"]["coin"],
        "passed": verdict.passed,
        "days": verdict.days,
        "side_misses": verdict.side_misses,
        "reference_return": str(verdict.reference_return),
        "reference_trades": reference["trades"],
        "jesse_return": str(verdict.jesse_return),
        "jesse_trades": count,
        "gap_pp": str(verdict.gap_pp),
        "max_gap_pp": str(MAX_GAP_PP),
    }
    (folder / "jesse.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return verdict


def main(argv: list[str] | None = None) -> int:
    """Check every folder given; exit 1 when any coin fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folders", nargs="+", type=Path, help="lab book jesse-export folders")
    args = parser.parse_args(argv)
    args.folders = [f.resolve() for f in args.folders]  # run_jesse changes the working directory
    missing = [
        str(f) for f in args.folders if not (f / "reference.json").is_file() or not (f / "candles.csv").is_file()
    ]
    if missing:
        print(f"cross_check: no jesse-export inputs in {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE
    passed = True
    for folder in args.folders:
        v = check(folder)
        passed = passed and v.passed
        print(
            f"{folder.name:<40} {'PASS' if v.passed else 'FAIL'}  days {v.days}  side misses {len(v.side_misses)}  "
            f"return pure {v.reference_return:.4%} jesse {v.jesse_return:.4%}  gap {v.gap_pp:.3f} pp"
        )
    return 0 if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
