"""Print every table in README.md from the candles under data/ (run fetch.py first).

A float screen on 1 minute candles, not lab evidence: no order book, no queue, no funding. Costs are the
candle lab's defaults (taker 5 bps, maker 2 bps, 2 bps slippage on every market fill).
"""

from datetime import UTC, datetime

import bt
import numpy as np
import sigs
from numba import njit

COINS = ["PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "PF_XRPUSD", "PF_DOGEUSD", "PF_LINKUSD"]


def _ts(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


DEV = (_ts(2025, 1), _ts(2026, 5))  # every choice below was made on this range
HOLD = (_ts(2026, 5), _ts(2026, 11, 10))  # read once, for the shortlist only
DATA = {c: bt.load(c) for c in COINS}
_BARS: dict[tuple[str, int], sigs.Bars] = {}


def bars(coin, n):
    if (coin, n) not in _BARS:
        _BARS[(coin, n)] = sigs.Bars(DATA[coin], n)
    return _BARS[(coin, n)]


def sim(coin, signals, sl=3, tr=0, ts=0, be=0, tp=0, mx=300):
    """Market entry trades for one coin, tagged with the coin's index in the last column."""
    t, o, h, lo, c, _ = DATA[coin]
    trades = bt.simulate(
        t,
        o,
        h,
        lo,
        c,
        *signals,
        float(sl),
        float(tr),
        float(ts),
        float(be),
        float(tp),
        int(mx),
        0,
        0.0,
        bt.TAKER,
        bt.MAKER,
        bt.SLIP,
    )
    return np.column_stack([trades, np.full(len(trades), COINS.index(coin))])


def pooled(signal_fn, n, coins=COINS, **exits):
    return np.concatenate([sim(c, sigs.to_signals(bars(c, n), *signal_fn(bars(c, n))), **exits) for c in coins])


def line(name, x, span):
    y = x[(x[:, 0] >= span[0]) & (x[:, 0] < span[1])]
    net, days = y[:, 3], (span[1] - span[0]) / bt.DAY
    t_stat = net.mean() / net.std() * np.sqrt(len(y))
    hold = np.median(y[:, 1] - y[:, 0]) / 60
    print(
        f"| {name} | {len(y)} | {len(y) / days:.1f} | {hold:.0f} | {y[:, 4].mean() * 1e4:.1f} | "
        f"{net.mean() * 1e4:.1f} | {(net > 0).mean():.0%} | {t_stat:.1f} |"
    )


HEAD = (
    "| {} | trades | per day | median hold (min) | gross bps | net bps | wins | t |\n|---|---|---|---|---|---|---|---|"
)

FAMILIES = {
    "Donchian 20 breakout, EMA 200 side": lambda b: sigs.donchian(b, 20, 200),
    "Volume impulse (3 ATR bar, 3x volume), follow": lambda b: sigs.impulse(b, 3.0, 3.0),
    "Volume impulse (3 ATR bar, 3x volume), fade": lambda b: sigs.impulse_fade(b, 3.0, 3.0),
    "RSI(2) 5/95 with the EMA 200 trend": lambda b: sigs.rsi_revert(b, 2, 5, 95, 200),
    "EMA 20/100 pullback": lambda b: sigs.ema_pullback(b, 20, 100),
    "Bollinger squeeze breakout": lambda b: sigs.squeeze(b),
}
EXITS = {
    "stop 3 ATR, time stop 60 bars": lambda n: dict(sl=3, mx=60 * n),
    "stop 2 ATR, trail 2 ATR": lambda n: dict(sl=2, tr=2, mx=240 * n),
    "stop 3 ATR, trail 5 ATR": lambda n: dict(sl=3, tr=5, mx=240 * n),
}


def table_families():
    print("\n## 1. Market entry families, development range, six coins pooled\n")
    for n in (1, 5, 15):
        print(f"\n### {n} minute bars\n\n" + HEAD.format("entry, exit"))
        for fname, fn in FAMILIES.items():
            for ename, ex in EXITS.items():
                line(f"{fname}; {ename}", pooled(fn, n, **ex(n)), DEV)


def table_shortlist():
    print("\n## 2. Shortlist: development against the held out months\n\n" + HEAD.format("candidate, range"))
    imp = FAMILIES["Volume impulse (3 ATR bar, 3x volume), follow"]
    shortlist = {
        "15m impulse; stop 3 ATR, time 15 h": dict(sl=3, mx=900),
        "15m impulse; stop 3 ATR, maker target 6 ATR": dict(sl=3, tp=6, mx=900),
        "15m impulse; stop 3 ATR, trail 5 ATR": dict(sl=3, tr=5, mx=1800),
        "15m impulse; stop 3 ATR, trail 2 ATR once 4 ATR ahead": dict(sl=3, tr=2, ts=4, mx=1800),
    }
    for name, ex in shortlist.items():
        x = pooled(imp, 15, **ex)
        line(name + ", development", x, DEV)
        line(name + ", held out", x, HOLD)
    lead = np.concatenate([sim(c, lead_signals(c, 2.0, 0.3), sl=2, tr=2, mx=30) for c in COINS[1:]])
    line("1m BTC lead on five alts; stop 2 ATR, trail 2 ATR, development", lead, DEV)
    line("1m BTC lead on five alts; stop 2 ATR, trail 2 ATR, held out", lead, HOLD)


def lead_masks(coin, k, lag):
    """A BTC 1 minute close to close move of k ATR or more while the alt moved under `lag` times as far."""
    btc, alt = bars("PF_XBTUSD", 1), bars(coin, 1)
    prev_atr = np.concatenate(([np.nan], btc.atr[:-1]))
    move = np.concatenate(([0.0], np.diff(btc.close)))
    btc_ret = move / btc.close
    alt_ret = np.concatenate(([0.0], np.diff(alt.close))) / alt.close
    traded = alt.volume > 0
    return (move >= k * prev_atr) & (alt_ret < lag * btc_ret) & traded, (
        (move <= -k * prev_atr) & (alt_ret > lag * btc_ret) & traded
    )


def lead_signals(coin, k, lag):
    return sigs.to_signals(bars(coin, 1), *lead_masks(coin, k, lag))


def table_lead():
    print("\n## 3. BTC lead: the alt's raw move in bps after the signal, by entry delay (whole range)\n")
    print("| coin | signals per day | enter next open: 1m / 5m / 20m | enter 1 minute later: 1m / 5m / 20m |")
    print("|---|---|---|---|")
    days = (HOLD[1] - DEV[0]) / bt.DAY
    for coin in COINS[1:]:
        _, o, _, _, c, _ = DATA[coin]
        idx, side, _ = lead_signals(coin, 2.0, 0.3)
        cells = []
        for delay in (0, 1):
            i = idx + 1 + delay
            keep = i + 20 < len(c)
            i, s = i[keep], side[keep]
            cells.append(" / ".join(f"{(s * (c[i + hz - 1] - o[i]) / o[i]).mean() * 1e4:.1f}" for hz in (1, 5, 20)))
        print(f"| {coin} | {len(idx) / days:.1f} | {cells[0]} | {cells[1]} |")


@njit(cache=True)
def maker_fade(t, o, h, lo, c, last, nb, ema, atr, k, sl_atr, trail_atr, max_min, taker, maker, slip):
    """Rest a bid k ATR under the EMA and an offer k ATR over it for one bar; target the EMA with a maker limit.

    A limit fills only when a 1 minute bar trades through its price. The stop is a stop market; it can be hit
    in the bar that filled the entry. Rows as ``bt.simulate``.
    """
    out = np.empty((len(last), 7))
    n, q, free = len(c), 0, 0
    for b in range(len(last) - 1):
        i0 = last[b] + 1
        a = atr[b]
        if i0 < free or not (a > 0):
            continue
        bid, ask = ema[b] - k * a, ema[b] + k * a
        if c[last[b]] <= bid or c[last[b]] >= ask:
            continue  # the order would cross the market, so it is not a maker order
        side, i, entry = 0.0, -1, 0.0
        for j in range(i0, min(i0 + nb, n)):
            if lo[j] < bid:
                side, i, entry = 1.0, j, bid
                break
            if h[j] > ask:
                side, i, entry = -1.0, j, ask
                break
        if i < 0:
            continue
        first_stop = entry - side * sl_atr * a
        stop, target, best, reason, px = first_stop, ema[b], entry, 0, 0.0
        end = min(n - 1, i + max_min)
        j = i
        while j <= end:
            if j > i and side * (o[j] - stop) <= 0:
                px, reason = o[j] * (1 - side * slip), 1
                break
            if (side > 0 and lo[j] <= stop) or (side < 0 and h[j] >= stop):
                px, reason = stop * (1 - side * slip), 1
                break
            if j > i and ((side > 0 and h[j] > target) or (side < 0 and lo[j] < target)):
                px, reason = target, 3
                break
            if j > i and trail_atr > 0:
                ext = h[j] if side > 0 else lo[j]
                if side * (ext - best) > 0:
                    best = ext
                moved = best - side * trail_atr * a
                if side * (moved - stop) > 0:
                    stop = moved
                    if (side > 0 and lo[j] <= stop) or (side < 0 and h[j] >= stop):
                        px, reason = stop * (1 - side * slip), 2
                        break
            j += 1
        if reason == 0:
            j = end
            px, reason = c[j] * (1 - side * slip), 4
        fee = maker + (maker if reason == 3 else taker)
        mid_px = px if reason == 3 else px / (1 - side * slip)
        out[q, 0], out[q, 1], out[q, 2] = t[i], t[j], side
        out[q, 3] = side * (px - entry) / entry - fee
        out[q, 4] = side * (mid_px - entry) / entry
        out[q, 5], out[q, 6] = reason, sl_atr * a / entry
        q += 1
        free = j + 1
    return out[:q]


def table_maker():
    print("\n## 4. Maker fade around the EMA 20, development range, six coins pooled\n\n" + HEAD.format("setup"))
    for n in (1, 5):
        for k in (2, 3, 4):
            for trail in (0, 2):
                rows = []
                for coin in COINS:
                    t, o, h, lo, c, _ = DATA[coin]
                    b = bars(coin, n)
                    rows.append(
                        maker_fade(
                            t,
                            o,
                            h,
                            lo,
                            c,
                            b.last.astype(np.int64),
                            n,
                            b.ema(20),
                            b.atr,
                            float(k),
                            3.0,
                            float(trail),
                            60 * n,
                            bt.TAKER,
                            bt.MAKER,
                            bt.SLIP,
                        )
                    )
                exit_name = "stop 3 ATR" if trail == 0 else "stop 3 ATR, trail 2 ATR"
                line(f"{n}m, limits {k} ATR from the EMA; {exit_name}", np.concatenate(rows), DEV)


if __name__ == "__main__":
    table_families()
    table_shortlist()
    table_lead()
    table_maker()
