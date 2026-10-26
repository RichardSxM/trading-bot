# Verify: intraday trend candidates · spec 0020 · updated 2026-10-26

## Task 1 cost check (2026-10-26)

`scripts/spikes/cost_check_trend.py`, run in the `kfs/lab:s0019` image over the development range 2022-04-01 to 2025-10-19 (exclusive). It reads candles only and builds no strategy, so it is not a trial (AC-8). Round trip cost about 14 bps.

| Measure | bps | × cost |
|---|---|---|
| 30 minute bars | 62 256 bars | |
| median 30 minute ATR(14) | 43.87 | 3.13 |
| median noise band half width, `band_mult` 1 | 97.02 | 6.93 |
| median noise band half width, `band_mult` 1.5 | 145.53 | 10.39 |
| median noise band half width, `band_mult` 2 | 194.04 | 13.86 |
| 4 hour bars | 7 782 bars | |
| median 4 hour ATR(14) | 138.23 | 9.87 |
| `donchian_4h` stop at 2 ATR | 276.45 | 19.75 |
| `donchian_4h` stop at 3 ATR | 414.68 | 29.62 |

Share of 30 minute closes beyond `day_open × (1 ± band_mult × sigma)`, over the 61 584 bars with a ready `sigma` (lookback 14 days): 36.5% at `band_mult` 1, 22.4% at 1.5, 14.5% at 2.

Reading: the band is several times the cost, so a breakout that keeps going can clear it. But at `band_mult` 1 more than a third of all closes sit outside the band, so with unlimited re-entry `noise_breakout` will signal often; whipsaw churn (the child spec's expected failure) is the thing to watch in the results. The Donchian stops are 20 to 30 times the cost, so its trades are few and large.

## AC-1 hash baseline (2026-10-26)

`code_hash(code_closure(m, root))` for every `trading_core.node` module (28) on origin/main 02a0cc5, run in `kfs/lab:s0019` against a `git archive` of main's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `1b7e0a32a5c4ce62066c560872ed47d05bc76080ae197c9128374800b134dbc9`. The close out (AC-13) runs the same list on the branch: the 28 existing lines must match exactly, and only the new candidates' modules may be added.
