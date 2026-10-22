# 0001. `bollinger_rsi`: fade a 1 minute close back inside the Bollinger band, in flat bands, gated by ADX

## Summary

When price closes outside its 2 standard deviation band with RSI at an extreme, then closes back inside while the bands are flat, this candidate bets on a return to the 20 minute average. It enters with a post only order at the touch, exits at the middle band, a stop past the excursion, or after 15 minutes. It is screened in the candle lab with and without the ADX gate, and the gated version goes to replay.

## Hypothesis

- **Why it might persist**: in a quiet market a close beyond 2σ is often a short burst of market orders eating the nearby depth, not new information. Market makers quote again and price drifts back toward its recent mean.
- **When it should work**: range bound hours with low ADX, flat bands, and normal spreads.
- **When it should fail**: trending moves and news, where the band edge is the start of a move, not its end; a widening of volatility, where the bands chase price. The ADX gate and the flat band test exist for these.
- **Entry mode**: maker. The fade buys where sellers just pushed price, so a resting bid at the touch is on the natural side.

## Rules

All on `1-MINUTE-LAST` bars, evaluated on bar close `t` while flat (base rules of AC-5 apply).

- **Indicators**: `Bollinger(bb_n, bb_k)` on closes, `Rsi(rsi_n)` on closes, `Atr(atr_n)`.
- **Flat bands**: `|middle[t] − middle[t − bb_n]| ≤ flat_band_frac × (upper[t] − middle[t])`, and `upper[t] > middle[t]` (a zero band width is never flat). Needs `bb_n` bars of middle history.
- **Long**: `close[t − 1] < lower[t − 1]`, `rsi[t − 1] ≤ rsi_low`, `close[t] ≥ lower[t]`, and flat bands.
- **Short**: `close[t − 1] > upper[t − 1]`, `rsi[t − 1] ≥ 100 − rsi_low`, `close[t] ≤ upper[t]`, and flat bands.
- **Stop**: long, the lowest low of the run of consecutive bars that closed below the lower band (ending at `t − 1`, at least one bar, at most the last `bb_n` bars) and bar `t`, minus `stop_atr_buffer × atr[t]`. Short, the mirror (highest high plus the buffer).
- **Target**: `middle[t]`, fixed at the signal.
- **Time stop**: `time_stop_min`.
- **No trade**: unwarm indicators, the base's blocks, spread cap, reward floor, and `regime` when gated.
- **`ScalpSignal.inputs`**: `close`, `lower`, `middle`, `upper`, `rsi_prev`, `atr`, `band_drift` (the flat band left side).

## Config and grid

Fixed: `bb_n = 20`, `rsi_n = 14`, `atr_n = 14`, `flat_band_frac = "0.5"`, `stop_atr_buffer = "0.25"`, `time_stop_min = 15`, `entry_mode = "maker"`, `entry_timeout_s = 30`, `min_target_bps = "10.5"`.

Grid (4 points, sorted key expansion): `bb_k = ["2.0", "2.5"]`, `rsi_low = [30, 25]`.

Warmup: exactly 40 one minute bars (`2 × bb_n`: the first band at bar 20, then 20 bars of middle history), which also covers RSI and ATR; the first signal can come on the 41st bar close. With the gate, 28 five minute bars of ADX (140 minutes), whichever is later.

## Study files

| File | Lab | Differs by |
|---|---|---|
| `apps/lab/studies/bollinger_rsi_screen_v4.toml` | candle | no regime gate, `max_spread_bps` unset (replaces `bollinger_rsi_screen.toml`, `bollinger_rsi_screen_v2.toml` and `bollinger_rsi_screen_v3.toml`, all void, see index tasks 12b, 12c and 12d) |
| `apps/lab/studies/bollinger_rsi_screen_adx_v2.toml` | candle | `regime_adx_max = "25"`, `max_spread_bps` unset (replaces `bollinger_rsi_screen_adx.toml`, void, see index task 12d) |
| `apps/lab/studies/bollinger_rsi.toml` | replay | `regime_adx_max = "25"`, `max_spread_bps` = the audit p95 (index AC-3) |

All other fields per index AC-10. Scratch run files for index AC-16 (not baked into the image): `lab-reports/scratch/bollinger_rsi.toml` (2025-09-01) and `lab-reports/scratch/bollinger_rsi_candle.toml` (2025-06), both with `min_target_bps` and `regime_adx_max` unset.

## Revision 1: `bollinger_rsi_r1` (fourth amendment, 2026-10-22)

Both screens failed (index `verify.md`). The ungated screen `bollinger_rsi_screen_v4` passed the Sharpe (0.068) and the fee hurdle (edge 2.36 USD against 1.5 × 1.39 USD) and failed only on trade rate (0.31 a day against 17). The gated screen failed on Sharpe as well, so the revision starts from the ungated rules.

- **The one design change**: the exit. The target becomes a resting post only limit (index AC-19, `exit_mode = "maker"`), so a target exit pays the 2 bps maker fee instead of the 5 bps taker fee. The reward floor follows from it by the fixed rule (1.5 × the 4 bps round trip), `min_target_bps = "6"`. Every other rule, the fixed config, and the grid are exactly the v4 screen's.
- **How it is judged**: on the candle gate path (index AC-20): `lab screen` with the 0.17 a day floor, then the candle holdout year and `gate evaluate`. The 17 a day floor came from the 12 day replay holdout, and this revision never uses it.
- **Evidence it cites**: development screen figures only. No holdout figure exists for any scalping study.
- **What would kill it**: a walk forward Sharpe at or below 0, or an edge under 1.5 × cost, on the revision's own screen; then the candidate is closed.

| File | Lab | Differs from `bollinger_rsi_screen_v4.toml` by |
|---|---|---|
| `apps/lab/studies/bollinger_rsi_r1.toml` | candle | `exit_mode = "maker"`, `min_target_bps = "6"`, `[study] name = "bollinger_rsi_r1"`, `candle_gate = true`; its header comment says it holds out |

Its results go in `verify.md` like the others, with the screen first, then the promotion id, verdict, reasons, the family N, and the holdout trades.

## Rationale (short)

Picked as the first bar idea because it has the fewest free settings of the scope's bar ideas, its exit (the middle band) comes from the idea itself, and it directly tests the scope's range regime question twice (flat bands, ADX). The gated version goes to replay because the hypothesis says it fails in trends. Runner up: the EMA pullback, left in the scope because it overlaps the VWAP candidate.
