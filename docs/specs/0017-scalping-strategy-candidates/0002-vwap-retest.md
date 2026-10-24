# 0002. `vwap_retest`: in a 1 minute EMA trend, enter a pullback that touches the day's VWAP and holds

## Summary

When the 9 EMA is above the 21 EMA and price pulls back from above to touch the day's VWAP (the volume weighted average price since 00:00 UTC) and still closes above it, this candidate buys (the mirror for shorts, which covers the scope's break and retest idea). It enters post only at the touch, aims for a set multiple of its risk, and exits after 15 minutes at most. It runs ungated, because the ADX gate would block exactly the trends it needs.

## Hypothesis

- **Why it might persist**: VWAP is the reference that execution algorithms and many traders judge themselves by, so in a trend there is buying near it from below (in an uptrend) and selling near it from above (in a downtrend). A pullback that holds VWAP marks that flow showing up.
- **When it should work**: days with an established direction and orderly pullbacks.
- **When it should fail**: range days, where the EMAs cross often and VWAP is crossed back and forth; sharp reversals that slice through VWAP; the hours just after 00:00 UTC, when VWAP rests on little volume (the warmup covers this).
- **Entry mode**: maker. The entry buys a pullback, so price comes to the resting order.

## Rules

All on `1-MINUTE-LAST` bars, evaluated on bar close `t` while flat (base rules of AC-5 apply).

- **Indicators**: `Ema(9)` and `Ema(21)` on closes (the existing `trading_core.strategy.indicators.Ema`, imported, not edited), `SessionVwap`, `Atr(atr_n)`.
- **VWAP anchor**: the anchor is 00:00 UTC of the first bar's day, and it resets at every bar whose open is 00:00 UTC. A strategy that starts mid day (a replay segment after a gap, the trader after a restart) sums from its first bar under that day's anchor, so it sees only part of the day's volume. That is the same in replay and live. No signal until `vwap_min_bars` bars have been summed under the current anchor.
- **Long**: `ema9[t] > ema21[t]`, `close[t − 1] > vwap[t − 1]`, `low[t] ≤ vwap[t]`, and `close[t] > vwap[t]`.
- **Short**: `ema9[t] < ema21[t]`, `close[t − 1] < vwap[t − 1]`, `high[t] ≥ vwap[t]`, and `close[t] < vwap[t]`.
- **Stop**: long `vwap[t] − stop_atr × atr[t]`; short `vwap[t] + stop_atr × atr[t]`.
- **Target**: long `close[t] + target_r × (close[t] − stop)`; short the mirror. It is fixed at the signal and not recomputed at the fill, so the realized reward to risk drifts by the fill's distance from `close[t]`. That is accepted.
- **Time stop**: `time_stop_min`.
- **No trade**: unwarm indicators, fewer than `vwap_min_bars` since the anchor, the base's blocks, spread cap, and reward floor.
- **`ScalpSignal.inputs`**: `close`, `low` or `high`, `vwap`, `ema9`, `ema21`, `atr`, `bars_since_anchor`.

## Config and grid

Fixed: `atr_n = 14`, `vwap_min_bars = 60`, `time_stop_min = 15`, `entry_mode = "maker"`, `entry_timeout_s = 30`, `min_target_bps = "10.5"`, `regime_adx_max` unset in replay.

Grid (4 points, sorted key expansion): `stop_atr = ["1.0", "2.0"]`, `target_r = ["1.5", "2.0"]`. The stops are wider than the article's because a 1 minute ATR is often only a few bps, and with a stop at 0.5 ATR most targets would fall under the 10.5 bps reward floor.

Warmup: 60 bars after each anchor (EMAs and ATR warm inside it).

Volume note: in the candle lab, bar volume comes from the four synthetic trades per minute (spec 0014 AC-9), which may slightly exceed the candle's real volume; in replay and live it is the real traded size.

## Study files

| File | Lab | Differs by |
|---|---|---|
| `apps/lab/studies/vwap_retest_screen.toml` | candle | no regime gate, `max_spread_bps` unset |
| `apps/lab/studies/vwap_retest_screen_adx.toml` | candle | `regime_adx_max = "25"`, `max_spread_bps` unset |
| `apps/lab/studies/vwap_retest.toml` | replay | no regime gate, `max_spread_bps` = the audit p95 (index AC-3) |

All other fields per index AC-10. Scratch run files for index AC-16 (not baked into the image): `lab-reports/scratch/vwap_retest.toml` (2025-09-01) and `lab-reports/scratch/vwap_retest_candle.toml` (2025-06), both with `min_target_bps` unset.

## Rationale (short)

The scope names the VWAP bounce as a simple early fit for the fee hurdle, and its break and retest mirror makes it two sided, which the gate's direction concentration check needs. It is the trend idea of the set, so it balances the two reversion ideas. Runner up: the EMA pullback, which uses the same trend filter with a less widely watched level.
