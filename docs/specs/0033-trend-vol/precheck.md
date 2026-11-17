# Pre check: trend_vol_precheck · spec 0033 AC-12, AC-12a, AC-12b

_Run once by `lab book precheck` at commit `bdb7d06aa2a085cd325b3c86d6e9aab6c4b20145`. Window 2025-05-24 to 2026-11-16, second half from 2026-02-19, cost 0.0007 a side, starting equity 10000, 12 coins on Kraken candles. Funding where Kraken has rates; the most days with a missing rate in any run: 178._

_Run note: a first attempt at commit `3b2c155` stopped at the runner's own check (the hourly trial engine and
`replay_book` differed in the 28th decimal from 2025-11-14, the order funding was added to cash) before any
figure was computed or written; the ledger was untouched. Fixed in `bdb7d06`, with a test that catches it, and
this is the one run of the unchanged pre registration file._

## Pass rule (as pre registered)

Net of costs and funding, Sharpe per half (first half before 2026-02-19), trend_vol as built otherwise.
- Baseline reference per half: the mean Sharpe of trend_vol at cut hours 00, 06, 12, 18.
- ewma: ewma20 beats the reference in both halves (strictly above); ewma10 and ewma40 are not below it in
  either half.
- tranche: tranche4 beats the reference in both halves; tranche2 and tranche8 are not below it in either
  half; and the full window Sharpe spread (highest minus lowest) of tranche4 at start offsets 00 to 05
  (tranche4 and tranche4_off01 to _off05) is under half the baseline's full window spread across its four
  cut hours.
- scale (AC-12a): scale3_stop8 beats the baseline at its own cut hour in both halves at each of 00, 06, 12,
  18; scale2_stop8, scale5_stop8, scale3_stop5, scale3_stop12 (at 00) are not below the baseline at 00 in
  either half; scale3_nostop is a control and decides nothing. Any pass is labelled "seen" (the trial was
  found by a scratch replay that saw this window), or "seen, fragile" when scale3_stop8@00's Binance
  information run has a lower full window Sharpe than the Binance baseline at 00.
- A pass of ewma leads to trend_vol_r2 (AC-13); a pass of tranche or scale only unlocks /architect.
- Binance 2018-01-01 to 2022-03-31 runs (AC-12b) are information only and decide nothing except the label.

## Kraken runs

| Run | Cut | Sharpe H1 | Sharpe H2 | Sharpe full | End | Max DD | Gross | Turnover a day | Stopped | Stops |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline@00 | 00 | -0.389 | 1.233 | 0.391 | 1.10x | 53.8% | 2.07 | 76.2% | 0.0% | 0 |
| baseline@06 | 06 | 0.331 | 1.139 | 0.702 | 1.42x | 43.6% | 2.19 | 77.5% | 0.0% | 0 |
| baseline@12 | 12 | 0.120 | 1.149 | 0.612 | 1.32x | 53.7% | 2.19 | 76.3% | 0.0% | 0 |
| baseline@18 | 18 | -0.241 | 1.343 | 0.545 | 1.25x | 51.5% | 2.10 | 74.1% | 0.0% | 0 |
| ewma20 | 00 | -0.194 | 1.261 | 0.496 | 1.20x | 47.5% | 1.92 | 68.1% | 0.0% | 0 |
| ewma10 | 00 | -0.299 | 1.133 | 0.397 | 1.11x | 53.6% | 2.01 | 73.6% | 0.0% | 0 |
| ewma40 | 00 | -0.119 | 1.507 | 0.644 | 1.35x | 44.9% | 1.84 | 65.1% | 0.0% | 0 |
| tranche4 | 00 | 0.078 | 1.197 | 0.621 | 1.33x | 48.9% | 1.97 | 65.1% | 0.0% | 0 |
| tranche2 | 00 | -0.253 | 1.258 | 0.479 | 1.19x | 48.7% | 1.99 | 68.7% | 0.0% | 0 |
| tranche8 | 00 | -0.212 | 1.353 | 0.548 | 1.25x | 47.4% | 1.92 | 59.2% | 0.0% | 0 |
| tranche4_off01 | 01 | 0.200 | 1.545 | 0.853 | 1.59x | 47.0% | 1.97 | 63.7% | 0.0% | 0 |
| tranche4_off02 | 02 | -0.106 | 1.269 | 0.571 | 1.27x | 48.1% | 1.98 | 65.3% | 0.0% | 0 |
| tranche4_off03 | 03 | 0.113 | 1.361 | 0.721 | 1.43x | 45.7% | 1.95 | 65.9% | 0.0% | 0 |
| tranche4_off04 | 04 | 0.178 | 1.252 | 0.705 | 1.42x | 48.2% | 1.95 | 64.0% | 0.0% | 0 |
| tranche4_off05 | 05 | 0.286 | 1.036 | 0.661 | 1.37x | 44.8% | 1.95 | 65.0% | 0.0% | 0 |
| scale3_stop8@00 | 00 | 0.163 | 1.486 | 0.791 | 1.51x | 50.2% | 1.87 | 60.5% | 0.0% | 2 |
| scale3_stop8@06 | 06 | 0.508 | 1.210 | 0.837 | 1.56x | 41.3% | 1.99 | 61.5% | 0.0% | 3 |
| scale3_stop8@12 | 12 | -0.016 | 0.980 | 0.455 | 1.16x | 56.2% | 1.98 | 61.3% | 0.3% | 2 |
| scale3_stop8@18 | 18 | -0.395 | 1.184 | 0.388 | 1.10x | 49.6% | 1.90 | 59.1% | 0.3% | 2 |
| scale2_stop8 | 00 | -0.155 | 1.534 | 0.661 | 1.37x | 51.0% | 1.96 | 66.8% | 0.0% | 2 |
| scale5_stop8 | 00 | 0.301 | 1.387 | 0.818 | 1.53x | 46.9% | 1.73 | 52.5% | 0.0% | 2 |
| scale3_stop5 | 00 | 0.199 | 1.472 | 0.805 | 1.53x | 49.6% | 1.87 | 60.5% | 0.0% | 11 |
| scale3_stop12 | 00 | 0.163 | 1.459 | 0.778 | 1.50x | 50.2% | 1.87 | 60.5% | 0.0% | 2 |
| scale3_nostop | 00 | 0.163 | 1.492 | 0.794 | 1.52x | 50.2% | 1.87 | 60.5% | 0.0% | 0 |

Baseline reference (mean of the cut hours): H1 -0.045, H2 1.216.

## Verdicts

### ewma: FAIL

- ewma20 beats the reference: H1 -0.194 vs -0.045, H2 1.261 vs 1.216, FAIL
- ewma10 not below the reference: H1 -0.299 vs -0.045, H2 1.133 vs 1.216, FAIL
- ewma40 not below the reference: H1 -0.119 vs -0.045, H2 1.507 vs 1.216, FAIL

### tranche: FAIL

- tranche4 beats the reference: H1 0.078 vs -0.045, H2 1.197 vs 1.216, FAIL
- tranche2 not below the reference: H1 -0.253 vs -0.045, H2 1.258 vs 1.216, FAIL
- tranche8 not below the reference: H1 -0.212 vs -0.045, H2 1.353 vs 1.216, FAIL
- offset spread 0.282 over 6 runs vs half the baseline spread 0.156, FAIL

### scale: FAIL

- scale3_stop8@00 beats the baseline at 00: H1 0.163 vs -0.389, H2 1.486 vs 1.233, ok
- scale3_stop8@06 beats the baseline at 06: H1 0.508 vs 0.331, H2 1.210 vs 1.139, ok
- scale3_stop8@12 beats the baseline at 12: H1 -0.016 vs 0.120, H2 0.980 vs 1.149, FAIL
- scale3_stop8@18 beats the baseline at 18: H1 -0.395 vs -0.241, H2 1.184 vs 1.343, FAIL
- scale2_stop8 not below the baseline at 00: H1 -0.155 vs -0.389, H2 1.534 vs 1.233, ok
- scale5_stop8 not below the baseline at 00: H1 0.301 vs -0.389, H2 1.387 vs 1.233, ok
- scale3_stop5 not below the baseline at 00: H1 0.199 vs -0.389, H2 1.472 vs 1.233, ok
- scale3_stop12 not below the baseline at 00: H1 0.163 vs -0.389, H2 1.459 vs 1.233, ok
- scale3_nostop (control, decides nothing): H1 0.163, H2 1.492

## Binance information runs (AC-12b, information only, other venue)

_Binance spot USDT hourly closes, 2018-01-01 to 2022-03-31, cut hour 00, fills at the next hourly close, no funding, 11 coins (no HYPE). Nothing here decides a pass._

| Run | Cut | Sharpe full | Change vs baseline | End | Max DD | Gross | Turnover a day | Stopped | Stops |
|---|---|---|---|---|---|---|---|---|---|
| baseline@00 | 00 | 1.637 | n/a | 31.33x | 35.4% | 1.13 | 37.3% | 0.0% | 0 |
| ewma20 | 00 | 1.714 | 0.077 | 27.65x | 35.2% | 0.98 | 31.1% | 0.0% | 0 |
| ewma10 | 00 | 1.641 | 0.004 | 25.44x | 36.0% | 1.05 | 34.5% | 0.0% | 0 |
| ewma40 | 00 | 1.681 | 0.043 | 26.22x | 37.7% | 0.93 | 29.0% | 0.0% | 0 |
| tranche4 | 00 | 1.835 | 0.198 | 44.97x | 33.8% | 1.05 | 32.1% | 0.0% | 0 |
| tranche2 | 00 | 1.835 | 0.198 | 46.75x | 38.3% | 1.07 | 34.0% | 0.0% | 0 |
| tranche8 | 00 | 1.961 | 0.324 | 62.10x | 38.8% | 1.04 | 29.6% | 0.0% | 0 |
| scale3_stop8@00 | 00 | 1.817 | 0.179 | 42.50x | 39.1% | 1.03 | 30.0% | 0.1% | 5 |
| scale2_stop8 | 00 | 1.790 | 0.152 | 40.71x | 33.1% | 1.07 | 32.7% | 0.1% | 5 |
| scale5_stop8 | 00 | 1.727 | 0.090 | 33.46x | 39.3% | 0.96 | 26.3% | 0.1% | 5 |
| scale3_stop5 | 00 | 1.847 | 0.210 | 45.77x | 39.0% | 1.03 | 29.9% | 0.3% | 19 |
| scale3_stop12 | 00 | 1.781 | 0.144 | 39.80x | 37.6% | 1.04 | 30.1% | 0.0% | 0 |
| scale3_nostop | 00 | 1.781 | 0.144 | 39.80x | 37.6% | 1.04 | 30.1% | 0.0% | 0 |

## Outcome (AC-13)

Nothing passed, so no `trend_vol_r2` is built and no `/architect` round is owed for tranches or scale in. The
study (tasks 9 and 10) runs on `trend_vol` as built.
