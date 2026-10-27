# 0020.1 Noise area breakout on 30 minute bars (`noise_breakout`)

## Summary

Each UTC day, draw bands around the day's opening price, as wide as BTC's average move from the open at that time of day over the last 14 days (the "noise area", where price usually wanders). When a 30 minute close is above the upper band and above the day's VWAP (volume weighted average price), go long; below the lower band and below the VWAP, go short. Stay in while price stays beyond that line, exit on the first close back inside it, enter again whenever a close breaks out again, and never hold more than 8 hours.

## Hypothesis

- **Why it might work:** moves that leave the usual intraday range often keep going for hours, as traders chase and stops cascade. Concretum's 2025 study reports a gross Sharpe of about 1.6 for BTC intraday trend models over 2018 to 2025, and a peer reviewed study finds BTC intraday momentum holds out of sample. With no fixed target, a winning trend day can earn many times the 14 bps cost.
- **When it should work:** news or flow days with one clear direction.
- **When it should fail:** choppy days, when price pokes out of the band and falls back inside again and again. Each such poke pays the full round trip, and re-entry is unlimited, so this is the likely way it fails.
- **No trade:** until each slot has 14 earlier days of history (about 15 days of warmup); on a day whose 00:00 bar is missing; while a position is open; on the bar the base exits.

## Rules

Per bar, with `bar_open_ns = ts_ns − bar_interval_ns`:

- `vwap = DayVwap.update(bar_open_ns, high, low, close, volume)`, `sigma = NoiseBands.update(bar_open_ns, open, close)`.
- When `day_open` and `sigma` are both set: `upper = day_open × (1 + band_mult × sigma)`, `lower = day_open × (1 − band_mult × sigma)`. Otherwise both are `None`.
- `long_line = max(upper, vwap)` and `short_line = min(lower, vwap)`, each `None` when any input is `None`.
- **Day memory:** every `update` appends the bar's UTC open date to `days`, a deque with `maxlen = time_stop_bars + 2`, and sets `day_lines[that date] = (long_line, short_line)`, the latest bar's values **even when they are `None`**. So `day_lines[d]` ends up holding day `d`'s last bar's lines. Dates no longer in `days` are dropped from `day_lines`.
- **Long signal:** `long_line` set and `close > long_line`. Reason `noise_long`. Structural stop `long_line`. **Short signal:** `short_line` set and `close < short_line`. Reason `noise_short`. Structural stop `short_line`. (Both cannot hold at once, since `upper > lower`.) Said plainly: the line sits close to the close, so the base's 1 ATR floor almost always replaces it, and the stop actually placed is 1 ATR from the mid. The structural stop mostly records the rule's intent. If the quote mid has already fallen to or below `long_line`, the base refuses the entry as `BAD_SIGNAL`, and the next bar may signal again. How often that happens is unknown, so the results count it (index AC-10).
- **Exit** (asked only in a position, after `update`): `entry_day = days[-1 - trade.bars_held]`, the open date of the bar that decided the entry. This counts processed bars, as `bars_held` does, so a gap in time does not shift it. If `bars_held` reaches past the oldest entry, the oldest date is used. If `entry_day` is the current bar's date, the line is the current `long_line` (long) or `short_line` (short). Otherwise it is `day_lines[entry_day]`, the entry day's last lines, held fixed. A long exits when `close < line`, a short when `close > line`, reason `noise_trail`. A `None` line, including a frozen one that was `None` at the entry day's last bar, gives no rule exit. The resting stop and the time stop still protect the trade.
- **Time stop:** `time_stop_bars` = 16, in the base. It counts bars, not hours: it fires at the 16th bar close after the decision, about 8 hours plus the fill delay when no bar is missing. The base checks it before the rule's exit.
- **`values`:** `day` (ISO date), `slot`, `day_open`, `sigma`, `upper`, `lower`, `vwap`, `long_line`, `short_line`, `exit_line` (the line the last exit check used) (each `None` when unset).
- **Constructor:** `NoiseBreakoutRule(bar_interval_ns, *, lookback_days, band_mult, time_stop_bars)`, the interval passed positionally as `ema_pullback`'s node does. `band_mult` is a `Decimal`. All maths uses the default `Decimal` context, with no rounding inside the rule (tick rounding of the stop is `floored_stop`'s). Comparisons are exact.

## Setup state

`DayVwap` and `NoiseBands` state, `days`, `day_lines`, and a `warm` flag. `ready()` returns `warm`, which is set the first time any slot's `sigma` is not `None` and never cleared (the base skips exits and the time stop while `ready()` is false). The signal is a level condition, not a stored setup, so `consumed()` does nothing and the same condition may signal again on the next bar (unlimited re-entry, the trader's choice). `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"30-MINUTE-LAST"` | no | must be `30-MINUTE-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `time_stop_bars` | 16 | no | base |
| `lookback_days` | 14 | no | ≥ 1 |
| `band_mult` | | `["1", "1.5", "2"]` | > 0 |

3 grid points (the trader's choice, around the published setting of 1 with 14 days). Config defaults for the grid field are the grid's first point.

## Tests (AC-5)

- A fixture of 15 quiet days, then a day that rises steadily from 10:00 UTC: a long signal at the first close above both the upper band and the VWAP, with structural stop `long_line`.
- The mirror case gives a short.
- A close above the upper band but below the VWAP gives no signal.
- A long whose close falls back below `long_line` exits with `noise_trail`; on the next bar, a close above the line again signals a new long (re-entry).
- A long entered at 22:30 UTC and still open after 00:00 trails against the entry day's frozen line, not the new day's (a close above the frozen line but below the new day's upper band does not exit).
- With one bar missing between entry and the current bar, `entry_day` still names the decision bar's date.
- A long entered on a day whose last bar had a `None` line, still open after 00:00, gets no `noise_trail` exit, and the time stop still closes it.
- A long still open 16 bars after entry exits with `time_stop` (base).
- A day with no 00:00 bar gives no signal all day, and `ready()` stays true.
- `ready()` turns true at the first non `None` `sigma` and stays true through a later day with no bands.
- No signal before 14 earlier days of history for that slot.
- `band_mult = 0`, `lookback_days = 0`, and `bar_spec = "15-MINUTE-LAST"` are refused by the config.

## Expected and risks

Somewhere between a few hundred and a few thousand holdout trades, far above the 60 trade floor. At `band_mult` 1 a large share of closes sit outside the band (task 1 measures it), and early in the UTC day the band is narrow and the VWAP rests on one or two bars. So churn near midnight is the likely failure. The trader chose to keep the published rule (no wait before trusting the VWAP); a revision under index AC-12 can add one if the results show it hurt. The checks to watch are `fee_hurdle` and `holdout_sharpe`, the share of `noise_trail` exits within 2 bars (fast whipsaws), and the `BAD_SIGNAL` count. Holding overnight and re-entering without limit are departures from the published design, both chosen by the trader.

## Short rationale

This is the published intraday trend rule with the fewest free settings (one band width, one lookback), it needs only two small helpers on the frozen base, and its win condition is the opposite of every failed candidate: few large moves held, not many small ones taken.

## Results

**Verdict: `fail`.**

| Field | Value |
|---|---|
| Study | `noise_breakout` (study file committed 2026-10-26 in b830c83, study batch `01a1776b-cae8-72e4-8d4d-e320988a34e9`) |
| Promotion id | `01a17847-1da0-7077-bab3-07b324da327c` (VPS) |
| Evaluation date | 2026-10-26 |
| Chosen grid point | 2 of 0 to 2 (`band_mult` 2) |
| Walk forward Sharpe | −3.092398 (5 folds; return −69.59%) |
| Holdout Sharpe | −2.361946, against buy and hold −0.460362 and the MACD baseline −1.377988 |
| Holdout return after costs | −29.30% |
| Holdout marked max drawdown | 31.42% |
| Deflated Sharpe probability | 0.000000 |
| PBO | not applicable (gate reports `pbo_applicable` false) |
| Holdout trades | 483 (238 long, 245 short; win rate 25.05%) |
| Trial count N, trial pool | 19, 19 (`kind+lab`, VPS) |
| `avg_hold_minutes` | 163.3 (longest 480) |
| Holdout gross per trade | 0.188 USD |
| Holdout cost per trade | 6.277 USD |

Reasons (code, value, threshold):
- `fee_hurdle`: `failed`, 0.188, 9.416
- `holdout_sharpe`: `failed`, −2.361946, 0
- `beats_buy_and_hold`: `failed`, −2.361946, −0.460362
- `beats_macd_baseline`: `failed`, −2.361946, −1.377988
- `walk_forward_sharpe`: `failed`, −3.092398, 0
- `bootstrap_lower_bound`: `failed`, −0.001622, 0
- `deflated_sharpe`: `failed`, 0.000000, 0.95
- `drawdown_budget`: `failed`, 0.3142, 0.10

From the holdout run's `decisions` (483 closed trades):
- Exits by reason: `noise_trail` 296 (61.3%), `time_stop` 80 (16.6%), stop fill 107 (22.2%, the positions with no exit decision).
- Rule exits within 2 bars of entry: 170, which is 57.4% of the `noise_trail` exits and 35.2% of all trades.
- `BAD_SIGNAL` refusals: 0 (all 859 decisions approved).
- Warmup bars skipped: 672 (the first 14 UTC days, 2025-10-19 to 2025-11-01, until every slot's `sigma` has 14 days of history; first decision 2025-11-03 01:00 UTC).

Reading: the signal barely moves before costs. On the holdout each trade made 0.19 USD gross against 6.28 USD of cost (0.03 ×), and in the development range all three grid points were negative even before costs (−0.61 to −0.93 USD per trade). So this is not only a fee problem: on 30 minute bars the breakout past the noise band did not keep going often enough. Over a third of trades were whipsaws, closed back inside the line within 2 bars, which is the churn the expected risks named. The development range grid lost 78% to 99%, with fewer losses at wider bands (fewer trades).

Final family N (AC-13, 2026-10-27): N 27, pool 27 (`kind+lab`, VPS), verdict `fail`, promotion `01a17c48-bc35-7026-8382-08ceb78914ec`. The same 8 checks failed, with the same holdout Sharpe (−2.361946) and deflated Sharpe probability (0.000000).
