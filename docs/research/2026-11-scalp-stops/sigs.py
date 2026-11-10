"""Entry signal families on n minute bars. Each returns (bar index mask long, mask short); ATR comes separately."""

import bt
import numpy as np


class Bars:
    def __init__(self, data, n):
        t, o, h, lo, c, v = data
        self.n = n
        self.open_, self.high, self.low, self.close, self.volume, self.last = bt.resample(t, o, h, lo, c, v, n)
        self.atr = bt.atr(self.high, self.low, self.close, 14)
        self._cache = {}

    def ema(self, p):
        k = ("ema", p)
        if k not in self._cache:
            self._cache[k] = bt.ema(self.close, p)
        return self._cache[k]

    def hi(self, p):
        k = ("hi", p)
        if k not in self._cache:
            self._cache[k] = bt.rolling_max(self.high, p)
        return self._cache[k]

    def lo(self, p):
        k = ("lo", p)
        if k not in self._cache:
            self._cache[k] = bt.rolling_min(self.low, p)
        return self._cache[k]

    def rsi(self, p):
        k = ("rsi", p)
        if k not in self._cache:
            self._cache[k] = bt.rsi(self.close, p)
        return self._cache[k]

    def vmean(self, p):
        k = ("vm", p)
        if k not in self._cache:
            m = bt.rolling_mean(self.volume, p)
            self._cache[k] = np.concatenate(([np.nan], m[:-1]))  # previous p bars, current excluded
        return self._cache[k]

    def atr_bps(self):
        return self.atr / self.close * 1e4


def donchian(b, entry=20, ema_p=200, vol_floor_bps=0.0):
    """Close breaks the previous `entry` bars' high or low, on the EMA's side (ema_p 0 = no filter)."""
    close = b.close
    lg = close > b.hi(entry)
    sh = close < b.lo(entry)
    if ema_p:
        e = b.ema(ema_p)
        lg &= close > e
        sh &= close < e
    if vol_floor_bps:
        ok = b.atr_bps() >= vol_floor_bps
        lg &= ok
        sh &= ok
    return lg, sh


def impulse(b, k=2.0, vol_mult=2.0, ema_p=0):
    """Momentum ignition: a bar whose close to close move is k ATR or more, on volume vol_mult times normal."""
    close = b.close
    prev_atr = np.concatenate(([np.nan], b.atr[:-1]))
    ch = np.concatenate(([0.0], np.diff(close)))
    big = b.volume >= vol_mult * b.vmean(50)
    lg = (ch >= k * prev_atr) & big
    sh = (ch <= -k * prev_atr) & big
    if ema_p:
        e = b.ema(ema_p)
        lg &= close > e
        sh &= close < e
    return lg, sh


def impulse_fade(b, k=2.5, vol_mult=3.0):
    """Capitulation reversal: fade a k ATR bar on a volume spike."""
    lg, sh = impulse(b, k, vol_mult, 0)
    return sh, lg


def rsi_revert(b, p=2, lo=5.0, hi=95.0, ema_p=200):
    """Connors style: with the trend (close above the EMA), buy an oversold short RSI; mirrored for shorts."""
    close = b.close
    r = b.rsi(p)
    e = b.ema(ema_p)
    return (r < lo) & (close > e), (r > hi) & (close < e)


def ema_pullback(b, fast=20, slow=100):
    """Trend by EMA order; the bar dips to the fast EMA and closes back above it (mirrored)."""
    close, low, high = b.close, b.low, b.high
    f, s = b.ema(fast), b.ema(slow)
    pf = np.concatenate(([np.nan], f[:-1]))
    pc = np.concatenate(([np.nan], close[:-1]))
    lg = (f > s) & (low <= f) & (close > f) & (pc > pf) & (close > b.open_)
    sh = (f < s) & (high >= f) & (close < f) & (pc < pf) & (close < b.open_)
    return lg, sh


def squeeze(b, p=20, width_q=0.2, look=500):
    """Bollinger squeeze breakout: band width in the lowest width_q of the last `look` bars, close outside."""
    close = b.close
    m = bt.rolling_mean(close, p)
    s = bt.rolling_std(close, p)
    width = 4 * s / m
    # rank of the previous bar's width against a rolling window, by a cheap strided quantile
    q = np.full(len(close), np.nan)
    step = 50
    for i in range(look, len(close), step):
        q[i : i + step] = np.nanquantile(width[i - look : i], width_q)
    pw = np.concatenate(([np.nan], width[:-1]))
    tight = pw <= q
    return tight & (close > m + 2 * s), tight & (close < m - 2 * s)


def to_signals(b, lg, sh):
    """Bar masks to 1m signal arrays (index of the bar's last minute, side, ATR at that bar)."""
    lg = np.nan_to_num(lg, nan=0).astype(bool)
    sh = np.nan_to_num(sh, nan=0).astype(bool)
    idx = np.nonzero(lg | sh)[0]
    side = np.where(lg[idx], 1.0, -1.0)
    return b.last[idx].astype(np.int64), side, b.atr[idx]
