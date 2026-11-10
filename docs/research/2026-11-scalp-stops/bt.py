"""Research backtester: bar signals, exits walked on 1 minute bars with a fixed stop and a trailing stop.

Costs match the repo's candle lab defaults: taker 5 bps, maker 2 bps, slippage 2 bps a side on market fills.
Pessimistic intrabar order, both ways: a 1 minute bar is checked against the stop as it stood before the
bar, and again against the stop after the bar's own best price moved it.
"""

from pathlib import Path

import numpy as np
from numba import njit

TAKER = 5e-4
MAKER = 2e-4
SLIP = 2e-4
DAY = 86400


def load(sym, d=Path(__file__).parent / "data"):
    a = np.load(d / f"{sym}.npy")
    t = a[:, 0].astype(np.int64)
    # fill missing minutes with flat bars at the previous close (no trades that minute)
    full = np.arange(t[0], t[-1] + 60, 60)
    idx = np.searchsorted(t, full, side="right") - 1
    present = t[idx] == full
    o, h, lo, c, v = (a[idx, k].copy() for k in range(1, 6))
    pc = a[idx, 4]
    for arr in (o, h, lo):
        arr[~present] = pc[~present]
    v[~present] = 0.0
    return full, o, h, lo, c, v


def resample(t, o, h, lo, c, v, n):
    """n minute bars aligned to the epoch; returns arrays plus the 1m index of each bar's last minute."""
    start = (-(t[0] // 60)) % n
    m = (len(t) - start) // n
    sl = slice(start, start + m * n)
    open_ = o[sl].reshape(m, n)[:, 0]
    high = h[sl].reshape(m, n).max(1)
    low = lo[sl].reshape(m, n).min(1)
    close = c[sl].reshape(m, n)[:, -1]
    volume = v[sl].reshape(m, n).sum(1)
    last = start + np.arange(m) * n + n - 1
    return open_, high, low, close, volume, last


@njit(cache=True)
def ema(x, n):
    out = np.empty_like(x)
    k = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + k * (x[i] - out[i - 1])
    return out


@njit(cache=True)
def atr(h, lo, c, n):
    out = np.full(len(c), np.nan)
    tr_sum = 0.0
    a = 0.0
    for i in range(1, len(c)):
        tr = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        if i <= n:
            tr_sum += tr
            if i == n:
                a = tr_sum / n
                out[i] = a
        else:
            a = (a * (n - 1) + tr) / n
            out[i] = a
    return out


@njit(cache=True)
def rolling_max(x, n):
    """Max of the previous n values, excluding the current one."""
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        m = x[i - n]
        for j in range(i - n + 1, i):
            if x[j] > m:
                m = x[j]
        out[i] = m
    return out


@njit(cache=True)
def rolling_min(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        m = x[i - n]
        for j in range(i - n + 1, i):
            if x[j] < m:
                m = x[j]
        out[i] = m
    return out


@njit(cache=True)
def rsi(c, n):
    out = np.full(len(c), np.nan)
    g = 0.0
    d = 0.0
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        up = ch if ch > 0 else 0.0
        dn = -ch if ch < 0 else 0.0
        if i <= n:
            g += up / n
            d += dn / n
        else:
            g = (g * (n - 1) + up) / n
            d = (d * (n - 1) + dn) / n
        if i >= n:
            out[i] = 100.0 if d == 0 else 100.0 - 100.0 / (1.0 + g / d)
    return out


@njit(cache=True)
def rolling_mean(x, n):
    out = np.full(len(x), np.nan)
    s = 0.0
    for i in range(len(x)):
        s += x[i]
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


@njit(cache=True)
def rolling_std(x, n):
    out = np.full(len(x), np.nan)
    s = 0.0
    s2 = 0.0
    for i in range(len(x)):
        s += x[i]
        s2 += x[i] * x[i]
        if i >= n:
            s -= x[i - n]
            s2 -= x[i - n] * x[i - n]
        if i >= n - 1:
            var = s2 / n - (s / n) ** 2
            out[i] = np.sqrt(var) if var > 0 else 0.0
    return out


@njit(cache=True)
def simulate(
    t,
    o,
    h,
    lo,
    c,
    sig_i,
    sig_side,
    sig_atr,
    sl_atr,
    trail_atr,
    trail_start_atr,
    be_atr,
    tp_atr,
    max_min,
    cooldown_min,
    min_stop_bps,
    taker,
    maker,
    slip,
):
    """One position at a time. A signal known at the close of 1m bar sig_i enters at the open of bar sig_i + 1.

    sl_atr           initial stop distance in ATR (floored at min_stop_bps of price)
    trail_atr        trailing distance in ATR behind the best price (0 = no trail)
    trail_start_atr  the trail only starts once the open profit reached this many ATR
    be_atr           move the stop to the entry once the open profit reached this many ATR (0 = off)
    tp_atr           resting take profit in ATR (0 = off), a maker fill, needs a trade through the price
    Returns rows: entry time, exit time, side, net return, gross return, exit reason, risk (stop distance / entry).
    Reasons: 1 stop (never moved), 2 trailed or break even stop, 3 take profit, 4 time stop.
    """
    n = len(c)
    out = np.empty((len(sig_i), 7))
    k = 0
    free_at = 0
    for s in range(len(sig_i)):
        i = sig_i[s] + 1
        if i >= n - 1 or i < free_at:
            continue
        a = sig_atr[s]
        if not (a > 0):
            continue
        side = sig_side[s]
        entry = o[i] * (1 + side * slip)
        dist = max(sl_atr * a, min_stop_bps * 1e-4 * entry)
        stop = entry - side * dist
        tp = entry + side * tp_atr * a if tp_atr > 0 else 0.0
        best = entry
        moved = False
        reason = 0
        px = 0.0
        j = i
        end = min(n - 1, i + max_min)
        while j <= end:
            # gap through the stop at the open
            if j > i and side * (o[j] - stop) <= 0:
                px = o[j] * (1 - side * slip)
                reason = 2 if moved else 1
                break
            if (side > 0 and lo[j] <= stop) or (side < 0 and h[j] >= stop):
                px = stop * (1 - side * slip)
                reason = 2 if moved else 1
                break
            if tp_atr > 0 and ((side > 0 and h[j] > tp) or (side < 0 and lo[j] < tp)):
                px = tp
                reason = 3
                break
            ext = h[j] if side > 0 else lo[j]
            if side * (ext - best) > 0:
                best = ext
            gain = side * (best - entry)
            if be_atr > 0 and gain >= be_atr * a and side * (entry - stop) > 0:
                stop = entry
                moved = True
            if trail_atr > 0 and gain >= trail_start_atr * a:
                ns = best - side * trail_atr * a
                if side * (ns - stop) > 0:
                    stop = ns
                    moved = True
            # the stop moved inside this bar: if the bar's adverse extreme reaches the new stop, count it as hit
            if (
                moved
                and ((side > 0 and lo[j] <= stop) or (side < 0 and h[j] >= stop))
                and side * (stop - (entry - side * dist)) > 0
            ):
                px = stop * (1 - side * slip)
                reason = 2
                break
            j += 1
        if reason == 0:
            j = end
            px = c[j] * (1 - side * slip)
            reason = 4
        gross = side * (px - entry) / entry
        fee = taker + (maker if reason == 3 else taker)
        raw = side * ((px / (1 - side * slip) if reason != 3 else px) - o[i]) / o[i]
        out[k, 0] = t[i]
        out[k, 1] = t[j]
        out[k, 2] = side
        out[k, 3] = gross - fee
        out[k, 4] = raw
        out[k, 5] = reason
        out[k, 6] = dist / entry
        k += 1
        free_at = j + 1 + cooldown_min
    return out[:k]


def stats(tr, t0, t1, risk_frac=None):
    """Summary of trades with entry time in [t0, t1). Returns a dict, bps units for per trade figures."""
    m = (tr[:, 0] >= t0) & (tr[:, 0] < t1)
    x = tr[m]
    days = (t1 - t0) / DAY
    if len(x) < 5:
        return dict(
            n=len(x),
            tpd=len(x) / days,
            net=np.nan,
            gross=np.nan,
            cost=np.nan,
            win=np.nan,
            t=np.nan,
            sharpe=np.nan,
            tot=np.nan,
            mdd=np.nan,
            r=np.nan,
        )
    net = x[:, 3]
    # daily P&low series at fixed fractional risk when risk_frac is given, else unit notional
    w = (risk_frac / x[:, 6]) if risk_frac is not None else np.ones(len(x))
    pnl = net * w
    d = ((x[:, 1] - t0) // DAY).astype(np.int64)
    daily = np.bincount(np.clip(d, 0, int(days)), weights=pnl, minlength=int(days) + 1)
    eq = np.cumsum(daily)
    mdd = float((np.maximum.accumulate(eq) - eq).max())
    sd = daily.std()
    return dict(
        n=len(x),
        tpd=len(x) / days,
        net=net.mean() * 1e4,
        gross=x[:, 4].mean() * 1e4,
        cost=(x[:, 4] - net).mean() * 1e4,
        win=(net > 0).mean(),
        t=net.mean() / (net.std() / np.sqrt(len(x))),
        sharpe=daily.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
        tot=pnl.sum(),
        mdd=mdd,
        r=(net / x[:, 6]).mean(),
    )
