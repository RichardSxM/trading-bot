# 0003. `delta_divergence`: fade a strong few minute move that aggressive volume does not back, gated by ADX

## Summary

When price moves strongly over the last few minutes but the net aggressive volume (buy market orders minus sell market orders) over the same minutes is flat or against the move, this candidate bets the move fades. It enters post only at the touch, targets a half retrace, and exits after 10 minutes at most. It needs real trade aggressor data, so it runs in replay only, with the ADX gate on.

## Hypothesis

- **Why it might persist**: a move without net aggressive buying (or selling) is driven by thin depth and pulled quotes, not real demand. When liquidity returns, price tends to give part of it back. It measures actual trades, unlike book imbalance.
- **When it should work**: calm, range bound hours.
- **When it should fail**: trends built by passive buying (hidden or iceberg orders) that delta does not see; news; liquidation cascades (Tardis has no liquidation data, so this is untested here).
- **Entry mode**: maker. A fade rests where price has just been pushed.

## Rules

All on `1-MINUTE-LAST` bars, evaluated on bar close `t` while flat (base rules of AC-5 apply). The node also subscribes trade ticks.

- **Delta buckets**: every trade tick adds its signed size (buyer aggressor +, seller aggressor −, `NO_AGGRESSOR` 0) to the bucket keyed by its minute, `floor(ts_event ÷ 60 s)`. On the close of the bar whose `ts_event` closes minute m, `DeltaWindow(lookback_bars)` sums the buckets of minutes m − lookback_bars + 1 through m (a minute with no trades is 0). It then drops buckets older than the window. A trade whose minute is already summed (it arrived late) is dropped and counted in `late_trades`. Keying by `ts_event` makes the result independent of callback order and latency.
- **Indicators**: `Atr(atr_n)`, `DeltaWindow(lookback_bars)`.
- **Move**: `move = close[t] − close[t − lookback_bars]`.
- **Short**: `move ≥ move_atr × atr[t]` and `delta ≤ 0`.
- **Long**: `move ≤ −move_atr × atr[t]` and `delta ≥ 0`.
- **Stop**: short, the highest high of the last `lookback_bars` bars plus `stop_atr_buffer × atr[t]`; long, the lowest low minus it.
- **Target**: `close[t] − move ÷ 2` (half retrace), for both sides.
- **Time stop**: `time_stop_min`.
- **No trade**: unwarm indicators, the base's blocks, spread cap, reward floor, and `regime`.
- **`ScalpSignal.inputs`**: `close`, `move`, `atr`, `delta`, `buy_size`, `sell_size` (the window's two sides).

## Config and grid

Fixed: `atr_n = 14`, `stop_atr_buffer = "0.25"`, `time_stop_min = 10`, `entry_mode = "maker"`, `entry_timeout_s = 30`, `min_target_bps = "10.5"`, `regime_adx_max = "25"`.

Grid (4 points, sorted key expansion): `lookback_bars = [5, 10]`, `move_atr = ["1.5", "2.5"]`.

Warmup: `max(atr_n + 1, lookback_bars + 1)` one minute bars, and 140 minutes for the 5 minute ADX, whichever is later.

## Study files

| File | Lab | Differs by |
|---|---|---|
| `apps/lab/studies/delta_divergence.toml` | replay | `regime_adx_max = "25"`, `max_spread_bps` = the audit p95 (index AC-3) |

All other fields per index AC-10. No candle screen (the candle lab's trades carry no aggressor side). Instead, the index AC-17 pre check (4 development days, the most permissive grid point, at least 17 approved entries a day) must pass first. Scratch run files (not baked into the image): `lab-reports/scratch/delta_divergence.toml` (2025-09-01, `min_target_bps` and `regime_adx_max` unset, for index AC-16) and `lab-reports/scratch/delta_divergence_precheck.toml` (the AC-17 days and grid point, with the study's own settings).

## Rationale (short)

The one order flow idea the set keeps: it uses data only the replay can see, and it is the scope's most distinct idea. Book imbalance was left out because its edge decays within seconds, far below the 1 to 15 minute horizon the fees require. The half retrace target comes from the idea itself, which avoids a free R multiple setting.
