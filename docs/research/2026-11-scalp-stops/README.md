# Scalping with a stop loss and a trailing stop: what the candles say

**Date**: 2026-11-10
**Status**: research note, no strategy built. A float screen on 1 minute candles, not lab or gate evidence.

## Answer

No scalping strategy tested here makes money after fees, with or without a trailing stop. That agrees with
every scalping verdict already recorded in specs 0017, 0021, 0025, and 0026.

- A market order round trip costs about 14 bps (5 bps taker fee and 2 bps slippage on each side). A typical
  1 minute bar is 5 to 8 bps tall and a 5 minute ATR is 14 to 22 bps. In the tables below no entry signal on
  1 to 5 minute bars earned more than 12 bps a trade before costs, and most earned 0 to 3. The strongest
  setting seen anywhere on those bars earned 15, which paid the cost and nothing more.
- A trailing stop never turned a losing setup into a winning one. A tight trail (2 ATR) is hit by normal
  noise: on 1 and 5 minute bars wins fall to 10 to 28 % and the trade ends in minutes with the full cost
  paid. On the one setup with a positive development result, the trail cut it from +14.8 bps to +7.3
  (5 ATR) or −6.1 (2 ATR).
- A take profit does not rescue the short bars either (table 5). All 72 setups on 1 and 5 minute bars, with a
  target at 1, 2, or 3 ATR and a stop at 1.5 or 3 ATR, lost 8.5 to 14.6 bps a trade. A close target lifts the
  win rate (up to 72 % on 5 minute bars) without lifting the result: the wins are small and each one still
  pays the fees. On 1 minute bars a 1 ATR target is about 6 bps, less than the round trip's cost, so even a
  trade that reaches its target loses.
- The one candidate that looked alive, a 15 minute volume impulse, earned +15 bps a trade after costs on the
  development range and lost 3 to 17 bps a trade on the held out months, depending on the exit.
- One effect is real and stable: after a sharp 1 minute BTC move, a thin altcoin that has not moved yet catches
  up (LINK about 5 bps, DOGE about 3.5). Most of it is gone one minute later, and it is a third of the cost.

## The best option

If one strategy must be picked, it is the 15 minute volume impulse with a fixed 3 ATR stop and a resting
6 ATR target: about 2.4 trades a day over six coins, −3.2 bps a trade on the held out months (t −0.4, so not
told apart from zero). It is the least bad result, not an edge. Its trailing stop versions lost 11 to 17 bps.

The better use of a stop loss and a trailing stop is where they already are: `six_vote_trend` trails a 3 ATR
stop on 1 hour bars, where one round trip's cost is small next to the move.

What would change the answer: a round trip cost near 5 bps (not on offer at the base fee tier), or a signal
that candles cannot hold. The order book signals were tested in the replay lab and failed as well (specs
0017 child 0004 and 0021).

## Method

- Kraken Futures 1 minute trade candles, 2025-01-01 to 2026-11-09, for BTC, ETH, SOL, XRP, DOGE, and LINK
  (`fetch.py`). Minutes with no trade are filled flat at the last close.
- Development range 2025-01-01 to 2026-04-30. Every choice was made there. The held out range, 2026-05-01 to
  2026-11-09, was read once, for the shortlist in table 2. Table 5 was added later the same day; none of its setups was positive on the
  development range, so none was read on the held out range.
- Signals are decided at a bar close and entered at the next minute's open. Exits are walked on 1 minute bars
  (`bt.py`): a stop market at the stop price, or at the open when the bar gaps through it; a resting target
  fills only when a bar trades through it and pays the maker fee. A bar is checked against the stop as it stood
  before the bar and again after the bar's own best price moved it, so the trail never gets the benefit of
  the doubt.
- One position at a time per coin. Stops and trails are in multiples of the signal bar's 14 period ATR.
- About 300 settings were looked at in all (bar length, signal strength, stop, trail, target, time stop), so a
  t value near 1.5 on the development range means nothing by itself. Table 2 shows what it was worth.

Not modelled: the spread beyond 2 bps a side, queue position, funding, and outages. Each of these would
lower the results, not raise them. The BTC lead numbers are the least reliable: a thin coin's "next open" is
its first trade of the minute, which can be a stale price that a real order could not get.

To run it again: `python fetch.py PF_XBTUSD PF_ETHUSD PF_SOLUSD PF_XRPUSD PF_DOGEUSD PF_LINKUSD`, then
`python report.py` (Python 3.12 with `numpy` and `numba`). The tables below are that script's output.
"Gross" is the move from the entry open to the exit before slippage and fees, "net" is after both, and "t"
is the mean net divided by its standard error.

# Tables

## 1. Market entry families, development range, six coins pooled


### 1 minute bars

| entry, exit | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, time stop 60 bars | 98732 | 203.6 | 21 | 0.7 | -13.3 | 23% | -69.2 |
| Donchian 20 breakout, EMA 200 side; stop 2 ATR, trail 2 ATR | 241190 | 497.3 | 2 | -2.6 | -16.6 | 12% | -357.3 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, trail 5 ATR | 109747 | 226.3 | 14 | -0.7 | -14.7 | 21% | -90.6 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, time stop 60 bars | 24285 | 50.1 | 16 | 1.4 | -12.6 | 21% | -29.8 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 2 ATR, trail 2 ATR | 30628 | 63.2 | 1 | -2.2 | -16.2 | 11% | -123.8 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, trail 5 ATR | 27487 | 56.7 | 9 | -1.6 | -15.6 | 20% | -53.2 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, time stop 60 bars | 25208 | 52.0 | 19 | 2.3 | -11.7 | 26% | -32.1 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 2 ATR, trail 2 ATR | 30621 | 63.1 | 2 | -1.4 | -15.4 | 12% | -121.4 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, trail 5 ATR | 26457 | 54.6 | 13 | 1.5 | -12.5 | 23% | -36.9 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, time stop 60 bars | 62779 | 129.4 | 26 | 2.3 | -11.7 | 25% | -48.0 |
| RSI(2) 5/95 with the EMA 200 trend; stop 2 ATR, trail 2 ATR | 115969 | 239.1 | 4 | -0.5 | -14.5 | 14% | -207.0 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, trail 5 ATR | 66438 | 137.0 | 18 | 1.4 | -12.6 | 23% | -57.7 |
| EMA 20/100 pullback; stop 3 ATR, time stop 60 bars | 51322 | 105.8 | 26 | 0.3 | -13.7 | 25% | -45.3 |
| EMA 20/100 pullback; stop 2 ATR, trail 2 ATR | 81607 | 168.3 | 4 | -1.9 | -15.9 | 15% | -149.4 |
| EMA 20/100 pullback; stop 3 ATR, trail 5 ATR | 51780 | 106.8 | 17 | -0.4 | -14.4 | 23% | -51.3 |
| Bollinger squeeze breakout; stop 3 ATR, time stop 60 bars | 61028 | 125.8 | 18 | 0.8 | -13.2 | 21% | -68.1 |
| Bollinger squeeze breakout; stop 2 ATR, trail 2 ATR | 101070 | 208.4 | 3 | -1.5 | -15.5 | 10% | -320.5 |
| Bollinger squeeze breakout; stop 3 ATR, trail 5 ATR | 64235 | 132.4 | 13 | -0.4 | -14.4 | 19% | -95.1 |

### 5 minute bars

| entry, exit | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, time stop 60 bars | 17095 | 35.2 | 165 | 1.3 | -12.7 | 30% | -11.4 |
| Donchian 20 breakout, EMA 200 side; stop 2 ATR, trail 2 ATR | 33859 | 69.8 | 21 | -2.3 | -16.3 | 26% | -46.7 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, trail 5 ATR | 15446 | 31.8 | 106 | 1.3 | -12.7 | 30% | -10.9 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, time stop 60 bars | 2717 | 5.6 | 146 | 12.0 | -2.0 | 32% | -0.6 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 2 ATR, trail 2 ATR | 3130 | 6.5 | 8 | -2.0 | -16.0 | 27% | -10.3 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, trail 5 ATR | 2791 | 5.8 | 71 | 4.3 | -9.7 | 31% | -3.1 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, time stop 60 bars | 2906 | 6.0 | 126 | -5.4 | -19.4 | 31% | -6.1 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 2 ATR, trail 2 ATR | 3126 | 6.4 | 12 | -0.6 | -14.6 | 26% | -9.7 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, trail 5 ATR | 2842 | 5.9 | 81 | -7.0 | -21.0 | 29% | -6.6 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, time stop 60 bars | 10502 | 21.7 | 239 | 5.5 | -8.5 | 34% | -6.2 |
| RSI(2) 5/95 with the EMA 200 trend; stop 2 ATR, trail 2 ATR | 18809 | 38.8 | 37 | 2.7 | -11.3 | 28% | -23.6 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, trail 5 ATR | 9320 | 19.2 | 164 | 4.3 | -9.7 | 31% | -6.1 |
| EMA 20/100 pullback; stop 3 ATR, time stop 60 bars | 12040 | 24.8 | 203 | 4.0 | -10.0 | 32% | -7.6 |
| EMA 20/100 pullback; stop 2 ATR, trail 2 ATR | 21870 | 45.1 | 31 | -0.5 | -14.5 | 27% | -31.4 |
| EMA 20/100 pullback; stop 3 ATR, trail 5 ATR | 10635 | 21.9 | 136 | 2.3 | -11.7 | 30% | -7.9 |
| Bollinger squeeze breakout; stop 3 ATR, time stop 60 bars | 10378 | 21.4 | 147 | 1.2 | -12.8 | 28% | -11.0 |
| Bollinger squeeze breakout; stop 2 ATR, trail 2 ATR | 16870 | 34.8 | 23 | -2.2 | -16.2 | 23% | -47.7 |
| Bollinger squeeze breakout; stop 3 ATR, trail 5 ATR | 9820 | 20.2 | 100 | -0.5 | -14.5 | 28% | -13.3 |

### 15 minute bars

| entry, exit | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, time stop 60 bars | 5336 | 11.0 | 634 | 1.0 | -13.0 | 33% | -3.8 |
| Donchian 20 breakout, EMA 200 side; stop 2 ATR, trail 2 ATR | 10100 | 20.8 | 68 | -1.0 | -15.0 | 32% | -12.8 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, trail 5 ATR | 4554 | 9.4 | 422 | 3.8 | -10.2 | 33% | -2.6 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, time stop 60 bars | 891 | 1.8 | 683 | 28.8 | 14.8 | 38% | 1.5 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 2 ATR, trail 2 ATR | 1009 | 2.1 | 28 | 7.9 | -6.1 | 37% | -1.3 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, trail 5 ATR | 894 | 1.8 | 342 | 21.3 | 7.3 | 37% | 0.7 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, time stop 60 bars | 977 | 2.0 | 358 | -24.1 | -38.1 | 31% | -3.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 2 ATR, trail 2 ATR | 1020 | 2.1 | 29 | -27.6 | -41.6 | 24% | -10.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, trail 5 ATR | 931 | 1.9 | 277 | -22.0 | -36.0 | 29% | -3.3 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, time stop 60 bars | 3283 | 6.8 | 775 | -4.7 | -18.7 | 35% | -4.4 |
| RSI(2) 5/95 with the EMA 200 trend; stop 2 ATR, trail 2 ATR | 5593 | 11.5 | 120 | 0.4 | -13.6 | 31% | -8.9 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, trail 5 ATR | 2972 | 6.1 | 532 | -8.4 | -22.4 | 32% | -4.7 |
| EMA 20/100 pullback; stop 3 ATR, time stop 60 bars | 4186 | 8.6 | 672 | -5.6 | -19.6 | 33% | -5.3 |
| EMA 20/100 pullback; stop 2 ATR, trail 2 ATR | 7795 | 16.1 | 98 | -2.4 | -16.4 | 31% | -12.6 |
| EMA 20/100 pullback; stop 3 ATR, trail 5 ATR | 3572 | 7.4 | 470 | -6.4 | -20.4 | 31% | -4.4 |
| Bollinger squeeze breakout; stop 3 ATR, time stop 60 bars | 3245 | 6.7 | 529 | 4.1 | -9.9 | 31% | -2.6 |
| Bollinger squeeze breakout; stop 2 ATR, trail 2 ATR | 5342 | 11.0 | 70 | -2.7 | -16.7 | 29% | -14.6 |
| Bollinger squeeze breakout; stop 3 ATR, trail 5 ATR | 2968 | 6.1 | 349 | -5.5 | -19.5 | 30% | -5.4 |

## 2. Shortlist: development against the held out months

| candidate, range | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| 15m impulse; stop 3 ATR, time 15 h, development | 891 | 1.8 | 683 | 28.8 | 14.8 | 38% | 1.5 |
| 15m impulse; stop 3 ATR, time 15 h, held out | 426 | 2.2 | 582 | 4.8 | -9.2 | 35% | -1.0 |
| 15m impulse; stop 3 ATR, maker target 6 ATR, development | 973 | 2.0 | 300 | 28.5 | 15.9 | 44% | 1.6 |
| 15m impulse; stop 3 ATR, maker target 6 ATR, held out | 472 | 2.4 | 281 | 9.6 | -3.2 | 39% | -0.4 |
| 15m impulse; stop 3 ATR, trail 5 ATR, development | 916 | 1.9 | 341 | 22.3 | 8.3 | 38% | 0.8 |
| 15m impulse; stop 3 ATR, trail 5 ATR, held out | 439 | 2.3 | 276 | 2.9 | -11.1 | 34% | -1.3 |
| 15m impulse; stop 3 ATR, trail 2 ATR once 4 ATR ahead, development | 969 | 2.0 | 215 | 20.0 | 6.0 | 50% | 0.7 |
| 15m impulse; stop 3 ATR, trail 2 ATR once 4 ATR ahead, held out | 462 | 2.4 | 190 | -3.3 | -17.3 | 42% | -2.2 |
| 1m BTC lead on five alts; stop 2 ATR, trail 2 ATR, development | 14539 | 30.0 | 2 | 0.6 | -13.4 | 12% | -78.6 |
| 1m BTC lead on five alts; stop 2 ATR, trail 2 ATR, held out | 5731 | 29.7 | 2 | 1.8 | -12.2 | 8% | -83.8 |

## 3. BTC lead: the alt's raw move in bps after the signal, by entry delay (whole range)

| coin | signals per day | enter next open: 1m / 5m / 20m | enter 1 minute later: 1m / 5m / 20m |
|---|---|---|---|
| PF_ETHUSD | 4.1 | 0.7 / 0.2 / 0.3 | -0.2 / -0.4 / -0.5 |
| PF_SOLUSD | 5.2 | 1.7 / 1.6 / 1.9 | 0.1 / -0.1 / 0.2 |
| PF_XRPUSD | 7.3 | 1.4 / 1.7 / 1.1 | 0.3 / 0.3 / -0.6 |
| PF_DOGEUSD | 7.0 | 2.9 / 3.5 / 3.0 | 0.3 / 0.5 / 0.0 |
| PF_LINKUSD | 8.0 | 3.9 / 5.2 / 5.2 | 0.8 / 1.5 / 0.9 |

## 4. Maker fade around the EMA 20, development range, six coins pooled

| setup | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| 1m, limits 2 ATR from the EMA; stop 3 ATR | 232302 | 479.0 | 6 | -2.5 | -8.9 | 51% | -130.0 |
| 1m, limits 2 ATR from the EMA; stop 3 ATR, trail 2 ATR | 325512 | 671.2 | 3 | -2.8 | -10.2 | 32% | -246.1 |
| 1m, limits 3 ATR from the EMA; stop 3 ATR | 161943 | 333.9 | 7 | -2.0 | -8.8 | 44% | -93.3 |
| 1m, limits 3 ATR from the EMA; stop 3 ATR, trail 2 ATR | 224402 | 462.7 | 3 | -3.1 | -11.1 | 20% | -211.0 |
| 1m, limits 4 ATR from the EMA; stop 3 ATR | 105198 | 216.9 | 8 | -1.9 | -9.1 | 38% | -72.1 |
| 1m, limits 4 ATR from the EMA; stop 3 ATR, trail 2 ATR | 134674 | 277.7 | 3 | -3.5 | -12.0 | 17% | -178.6 |
| 5m, limits 2 ATR from the EMA; stop 3 ATR | 39465 | 81.4 | 38 | -2.6 | -8.8 | 57% | -21.7 |
| 5m, limits 2 ATR from the EMA; stop 3 ATR, trail 2 ATR | 59290 | 122.2 | 20 | -2.1 | -9.3 | 36% | -41.2 |
| 5m, limits 3 ATR from the EMA; stop 3 ATR | 23503 | 48.5 | 47 | -2.1 | -8.8 | 48% | -14.1 |
| 5m, limits 3 ATR from the EMA; stop 3 ATR, trail 2 ATR | 33270 | 68.6 | 19 | -1.7 | -9.5 | 30% | -28.9 |
| 5m, limits 4 ATR from the EMA; stop 3 ATR | 12711 | 26.2 | 50 | -3.4 | -10.5 | 41% | -10.8 |
| 5m, limits 4 ATR from the EMA; stop 3 ATR, trail 2 ATR | 16201 | 33.4 | 14 | -3.1 | -11.4 | 29% | -22.1 |

## 5. Take profits on 1 and 5 minute bars, six coins pooled, time stop 60 bars

Development range; a held out row follows any setup whose development net is above zero.

### 1 minute bars

| entry, exit | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 1 ATR | 328869 | 678.1 | 1 | -0.5 | -12.1 | 32% | -364.2 |
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 2 ATR | 263978 | 544.3 | 2 | 0.1 | -12.1 | 33% | -251.8 |
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 3 ATR | 228014 | 470.1 | 3 | 0.4 | -12.2 | 28% | -200.7 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 1 ATR | 302720 | 624.2 | 3 | -1.2 | -11.9 | 43% | -237.0 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 2 ATR | 226714 | 467.5 | 5 | -0.6 | -12.0 | 48% | -159.3 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 3 ATR | 185406 | 382.3 | 8 | -0.3 | -12.1 | 43% | -125.4 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 1 ATR | 30663 | 63.2 | 0 | -1.1 | -12.9 | 24% | -129.9 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 2 ATR | 30624 | 63.1 | 1 | -0.1 | -12.4 | 29% | -95.3 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 3 ATR | 30408 | 62.7 | 1 | 0.4 | -12.3 | 26% | -78.6 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 1 ATR | 30575 | 63.0 | 2 | -1.6 | -12.6 | 34% | -88.9 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 2 ATR | 30319 | 62.5 | 4 | -0.8 | -12.2 | 43% | -66.2 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 3 ATR | 29827 | 61.5 | 5 | -0.3 | -12.1 | 41% | -54.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 1 ATR | 30662 | 63.2 | 1 | 0.3 | -11.3 | 27% | -113.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 2 ATR | 30627 | 63.1 | 1 | 1.2 | -10.8 | 33% | -82.0 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 3 ATR | 30426 | 62.7 | 2 | 1.7 | -10.8 | 30% | -67.7 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 1 ATR | 30563 | 63.0 | 1 | -0.3 | -11.1 | 35% | -80.6 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 2 ATR | 30335 | 62.5 | 3 | 0.4 | -10.9 | 46% | -59.0 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 3 ATR | 29852 | 61.6 | 6 | 0.8 | -11.0 | 43% | -49.6 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 1 ATR | 127661 | 263.2 | 2 | 0.4 | -11.2 | 30% | -217.8 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 2 ATR | 123578 | 254.8 | 3 | 0.9 | -11.3 | 33% | -166.8 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 3 ATR | 118014 | 243.3 | 4 | 1.3 | -11.3 | 28% | -137.2 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 1 ATR | 101150 | 208.6 | 4 | -0.2 | -11.0 | 40% | -137.6 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 2 ATR | 93199 | 192.2 | 7 | 0.3 | -11.1 | 47% | -101.6 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 3 ATR | 86331 | 178.0 | 10 | 0.8 | -11.0 | 43% | -83.2 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 1 ATR | 87014 | 179.4 | 2 | -0.3 | -11.8 | 35% | -140.6 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 2 ATR | 80839 | 166.7 | 3 | 0.2 | -12.0 | 35% | -112.1 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 3 ATR | 76911 | 158.6 | 4 | 0.6 | -11.9 | 29% | -94.8 |
| EMA 20/100 pullback; stop 3 ATR, take profit 1 ATR | 78148 | 161.1 | 4 | -1.0 | -11.7 | 46% | -95.2 |
| EMA 20/100 pullback; stop 3 ATR, take profit 2 ATR | 69326 | 142.9 | 7 | -0.6 | -11.9 | 50% | -73.4 |
| EMA 20/100 pullback; stop 3 ATR, take profit 3 ATR | 64203 | 132.4 | 10 | -0.1 | -11.9 | 44% | -61.9 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 1 ATR | 117450 | 242.2 | 1 | -0.0 | -11.8 | 23% | -302.4 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 2 ATR | 103872 | 214.2 | 2 | 0.4 | -11.9 | 29% | -223.5 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 3 ATR | 97354 | 200.7 | 3 | 0.7 | -11.9 | 26% | -184.4 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 1 ATR | 108893 | 224.5 | 3 | -0.7 | -11.6 | 31% | -202.2 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 2 ATR | 91346 | 188.3 | 5 | -0.3 | -11.7 | 44% | -145.9 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 3 ATR | 81678 | 168.4 | 7 | 0.0 | -11.9 | 40% | -118.8 |

### 5 minute bars

| entry, exit | trades | per day | median hold (min) | gross bps | net bps | wins | t |
|---|---|---|---|---|---|---|---|
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 1 ATR | 56881 | 117.3 | 9 | -0.8 | -12.0 | 54% | -63.0 |
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 2 ATR | 43734 | 90.2 | 16 | -0.0 | -12.0 | 40% | -40.9 |
| Donchian 20 breakout, EMA 200 side; stop 1.5 ATR, take profit 3 ATR | 37060 | 76.4 | 21 | 0.3 | -12.2 | 32% | -31.9 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 1 ATR | 53036 | 109.4 | 17 | -1.1 | -11.6 | 70% | -40.8 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 2 ATR | 37806 | 78.0 | 37 | -0.5 | -11.6 | 57% | -25.7 |
| Donchian 20 breakout, EMA 200 side; stop 3 ATR, take profit 3 ATR | 30222 | 62.3 | 55 | 0.2 | -11.5 | 48% | -19.3 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 1 ATR | 3147 | 6.5 | 3 | -2.7 | -14.0 | 53% | -14.3 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 2 ATR | 3147 | 6.5 | 6 | 0.9 | -11.0 | 41% | -8.4 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 1.5 ATR, take profit 3 ATR | 3142 | 6.5 | 9 | 3.7 | -8.7 | 34% | -5.4 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 1 ATR | 3145 | 6.5 | 7 | -3.4 | -13.9 | 69% | -9.4 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 2 ATR | 3140 | 6.5 | 21 | 0.3 | -10.8 | 58% | -5.7 |
| Volume impulse (3 ATR bar, 3x volume), follow; stop 3 ATR, take profit 3 ATR | 3124 | 6.4 | 33 | 3.0 | -8.6 | 50% | -3.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 1 ATR | 3147 | 6.5 | 2 | 0.2 | -10.9 | 55% | -11.6 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 2 ATR | 3146 | 6.5 | 8 | 0.3 | -11.6 | 41% | -8.8 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 1.5 ATR, take profit 3 ATR | 3143 | 6.5 | 13 | 0.1 | -12.4 | 32% | -7.9 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 1 ATR | 3146 | 6.5 | 5 | -4.1 | -14.6 | 68% | -10.6 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 2 ATR | 3142 | 6.5 | 18 | -2.5 | -13.7 | 56% | -7.5 |
| Volume impulse (3 ATR bar, 3x volume), fade; stop 3 ATR, take profit 3 ATR | 3129 | 6.5 | 33 | -2.5 | -14.3 | 46% | -6.4 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 1 ATR | 20200 | 41.6 | 13 | 1.8 | -9.3 | 57% | -31.2 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 2 ATR | 19025 | 39.2 | 27 | 2.5 | -9.4 | 42% | -21.8 |
| RSI(2) 5/95 with the EMA 200 trend; stop 1.5 ATR, take profit 3 ATR | 17585 | 36.3 | 37 | 2.7 | -9.6 | 34% | -17.9 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 1 ATR | 16805 | 34.6 | 22 | 1.5 | -8.9 | 72% | -19.5 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 2 ATR | 14876 | 30.7 | 53 | 2.6 | -8.5 | 59% | -12.8 |
| RSI(2) 5/95 with the EMA 200 trend; stop 3 ATR, take profit 3 ATR | 13417 | 27.7 | 80 | 3.0 | -8.7 | 49% | -10.5 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 1 ATR | 24855 | 51.2 | 13 | -0.1 | -11.3 | 55% | -37.8 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 2 ATR | 21670 | 44.7 | 23 | 0.3 | -11.7 | 40% | -27.7 |
| EMA 20/100 pullback; stop 1.5 ATR, take profit 3 ATR | 19848 | 40.9 | 31 | 0.7 | -11.7 | 32% | -22.4 |
| EMA 20/100 pullback; stop 3 ATR, take profit 1 ATR | 21548 | 44.4 | 23 | -0.2 | -10.7 | 70% | -23.4 |
| EMA 20/100 pullback; stop 3 ATR, take profit 2 ATR | 17584 | 36.3 | 49 | 0.8 | -10.4 | 57% | -15.7 |
| EMA 20/100 pullback; stop 3 ATR, take profit 3 ATR | 15592 | 32.1 | 71 | 1.4 | -10.3 | 48% | -12.5 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 1 ATR | 21521 | 44.4 | 10 | -0.7 | -12.0 | 51% | -56.0 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 2 ATR | 18101 | 37.3 | 17 | -0.9 | -13.0 | 38% | -41.0 |
| Bollinger squeeze breakout; stop 1.5 ATR, take profit 3 ATR | 16596 | 34.2 | 23 | -0.6 | -13.2 | 30% | -33.2 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 1 ATR | 19856 | 40.9 | 20 | -1.3 | -11.8 | 67% | -37.4 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 2 ATR | 15349 | 31.6 | 40 | -1.5 | -12.7 | 55% | -26.0 |
| Bollinger squeeze breakout; stop 3 ATR, take profit 3 ATR | 13287 | 27.4 | 57 | -1.2 | -12.9 | 46% | -20.4 |
