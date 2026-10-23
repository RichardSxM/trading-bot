# Verify: scalping strategy candidates · spec 0017 · updated 2026-10-22
_Results and verify steps for spec 0017. The audit record below was written before any candidate study ran (AC-3)._

## Audit record (AC-3)

- **Batch folder**: `lab-reports/audit-20261021/` (`audit.json`, `audit.md`), generated 2026-10-21T15:34:17Z over the 43 development days 2022-04-01 to 2025-10-01.
- **Pooled spread p95**: 6.2615 bps. Every child's replay run file sets `max_spread_bps = "6.27"` (rounded up to 2 places), never changed afterwards.
- **Median absolute 5 minute move**: 5.8884 bps (1 minute 2.4704, 15 minute 10.2615).
- **`reachable`**: `maker_entry` (7 bps, needs 10.5) false; `taker_entry` (10 bps, needs 15) false.
- **Run order**: all three candidates enter maker, so the flag warns about all of them and moves none (Order rule, step 2). The order stands: the four candle screens (`bollinger_rsi_screen`, `bollinger_rsi_screen_adx`, `vwap_retest_screen`, `vwap_retest_screen_adx`) first; then replay studies one at a time, `bollinger_rsi`, then `vwap_retest`, each only if its screen passed; then `delta_divergence` once its AC-17 pre check passes.

Warning, in plain words: the typical 5 minute move is smaller than the 10.5 bps a maker round trip needs to clear 1.5 times its cost. Only 28.5 % of 5 minute moves and 49.2 % of 15 minute moves reach 10.5 bps. A candidate has to pick its moments well to pass; the reward floor (`min_target_bps`) already refuses targets closer than that.

Notes for later (no effect on the order or the cap):

- The pooled p95 is driven by the thin 2022 days (day p95 up to 16.2 bps). Since 2023-10 every day's p95 is under 2 bps, so the 6.27 cap mostly binds on 2022 days.
- The per day mean absolute funding rate jumps by about five orders of magnitude between 2022-09-01 (0.24) and 2022-10-01 (0.0000055). This looks like a unit change in the recorded funding feed, not the market. No gate reads this column; check it before any later work uses funding.

## Both labs (AC-16)

Run 2026-10-21 to 2026-10-22 with `LAB_WORKERS=2`, branch code at 9464aa6 mounted into the lab container, scratch run files under `lab-reports/scratch/` (`min_target_bps` and `regime_adx_max` unset).

| Run | Range | Exit | Closed trades | Net P&L (USD) | Batch |
|---|---|---|---|---|---|
| `lab replay` `bollinger_rsi` | 2025-09-01 | 0 | 4 | -10.08 | `01a160d9-acff-7255-9fcf-76eff9e421e3` |
| `lab replay` `vwap_retest` | 2025-09-01 | 0 | 9 | -14.26 | `01a16135-692d-732b-8580-7384ab6d0dcf` |
| `lab replay` `delta_divergence` | 2025-09-01 | 0 | 14 | -13.05 | `01a16158-6100-7712-99b6-56dff9efabd9` |
| `lab backtest` `bollinger_rsi` | 2025-06 | 0 | 3 | 7.89 | `01a16188-ee3e-7703-b0fa-112d133ecc3a` |
| `lab backtest` `vwap_retest` | 2025-06 | 0 | 2 | 5.83 | `01a1618a-671c-7597-b93f-9f8b3dbeaa9b` |
| `lab backtest` `delta_divergence` | 2025-06 | 64 | none | none | refused: "is a scalping strategy; lab backtest runs hour_level strategies and the scalping candidates with a candle screen" |

Determinism rerun: `bollinger_rsi` over 2025-09-01 again (batch `01a1618c-4932-742e-9540-9f7f9ad262cb`). Its `base.json` matches the first run field for field, apart from `batch_id`, `run_id`, `started_at`, `ended_at`, and `run_config.git_commit`. Result: **pass**. These are scratch runs, so P&L means nothing here.

Heads up on `git_commit`: the two runs recorded `bd8cd13` and `0ffc03d`, neither of which is this branch. The value is baked into the shared lab image at build time, and other sessions rebuilt it between runs. The code that ran was this branch's (it was mounted), so the result holds, but the label is wrong. Before any study runs, rebuild the lab image from this branch with `GIT_COMMIT` set, so study reports carry the right commit.

## Delta trade rate pre check (AC-17)

Run file `lab-reports/scratch/delta_divergence_precheck.toml` (sha256 `30e31d8f…2433`): `lookback_bars = 5`, `move_atr = "1.5"`, `min_target_bps = "10.5"`, `regime_adx_max = "25"`, `max_spread_bps = "6.27"`, one `lab replay` per day.

| Day | Approved | `blocked:regime` | `target_too_close` | `invalid_stop` | Closed trades | Batch |
|---|---|---|---|---|---|---|
| 2022-10-01 | 6 | 65 | 155 | 2 | 3 | `01a161be-ac1e-7213-9dcc-f5990466572c` |
| 2023-10-01 | 0 | 151 | 98 | 0 | 0 | `01a161c9-4d4b-739e-8111-1b833facf343` |
| 2024-10-01 | 5 | 63 | 29 | 0 | 4 | `01a161d6-9b87-753f-baef-a8869b94ec04` |
| 2025-10-01 | 0 | 50 | 54 | 0 | 0 | `01a161f9-b0b5-7405-9001-7f427d6c7b3d` |

Approved `enter` decisions average 2.75 a day (11 over 4 days), under the 17 needed. **`delta_divergence`: not run: trade rate.** Its replay study does not run.

In plain words: the signal fires often enough (about 60 to 250 times a day), but the reward floor and the ADX regime filter refuse nearly all of it. That fits the audit's warning above. Changing the candidate is a job for an `/architect` amendment (one revision per candidate), not this build.

## Candle screens (AC-9, task 13)

### Void: `bollinger_rsi_screen` (2026-10-22, before AC-18)

Started 08:53 UTC on `kfs/lab:s0017` (commit 8c7a852) and stopped by hand at about 10:15 UTC. In early February 2023 a maker entry filled while the bar's quote was already past the stop, so the stop was rejected ("in the market") and `STOP_NOT_ACCEPTED` blocked entries for the rest of the run. 17,232 later signals were refused with `blocked:stop_not_accepted`.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `597be33c-e5d6-4bba-bbf4-a146451d3e69` | 10 | 1.41 |
| grid-2 | `0d4eee3d-c7f4-4465-ac03-d0db5913963c` | 9 | −37.37 |
| grid-3, grid-4 | stopped partway | | |

No `lab screen` ran on it. Its rows stay in Postgres and still count toward `bollinger_rsi`'s trial count N. Spec amendment AC-18 (commit 00d5586) fixed the base in e6c21ac, and the screen reruns as `bollinger_rsi_screen_v2` (index task 12b).

### Void: `bollinger_rsi_screen_v2` (2026-10-22, fill check only)

Started 11:01 UTC on `kfs/lab:s0017` (commit e6c21ac, batch `01a16348-352e-76c2-95d2-a1488dcc3388`) and stopped by hand at about 11:45 UTC, with no grid point finished. The fill check fired once, but 4 stops were still rejected as in the market, one in each grid point (for example a sell stop at 22966 with the venue's bid at 22955, 2023-02-01). The backtest engine shows each quote to the simulated venue before the strategy, so the strategy's cached quote still allowed the stop. 19,288 signals were refused with `blocked:stop_not_accepted`.

No `lab screen` ran on it, and its rows count toward N like the first. The second AC-18 amendment (the next quote settles a rejected stop, index task 12c) fixed it, and the screen reruns as `bollinger_rsi_screen_v3`.

### Void: `bollinger_rsi_screen_v3` and `bollinger_rsi_screen_adx` (2026-10-22, next quote verdict only)

`bollinger_rsi_screen_v3` started 13:33 UTC on `kfs/lab:s0017` (commit 8d24813, batch `01a163d2-2cd7-7424-aacf-652ba55687ef`) and finished all four grid points at 14:24 UTC. The fill check fired once. 3 stops were still rejected as in the market (all sell stops, the venue's bid below the stop, for example 2023-01-24 and 2023-02-03), and none of them was settled as through, so `STOP_NOT_ACCEPTED` blocked entries after the first rejection in each grid point. 16,246 signals were refused with `blocked:stop_not_accepted`.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `0a8272ee-7724-4634-9bd1-2673378d69de` | 10 | 1.41 |
| grid-2 | `9a04b97b-8b10-4ed3-a1f2-207cb3c47aae` | 9 | −37.37 |
| grid-3 | `a51e8662-6a73-4076-b6ce-6a83a3d3ac93` | 16 | −4.58 |
| grid-4 | `ffacd759-f201-4b78-bde2-2370f59540de` | 18 | −20.52 |

`lab screen bollinger_rsi_screen_v3` ran and said fail (exit 3: `missing:walk_forward_sharpe`, `fee_hurdle`, `trades_per_day`), but that verdict is void because the block, not the strategy, set the trade count.

The cause: the candle lab sends a minute's low as one quote, and the next quote is the close, back above the stop. That low fills the resting maker entry while the strategy's cache still holds the older quote, so the stop goes out. The strategy then receives the low quote, and only after it the venue's rejection. The next quote, which the second amendment waited for, was back above the stop, so the rejection looked unexplained. A node test reproduced it (one quote dip to 84700 under a stop at 84800, the venue's reason `bid=84699`, the next quote at 84999). The third AC-18 amendment (index task 12d) also checks the cached quote when the rejection arrives, and the screen reruns as `bollinger_rsi_screen_v4`.

`bollinger_rsi_screen_adx` started next at 14:24 UTC on the same image (batch `01a16400-b6de-761b-af30-618dd62933b3`) and was stopped by hand at about 14:45 UTC with no grid point finished. It was already refusing signals with `blocked:stop_not_accepted`, so it is void too and reruns as `bollinger_rsi_screen_adx_v2`. The rows of both studies stay in Postgres and count toward N.

### `bollinger_rsi_screen_v4` (2026-10-22): fail, trade rate

Started 15:27 UTC on `kfs/lab:s0017` (commit 9492219, batch `01a1643a-7b9a-7542-ac7c-a1543e78bb49`) and finished all four grid points at 16:47 UTC (exit 0, 4,779 s). 142 rejected stops were settled as `stop_through` exits, and no signal was refused with `blocked:stop_not_accepted`, so the third AC-18 amendment held.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `2ab79d1e-aca0-4d22-962b-66192ffe294a` | 470 | −155.13 |
| grid-2 | `bd1010a1-0047-42f8-9c21-f7a7b46101b6` | 241 | 159.63 |
| grid-3 | `5b1904d0-2b33-4b32-b18d-5a3a4520121c` | 455 | −31.54 |
| grid-4 | `76977975-d160-4afa-941d-21bb9d4dbd47` | 312 | 301.32 |

`lab screen bollinger_rsi_screen_v4`: **fail** (exit 3), reasons `trades_per_day` only, final grid point 3 (grid-4).

| Figure | Value | Needs |
|---|---|---|
| Walk forward Sharpe | 0.067923 | > 0 (pass) |
| Avg gross edge (USD per trade) | 2.3601 | ≥ 1.5 × cost = 2.0915 (pass) |
| Avg round trip cost (USD per trade) | 1.3944 | |
| Trades per day | 0.3053 (312 over 1,022 days) | ≥ 17 (fail) |

Recorded as **trade rate**: the signal clears the Sharpe and the fee hurdle, but fires about 55 times too rarely. `bollinger_rsi`'s replay study does not run from this screen; its result rests on `bollinger_rsi_screen_adx_v2`.

### `bollinger_rsi_screen_adx_v2` (2026-10-22): fail

Started 16:48 UTC on `kfs/lab:s0017` (commit 9492219, batch `01a16485-109d-73ca-9b57-3d3acf13ab68`) and finished all four grid points at 19:35 UTC (exit 0, 10,048 s). 74 rejected stops were settled as `stop_through` exits, and no signal was refused with `blocked:stop_not_accepted`, so the third AC-18 amendment held.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `07c775c7-ad26-4b68-b647-5153366d7e66` | 221 | 45.13 |
| grid-2 | `ff07584b-0e12-40f4-b74f-3ad0d865773a` | 121 | 118.87 |
| grid-3 | `c5a3930e-6506-49e3-935a-ae6a37754de2` | 206 | −62.19 |
| grid-4 | `cd255e9e-846d-4a1c-bb6e-2b4bf5523afc` | 142 | 46.20 |

`lab screen bollinger_rsi_screen_adx_v2`: **fail** (exit 3), reasons `walk_forward_sharpe` and `trades_per_day`, final grid point 1 (grid-2).

| Figure | Value | Needs |
|---|---|---|
| Walk forward Sharpe | −0.804244 | > 0 (fail) |
| Avg gross edge (USD per trade) | 2.3813 | ≥ 1.5 × cost = 2.0983 (pass) |
| Avg round trip cost (USD per trade) | 1.3989 | |
| Trades per day | 0.1184 (121 over 1,022 days) | ≥ 17 (fail) |

Not a trade rate only fail: the ADX filter halves the trade count and the walk forward Sharpe turns negative. Neither `bollinger_rsi` screen passed, so **`bollinger_rsi`: not run: screen fail** (AC-11). Its replay study does not run.

### `vwap_retest_screen` (2026-10-22): fail

Started 19:36 UTC on `kfs/lab:s0017` (commit 9492219, batch `01a1651e-0750-75b7-9765-d852650d6d5b`) and finished all four grid points at 20:48 UTC (exit 0, 4,364 s). 3 rejected stops were settled as `stop_through` exits, and no signal was refused with `blocked:stop_not_accepted`, so the third AC-18 amendment held.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `d95465ec-1bf9-4d71-a25c-c549357c2a6e` | 274 | −85.58 |
| grid-2 | `fbf049af-52ed-4b00-a775-44d702f724c0` | 273 | −188.99 |
| grid-3 | `131570f2-14a5-4509-a267-438fd6759888` | 268 | −336.25 |
| grid-4 | `203955a5-3c1f-430f-abf0-8b81a4fbf6d3` | 268 | −465.74 |

`lab screen vwap_retest_screen`: **fail** (exit 3), reasons `walk_forward_sharpe`, `fee_hurdle` and `trades_per_day`, final grid point 0 (grid-1).

| Figure | Value | Needs |
|---|---|---|
| Walk forward Sharpe | −0.604977 | > 0 (fail) |
| Avg gross edge (USD per trade) | 1.0830 | ≥ 1.5 × cost = 2.0961 (fail) |
| Avg round trip cost (USD per trade) | 1.3974 | |
| Trades per day | 0.2681 (274 over 1,022 days) | ≥ 17 (fail) |

Fails all three tests: every grid point loses money, and the gross edge does not even cover one round trip's cost. `vwap_retest`'s replay study now rests on `vwap_retest_screen_adx`.

### `vwap_retest_screen_adx` (2026-10-22): fail

Started 20:49 UTC on `kfs/lab:s0017` (commit 9492219, batch `01a16561-1aba-77cc-95b7-69066ed2afad`) and finished all four grid points at 22:18 UTC (exit 0, 5,324 s). 3 rejected stops were settled as `stop_through` exits, and no signal was refused with `blocked:stop_not_accepted`, so the third AC-18 amendment held.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `25fa0e75-b567-444a-b22b-c07f5a0ba140` | 173 | −117.15 |
| grid-2 | `9c03b78f-880d-4a81-bd24-de8cd93b4776` | 173 | −235.76 |
| grid-3 | `a75afc06-1302-45e6-a290-470b4d419c79` | 172 | −305.98 |
| grid-4 | `a2885ffd-ae84-4ef4-9a2c-d7de71e87854` | 172 | −397.79 |

`lab screen vwap_retest_screen_adx`: **fail** (exit 3), reasons `walk_forward_sharpe`, `fee_hurdle` and `trades_per_day`, final grid point 0 (grid-1).

| Figure | Value | Needs |
|---|---|---|
| Walk forward Sharpe | −1.358916 | > 0 (fail) |
| Avg gross edge (USD per trade) | 0.7173 | ≥ 1.5 × cost = 2.0959 (fail) |
| Avg round trip cost (USD per trade) | 1.3973 | |
| Trades per day | 0.1693 (173 over 1,022 days) | ≥ 17 (fail) |

The ADX filter makes it worse: fewer trades and a smaller edge. Neither `vwap_retest` screen passed, so **`vwap_retest`: not run: screen fail** (AC-11). Its replay study does not run.

### Task 14 (replay studies): none run

All four candle screens failed, so no replay study ran (AC-11). `bollinger_rsi` continues as `bollinger_rsi_r1` on the candle gate path (AC-19 to AC-21), and `book_imbalance` takes the replay slot (AC-22 to AC-24).

### `bollinger_rsi_r1` (2026-10-22): fail

The candle gate study (AC-20, AC-21): v4's grid with maker target exits (`exit_mode = "maker"`, `min_target_bps = "6"`). Started 22:27 UTC on `kfs/lab:s0017` (commit f115da6, batch `01a165bb-1b96-722f-9abb-297f6956af26`) and finished all four grid points at 23:33 UTC (exit 0, 3,988 s). 142 rejected stops were settled as `stop_through` exits, and no signal was refused with `blocked:stop_not_accepted`.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `a6783fa5-a1a1-4f7a-8b19-1f29a95fdd8a` | 473 | −361.11 |
| grid-2 | `80bc0b42-09fd-4b4c-9bdf-7024bb5e719f` | 241 | 38.88 |
| grid-3 | `5fccf196-3ce7-4151-8ad4-5ed24b1467bc` | 460 | −287.31 |
| grid-4 | `02b659ba-a413-43c4-949f-fe6f55ba8f68` | 313 | 84.52 |

`lab screen bollinger_rsi_r1`: **fail** (exit 3), reasons `walk_forward_sharpe` and `fee_hurdle`, final grid point 3 (grid-4).

| Figure | Value | Needs |
|---|---|---|
| Walk forward Sharpe | −0.901510 | > 0 (fail) |
| Avg gross edge (USD per trade) | 1.5596 | ≥ 1.5 × cost = 1.9343 (fail) |
| Avg round trip cost (USD per trade) | 1.2896 | |
| Trades per day | 0.3063 (313 over 1,022 days) | ≥ 0.17, the candle gate floor (pass) |

Maker exits cut the cost per trade only a little (1.3944 in v4 to 1.2896; the stops and time stops still pay taker), and the gross edge fell further (2.3601 to 1.5596). v4's walk forward Sharpe of 0.068 turns negative. No holdout runs (AC-20 needs a passed screen). The `bollinger_rsi` family is **closed** (AC-21).

### `book_imbalance` pre check (AC-23, task 19, 2026-10-23): not run, fee hurdle

`lab replay lab-reports/scratch/book_imbalance_precheck.toml` (`imb_min = "0.3"`, `target_bps = "6"`, `persist_bars` 2, `levels` 5, `stop_bps` 9, maker in and out, `time_stop_min` 5, `max_spread_bps` 6.27) on `kfs/lab:s0017` (commit 56ee678), one day per run, via `lab-reports/scratch/task19_precheck.sh`. Started 07:51 UTC (09:51 Amsterdam), all four exit 0 by 09:47 UTC (11:47 Amsterdam). The report JSON has no AC-5 `enter` table, so approved entries are counted from each run's decision rows, as AC-23 allows.

| Day | Run | Approved enters | Trades | Net P&L (USD) | Fees (USD) |
|---|---|---|---|---|---|
| 2022-10-01 | `0b936cd7-42e4-4b2b-b264-3a3e5f232780` | 126 | 21 | −28.61 | 25.20 |
| 2023-10-01 | `e70e87ff-6a7d-4ff4-83dc-680ffe52f4b1` | 88 (4 more rejected) | 16 | −14.26 | 17.39 |
| 2024-10-01 | `d52f5fb0-81a6-4052-8a30-e96dfb89319e` | 16 | 12 | −41.65 | 12.60 |
| 2025-10-01 | `5e495fd7-f25f-4f0f-b8bd-134a086cd356` | 20 | 10 | −38.89 | 12.45 |
| Total | | 250 | 59 | −123.40 | 67.64 |

| Figure | Value | Needs |
|---|---|---|
| Approved enters a day (average) | 62.5 | ≥ 17 (pass) |
| Gross P&L per trade (USD), (Σ net + Σ fees) ÷ Σ trades | −0.9451 | ≥ 1.5 × cost = 1.7196 (fail) |
| Cost per trade (USD), Σ fees ÷ Σ trades | 1.1464 | |

The gross is negative: before any fee, the trades lost money on average, so no fee saving could rescue the rule. Every day lost after costs. The 2025-09-01 proof day (gross 0.23 against cost 1.04) pointed the same way. `book_imbalance` is recorded as **not run: fee hurdle**, and its replay study (task 20) does not run (AC-23, AC-24).

With this, every spec 0017 candidate has a recorded result (AC-24): `bollinger_rsi` (v4, ADX v2, r1), `vwap_retest` (both screens), and `delta_divergence` all failed or were not run.

## Test notes

- `apps/lab/tests/test_candle_failure.py::test_sigterm_stops_every_unit_writes_interrupted_reports_and_exits_130` fails (exit 1, not 130) when the machine is loaded, on `main` as well as this branch. It sends SIGTERM on a fixed 2 second timer, which races the backtest's setup. This branch doesn't cause it; a fix belongs in its own change.
