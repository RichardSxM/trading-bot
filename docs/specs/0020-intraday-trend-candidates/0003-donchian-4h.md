# 0020.3 Donchian channel trend on 4 hour bars (`donchian_4h`)

## Summary

The classic "turtle" trend rule on 4 hour bars. Go long when a close breaks above the highest high of the previous 20 (or 55) bars, short when it breaks below the lowest low. Exit when a close breaks the opposite channel of half that length. The stop sits 2 or 3 ATR away, and no trade lasts more than 10 days. It holds for days, so each winner aims for a move far above the cost.

## Hypothesis

- **Why it might work:** "Catching Crypto Trends" (Zarattini, Pagani, Barbon, 2025) finds Donchian channel trend models on crypto give a Sharpe above 1.5 after fees (on a 20 coin portfolio; BTC alone is weaker). Multi day trends are the best documented crypto edge, and a move of several percent dwarfs 14 bps.
- **When it should work:** long trending stretches (2023 to 2024 style rallies, sharp sell offs).
- **When it should fail:** range bound months, when every break reverses; and if BTC's holdout year is flat or choppy.
- **No trade:** until `entry_bars` earlier bars and ATR(14) exist; while a position is open; on the bar the base exits.

## Rules

`trading_core/strategy/donchian.py`. Per bar, the channels are read **before** the current bar is added:

- `entry_high` / `entry_low`: the highest high and lowest low of the previous `entry_bars` bars. `exit_bars = entry_bars ÷ 2` rounded down (10 or 27); `exit_high` / `exit_low`: the same over the previous `exit_bars` bars.
- **Long signal:** `close > entry_high`. Reason `donchian_long`. Structural stop `close − stop_atr × atr`. **Short signal:** `close < entry_low`. Reason `donchian_short`. Structural stop `close + stop_atr × atr`. The base's 1 ATR floor is closer, so the structural stop is the one used (unless the mid moved).
- **Exit:** a long exits when `close < exit_low`, a short when `close > exit_high`, reason `channel_exit`.
- **Time stop:** `time_stop_bars` = 60 (10 days), in the base.
- After reading, the current bar's high and low are appended to both windows (deques with `maxlen` `entry_bars` and `exit_bars`).
- **`values`:** `entry_high`, `entry_low`, `exit_high`, `exit_low` (each `None` until its window is full).
- **Constructor:** `DonchianRule(*, entry_bars, stop_atr)`. `stop_atr` is a `Decimal`. All maths uses the default `Decimal` context with no rounding inside the rule; `floored_stop` rounds the stop to the tick.

## Setup state

The two windows and the last ATR. `ready()` is true once the entry window is full and an ATR has been seen, and never turns false. The signal is a level condition, so `consumed()` does nothing. In principle a close still beyond the channel after an exit may enter again, but in practice that is rare: the base never enters on the exit bar, and by the next bar the window includes the exit bar's own high or low. So a reversal (a long's `channel_exit` in a crash, then a short) comes at least one bar late, and often not at all. `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"4-HOUR-LAST"` | no | must be `4-HOUR-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `time_stop_bars` | 60 | no | base |
| `entry_bars` | | `[20, 55]` | ≥ 2 |
| `stop_atr` | | `["2", "3"]` | ≥ 1 |

4 grid points. Config defaults for the grid fields are the grid's first point.

## Tests (AC-7)

- A rising fixture: the first close above the previous 20 bar high gives a long, stop `close − 2 × ATR`.
- The mirror case gives a short.
- A close equal to the previous high gives no signal (strictly above).
- The current bar's own high is not in the channel it is compared against.
- A long exits with `channel_exit` at the first close below the previous 10 bar low.
- A long still open after 60 bars exits with `time_stop` (base).
- No signal before 20 earlier bars.
- A long that exits with `channel_exit` on a crash bar gets no short on that bar; the next bar gives a short only if its close is below the updated 20 bar low, which now includes the crash bar.
- `entry_bars = 1`, `stop_atr = "0.5"`, and `bar_spec = "1-HOUR-LAST"` are refused by the config.

## Expected and risks

Few trades: perhaps 30 to 80 a year at 20 bars and 15 to 30 at 55, so the holdout may fall under the 60 trade floor and give `insufficient`. That is an honest result, not a reason to lower the floor. Holds of days pay funding, which the lab charges. A valid revision under AC-12 is a switch to `1-HOUR-LAST` bars for more trades.

## Short rationale

The best documented crypto trend rule, with only two settings, and a timescale none of the earlier candidates tested. It is independent of candidates 1 and 2, so the checkpoint keeps it even if they fall below cost.

## Revision 1 (`donchian_4h_r1`, holdout seen)

Asked for by the trader on 2026-10-27, after the `fail` below. Written and committed before the revision's first `lab study` run.

**What changes:** the rule runs on `1-HOUR-LAST` bars instead of `4-HOUR-LAST`. The channel lengths stay the same counts of bars (`entry_bars` 20 or 55, exit half of that), so a channel now spans 20 or 55 hours instead of 80 or 220. The time stop stays at 10 days, which is now `time_stop_bars` 240. The stop grid (`stop_atr` 2 or 3, on the 1 hour ATR(14)), the rule code (`DonchianRule`, unchanged), the base, and every shared study setting stay as they are. 4 grid points, so N grows by 4.

**Why, from development and walk forward evidence only:**
- Too few trades for the gate's statistics. The best development point (20 bars, 2 ATR) made 209 trades in about 3.5 years, about 60 a year, right at the 60 trade floor, and the 55 bar points made only 115 to 120. The walk forward's 5 folds each held only a few dozen trades, so one fold could carry the whole result. A 1 hour bar gives about 4 times the signals.
- Shorter channels did better in the development range: 20 bars made +24.5% and +16.5%, 55 bars +0.9% and −3.6%. A 20 hour channel continues in that direction.
- Cost still looks covered. The development median 4 hour ATR is about 9.9 times the round trip cost, so a 1 hour ATR (roughly half) puts a 2 ATR stop still well above the 14 bps cost.

**Risk:** faster channels whipsaw more, so gross per trade will shrink, and the revision may fall below cost the way `noise_breakout` did. It runs once as `donchian_4h_r1` in its own node module `trading_core/node/donchian_4h_r1.py`, its results are labelled "holdout seen" (its holdout is the year whose verdict prompted it), and it is final.

## Results

**Verdict: `fail`.**

| Field | Value |
|---|---|
| Study | `donchian_4h` (study file committed 2026-10-26 in 86c928f, study batch `01a178e9-a6ad-713f-804c-105853b43e83`) |
| Promotion id | `01a17a79-2207-7512-944c-92079a9cc065` (VPS) |
| Evaluation date | 2026-10-26 |
| Chosen grid point | 1 of 1 to 4 (`entry_bars` 20, `stop_atr` 2) |
| Walk forward Sharpe | 0.133824 (5 folds; return +1.49%) |
| Holdout Sharpe | 0.399981, against buy and hold −0.460362 and the MACD baseline −1.377988 |
| Holdout return after costs | +2.78% |
| Holdout marked max drawdown | 7.45% |
| Deflated Sharpe probability | 0.000000 |
| PBO | 0.000000 (`pbo_applicable` true) |
| Holdout trades | 64 (31 long, 33 short; win rate 25.00%) |
| Trial count N, trial pool | 23, 23 (`kind+lab`, VPS) |
| `avg_hold_minutes` | 4 614.6 (longest 14 400, the 10 day time stop) |
| Holdout gross per trade | 6.109 USD |
| Holdout cost per trade | 1.845 USD (ratio 3.31) |

Reasons that failed (code, value, threshold):
- `concentration_fold`: `failed`, 8.250355, 0.60
- `bootstrap_lower_bound`: `failed`, −0.000314, 0
- `deflated_sharpe`: `failed`, 0.000000, 0.95
- `concentration_direction`: `failed`, 1.045410, 0.60
- `concentration_top_trades`: `failed`, 6.248314, 0.60

Every other check passed, among them the fee hurdle, holdout Sharpe above 0, beating both baselines, walk forward Sharpe above 0, and the drawdown budget.

From the holdout run's `decisions` (64 closed trades):
- Exits by reason: `channel_exit` 30 (46.9%), `time_stop` 4 (6.3%), stop fill 30 (46.9%, the positions with no exit decision).
- Rule exits within 2 bars of entry: 0.
- `BAD_SIGNAL` refusals: 0 (all 98 decisions approved).
- Warmup bars skipped: 20 (the entry window, 2025-10-19 00:00 to 2025-10-22 08:00 UTC; ATR(14) is ready inside it). First decision 2025-10-26 12:00 UTC.
- Net by side: longs +290.35 USD, shorts −12.61 USD.

Reading: this is the first candidate in the family whose signal clears its cost. Each trade made 6.11 USD gross against 1.85 USD of cost (3.3 ×), and the holdout beat buy and hold and MACD in a year when BTC fell. But the profit is thin and lopsided. Longs made more than all of it (direction share 1.05), one walk forward fold made 8.25 times the total, and the top trades made 6.25 times the total, so the rest of the trades lost money overall. With 64 trades the bootstrap lower bound sits just below 0, and at N 23 the deflated Sharpe is 0. In plain words, a few big trend trades carried a rule that otherwise bled, which is what a trend rule looks like, but one year and 64 trades are too few to tell that apart from luck.
