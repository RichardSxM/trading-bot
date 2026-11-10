"""Replay ``trend_portfolio`` on hourly candles with ways of taking profit daily (README section 6).

A float screen, not lab evidence. The plan follows ``trading_core.strategy.trend_portfolio`` (30 day sign,
30 day volatility scaling, a gross cap), every order costs 7 bps of its notional, funding is left out, and
exits are checked on hourly closes. Run ``fetch.py --hourly`` for the twelve coins first.
"""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

COINS = ["XBT", "ETH", "SOL", "XRP", "LINK", "UNI", "ZEC", "TRX", "DOGE", "QNT", "HYPE", "ADA"]
HOUR, DAY, COST = 3600, 86400, 7e-4


def ts(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


GRID = np.arange(ts(2023, 9), ts(2026, 11, 10), HOUR)


def _series(sym):
    a = np.load(Path(__file__).parent / "data" / "h1" / f"PF_{sym}USD.npy")
    idx = np.searchsorted(a[:, 0], GRID, side="right") - 1
    listed = idx >= 0
    row = np.clip(idx, 0, None)
    present = listed & (a[row, 0] == GRID)
    opens = np.where(present, a[row, 1], np.where(listed, a[row, 4], np.nan))
    closes = np.where(listed, a[row, 4], np.nan)
    return opens, closes


OPEN = np.column_stack([_series(s)[0] for s in COINS])
CLOSE = np.column_stack([_series(s)[1] for s in COINS])
N = len(COINS)
DAY_START = np.nonzero(GRID % DAY == 0)[0]
DAILY = CLOSE[DAY_START[1:] - 1]  # row d: the close of day d


def plan(day, vol_target, cap):
    """Weights from the daily closes before day index ``day``, as ``portfolio_plan`` computes them."""
    weights, eligible = np.zeros(N), 0
    for k in range(N):
        s = DAILY[:day, k]
        s = s[~np.isnan(s)]
        if len(s) < 120:
            continue
        eligible += 1
        window = s[-31:]
        vol = (window[1:] / window[:-1] - 1).std(ddof=1)
        weights[k] = 0.0 if vol == 0 else np.sign(s[-1] / s[-31] - 1) * vol_target / vol
    if eligible == 0:
        return weights
    weights /= eligible
    gross = np.abs(weights).sum()
    return weights * (cap / gross if gross > cap else 1.0)


def run(start, end, vol_target, cap, rebalance="weekly", winners=None, take=None, stop=None):
    """One replay. ``winners``: what the daily run does with a coin in profit ("flat" closes it until Monday,
    "reopen" closes and reopens it). ``take`` and ``stop``: the day's P&L as a share of the open book at which
    every coin closes until the next daily run."""
    equity, fees, exits = 1.0, 0.0, 0
    qty, entry, blocked = np.zeros(N), np.zeros(N), np.zeros(N, bool)
    flat, first, day_equity, book = False, True, 1.0, 0.0
    curve = []

    def trade(k, new_qty, px):
        nonlocal equity, fees
        delta = new_qty - qty[k]
        fee = abs(delta) * px * COST
        equity -= fee
        fees += fee
        if qty[k] == 0 or np.sign(new_qty) != np.sign(qty[k]):
            entry[k] = px
        elif abs(new_qty) > abs(qty[k]):
            entry[k] = (entry[k] * abs(qty[k]) + px * abs(delta)) / abs(new_qty)
        qty[k] = new_qty

    i0, i1 = np.searchsorted(GRID, start), np.searchsorted(GRID, end)
    prev = np.nan_to_num(CLOSE[i0 - 1])
    for i in range(i0, i1):
        if GRID[i] % DAY == 0:
            px = np.where(np.isnan(OPEN[i]), prev, OPEN[i])
            equity += float(np.sum(qty * (px - prev)))
            prev = px
            weights = plan(int(np.searchsorted(DAY_START, i)), vol_target, cap)
            monday = datetime.fromtimestamp(int(GRID[i]), UTC).weekday() == 0
            full = first or monday or rebalance == "daily"
            if full:
                blocked[:] = False
            for k in range(N):
                if np.isnan(OPEN[i, k]):
                    continue
                target = weights[k] * equity / px[k]
                if full or flat:
                    if not blocked[k]:
                        trade(k, target, px[k])
                elif winners and qty[k] != 0 and qty[k] * (px[k] - entry[k]) > 0:
                    trade(k, 0.0, px[k])
                    exits += 1
                    if winners == "flat":
                        blocked[k] = True
                    else:
                        trade(k, target, px[k])
            flat, first, day_equity = False, False, equity
            book = float(np.sum(np.abs(qty) * px))
            curve.append(equity)
        px = np.where(np.isnan(CLOSE[i]), prev, CLOSE[i])
        equity += float(np.sum(qty * (px - prev)))
        prev = px
        day_pnl = equity - day_equity
        hit = (take is not None and day_pnl >= take * book) or (stop is not None and day_pnl <= -stop * book)
        if hit and np.any(qty != 0):
            for k in range(N):
                trade(k, 0.0, px[k])
            flat, exits = True, exits + 1
        if equity <= 0:
            break
    e = np.array(curve)
    r = e[1:] / e[:-1] - 1
    years = len(r) / 365
    return (
        (e[-1] / e[0]) ** (1 / years) - 1,
        r.mean() / r.std() * np.sqrt(365),
        (1 - e / np.maximum.accumulate(e)).max(),
        fees / years,
        exits / years,
    )


VARIANTS = [
    ("weekly rebalance only (today)", {}),
    ("rebalance every day", dict(rebalance="daily")),
    ("daily: close each coin in profit, reopen at once", dict(winners="reopen")),
    ("daily: close each coin in profit, flat until Monday", dict(winners="flat")),
    ("book take profit 3 % only", dict(take=0.03)),
    ("book stop 3 % only", dict(stop=0.03)),
    ("book take profit and stop 2 %", dict(take=0.02, stop=0.02)),
    ("book take profit and stop 3 %", dict(take=0.03, stop=0.03)),
    ("book take profit and stop 4 %", dict(take=0.04, stop=0.04)),
    ("book take profit and stop 5 %", dict(take=0.05, stop=0.05)),
]

if __name__ == "__main__":
    whole, split = (ts(2024, 1), ts(2026, 11, 9)), ts(2025, 6)
    for label, vol_target, cap in (
        ("the tested sizing (2 % volatility target, 2x cap)", 0.02, 2.0),
        ("the demo's volatility target at a 2.5x cap", 0.155, 2.5),
        ("the demo sizing (15.5 % volatility target, 5x cap)", 0.155, 5.0),
    ):
        print(f"\n### {label[0].upper()}{label[1:]}\n")
        print(
            "| variant | return a year | Sharpe | worst drop | fees a year | exits a year | Sharpe to 2025-05 | after |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for name, kw in VARIANTS:
            ret, sharpe, drop, fees, exits = run(*whole, vol_target, cap, **kw)
            early = run(whole[0], split, vol_target, cap, **kw)[1]
            late = run(split, whole[1], vol_target, cap, **kw)[1]
            print(
                f"| {name} | {ret:+.0%} | {sharpe:.2f} | {drop:.0%} | {fees:.0%} | {exits:.0f} | "
                f"{early:.2f} | {late:.2f} |"
            )
