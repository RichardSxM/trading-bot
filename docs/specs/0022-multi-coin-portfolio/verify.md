# Verify: multi coin portfolio · spec 0022 · updated 2026-10-29

## AC-1 hash baseline (2026-10-28)

`code_hash(code_closure(m, root))` for every `trading_core.node` module (32) on origin/main bed1415, the commit `feat/multi-coin-portfolio` starts from, run through `scripts/spikes/hash_check.py` in `kfs/lab:dev` against a `git archive` of main's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `9c8c2a41d51b414ce8f21c140972c3ae3abc217a7fb2f8df88a0b724becd76b3`. The close out (AC-18) runs the same list on the branch: it must match line for line, with no module added or edited.

## AC-2 and AC-2a ETH data (2026-10-28)

`lab fetch-candles 2022-03 2026-10 --instrument PF_ETHUSD.KRAKEN` on the laptop (`--no-deps`, the AC-2a build of `feat/multi-coin-portfolio`) exited 0: 39 months fetched (2023-08 to 2026-10), 17 skipped as already complete (2022-03 to 2023-07, stored before AC-2a), 2026-10 stored as incomplete up to 2026-10-28 03:31 UTC (04:31 Amsterdam). Funding came from the import of 2026-10-28 (27,827 export rows) plus the API for the newest charges.

- **First candle minute**: 2022-03-23 10:05 UTC (BTC: 10:04). The one gap row is the time before listing, 2022-03-01 00:00 to 10:04 (32,284 minutes).
- **First whole month**: 2022-04, the same as BTC, so the first month both coins share is **2022-04** (AC-15's `from = 2022-04-01` stands).
- **Per year** (missing minutes / gap rows / missing funding charges): 2022: 32,284 / 1 / 0 (all before listing); 2023: 0 / 0 / 0; 2024: 0 / 0 / 0; 2025: 0 / 0 / 7; 2026 to date: 0 / 0 / 3. The missing charges fall in the same months and counts as BTC's (2025-07 1, 2025-08 1, 2025-11 4, 2025-12 1, 2026-02 2, 2026-05 1), so they are gaps in Kraken's funding history, not in the import.
- **`margin_maint`**: the pin's `0.005` matches Kraken's published first tier `maintenanceMargin` 0.005 (initial 0.01, tier up to 500,000 USD) from `/derivatives/api/v3/instruments` on 2026-10-28; `tickSize` 0.1 matches the pin's tick.

**Off tick roundings (AC-2a)**: the only off tick opens and closes are the two cases the spec names, stored as it says:

- 2023-08-17 22:55 close `1660.691517005051` stored 1660.6 (clamped to the stored high), and 22:56 open of the same raw value stored 1660.7.
- 2023-10-16 13:30 close `1617.853075194912` stored 1617.9, and 13:31 open (also its low) stored 1617.9.

So the most in one month is 2, far under `OFF_TICK_BODY_MAX` (10). The other real roundings are highs and lows, as spec 0014 AC-1 already handles: 2023-08 5, 2023-09 3, 2023-10 8, 2023-11 2. `price_rounded_rows` per month: 2023-08 6, 2023-09 3, 2023-10 9, 2023-11 2. Later months (2024-03 26, 2024-04 1, 2024-05 22, 2024-06 3, 2024-11 2, 2024-12 5, 2025-10 6, 2026-01 2, 2026-06 3) are noise snaps only (within `1e-8`), with no rounding listed.

**BTC unchanged**: no file under the BTC tree (`candles/data`, `candles/gaps`, `candles/manifests` at the root) was written on 2026-10-28. The new rule only changes a candle with an open or close off the tick, which before AC-2a was a bad row that stopped the month. Every stored BTC month built with none, so their rows stay byte identical, and none is fetched again.

## AC-3 to AC-8 thin thread (2026-10-28)

Built in fa16cf0. Tests on fixture candles in `apps/lab/tests/test_multi_coin.py` cover each part: run file refusals and sorted coins (AC-3), the merged stream tie order and funding at its charge time (AC-4, AC-4b), a gap on one coin while the other trades, plus the shared exposure cap (AC-5, AC-6), ruin on both coins closing both positions at the crash time (AC-7), and per coin funding rates with byte identical repeat runs (AC-8). Full suites on that build: core 1662 passed, lab 303 passed.

**Single coin unchanged**: snapshots of the reports and bundles for the `macd` 4 variant run, `buy_and_hold`, the crash unit, and every committed study's `plan_hash`, configs, and frozen hashes match the base image `kfs/lab:base0022` with 0 differences.

Three notes on the design as built:

- The ruin exit reason stays `ruined` (the existing `RUINED` constant), so single coin runs keep their bytes. The spec text says `ruin`; the meaning is the same.
- A stop sized entry (`donchian_4h`, `macd`) is shrunk to fit the shared cap. `buy_and_hold` uses a fixed quantity, so the cap refuses it with `exposure_cap` instead of shrinking it, which is what the fixture test asserts.
- The combined equity curve, exposure as the union of open time, and the mean buy and hold benchmark came forward from AC-9, because the bundle needs them already.

**Smoke run** (a benchmark, not a trial): `lab-reports/scratch/smoke_pair_buy_and_hold.toml`, `runs/buy_and_hold.toml` on BTC plus ETH, 2026-09-01 to 2026-09-30, 5000 USD notional each, in one 10,000 USD account. Batch `01a182c7-a3db-7250-a6be-a98709a52f28`, exit 0, at 13:51 to 13:55 UTC (14:51 to 14:55 Amsterdam).

- 688,265 events (ETH 344,295, BTC 343,952) in 197 s, peak memory about 596 MB.
- 2 positions, 4 fills, one per coin each, both opened 2026-09-01 01:00 and closed 2026-10-01 00:00 UTC.
- Realized P&L: ETH +423.06, BTC +306.00. Funding: ETH −35.55, BTC −24.76 (1,436 hourly rows, 718 per coin). Net +668.75 USD, which matches the sum.
- Exposure 0.9986, marked max drawdown 7.15%, benchmark (mean of both coins) 7.65%.
- The run config lists `instrument_ids`, a `bar_type` per coin, and `candle_gaps` per coin. The bundle still carries a single `instrument` key (null here); task 5 adds `instruments`.

## AC-9 to AC-14 report, bundles, gate, benchmarks (2026-10-28)

Built in 6fc8fe1 (AC-9, AC-10), cf51e1e (AC-12), fc49bb9 (AC-13, AC-14), and 305eb35 (AC-11). The files study.py and test_multi_coin.py carry hunks of several tasks, so they landed whole in the last commit. Full suites on that build: hooks and mypy clean, core 1672 passed (5 skipped), integration 151 passed, dashboard 551 passed, lab 49 passed on the chain plus 31 passed for `test_multi_coin.py` and `test_study.py` after the task 7 split.

- **AC-9, AC-10**: the report's `per_instrument` block and the mean buy and hold figure; the single coin commands refuse a run file with several coins with `MULTI_REFUSED`.
- **AC-12**: a bundle carries `instruments`, and `gate import` upserts each coin before its rows. The integration tests cover the upsert (an equal row is a no op) and the refusal when a stored coin has other specs. **One deviation**: the plan said to import the step 3 smoke bundle on a laptop gate. That bundle has no study version, so `gate import` refuses it by design (`MissingParentError`). The proof is the integration tests here, and the real study bundle in task 9.
- **AC-13, AC-14**: thresholds v2 adds `instrument_max_share = 0.60`, and v1 files still load with the same verdicts (the v1 fixture parity test). `concentration_instrument` is skipped with fewer than 2 coins or a total net at or below 0. The dashboard shows "Coin share of profit".
- **AC-11**: on several coins, a benchmark file's `notional_usd` is split evenly, so `buy_and_hold` on BTC plus ETH opens each coin at 5000 USD of the file's 10,000 USD and never trades again. A single coin benchmark keeps the file's notional.

## AC-15 and AC-16 study, holdout, and verdict (2026-10-28 to 2026-10-29)

- **AC-15**: `apps/lab/studies/donchian_4h_pair.toml` committed in 7b9bffa before its first run, and `kfs/lab:dev` rebuilt with `GIT_COMMIT` there. Shared first whole month 2022-04-01.
- **Laptop runs** (`LAB_WORKERS=2`, through `lab study` and `lab holdout` only): study 2026-10-28 16:47 to 19:16 UTC (8913 s), batch `01a1836a-2351-750f-8132-3deb98238d18`; holdout 19:16 to 20:30 UTC (4454 s), batches `01a183f1-5637-73e4-8fa5-2d783971a080` (grid-2), `01a18407-3d20-708e-a685-b78aaa76109a` (buy and hold), `01a18428-392c-74dc-9ab0-f9281bb0be34` (MACD).
- **Study, development period** (net USD, trades): grid-1 +3520.47 (432), grid-2 +2728.67 (406), grid-3 +1353.90 (243), grid-4 +396.94 (230). Buy and hold +76.85%. No liquidations.
- **Deploy**: PR #106 merged f38e872 and deployed 2026-10-29 01:38 UTC with thresholds v2. The deploy's promotion check exited 4, because the `rows.py` change in cf51e1e moved the `placeholder` frozen hash (see AC-18 below). Fixed by a manual promote of the new version (`01a1854a`). Moving `RISK_TOTAL_LOSS_SINCE` to 2026-10-29 in `/etc/kfs/kfs.env` (the demo trader had hit its total loss limit) made another version, promoted by hand as `01a18558`; the trader resumed at 01:51 UTC.
- **Push**: `scripts/push-bundles.sh` on the four batches, 01:40 to 03:14 UTC: loaded 7, already there 49, refused 0.
- **AC-16**: `gate evaluate donchian_4h_pair` run once, 07:56 UTC: `fail`, promotion `01a186a9-711e-76bb-b8d3-75e0b4f7bd5f`, N 31. Full record in the spec's Results.

## AC-18 close out (2026-10-29)

`scripts/spikes/hash_check.py` in `kfs/lab:dev` against a `git archive` of main f38e872 (the merged feature): 32 modules, list sha256 `b4045424f5875ddbee01211728f7218e8efcc093ca2738d1d18f42824b69465a`. Rerun on bed1415 the same way gives the baseline `9c8c2a41…` again, so the method is unchanged.

**Does not match.** No module was added, and no file under `trading_core/node/` changed (`git diff bed1415 f38e872 -- packages/core/src/trading_core/node` is empty). 22 lines differ: `bar_rule`, `bollinger_rsi`, `book_imbalance`, `buy_and_hold`, `connors_rsi`, `control`, `delta_divergence`, `donchian_4h`, `donchian_4h_r1`, `ema_pullback`, `imbalance_fade`, `loss_limits`, `macd_baseline`, `maker_probe`, `noise_breakout`, `paper_snapshots`, `placeholder`, `random_taker`, `recording`, `scalp_base`, `stale`, `vwap_retest`. These are exactly the 22 closures that contain `trading_core.recording.rows`, and the only change there is the `instruments` field on `RunBundle` (cf51e1e, AC-12). Of the other core files the feature changed, none is in any node closure. The 10 lines that match are the closures without `rows`.

What it means: every strategy version recorded before f38e872 now has a different frozen hash in the image than the one on its gate row. Old verdicts stay in the table, but a re-run of an old study would make new versions. The `placeholder` side already showed this at deploy (exit 4, fixed by hand). Spec 0022's own study is not affected: it was run at 7b9bffa, after cf51e1e.

**Amended AC-1 (2026-10-29): pass.** `/architect` amended AC-1 (rationale.md, *Hash rule and shared modules*): no node file edit, and every moved line explained by a module the spec names as changed (only `trading_core.recording.rows`, for AC-12). Rechecked on f38e872 with `code_closure` over the 9 core files changed since bed1415: `recording/rows` sits in exactly the 22 moved closures; `catalog/candles`, `gate/checks`, `gate/inputs`, `gate/thresholds`, `lab/candle_metrics`, `lab/candles`, `recording/gate_queries`, and `recording/repository` sit in none. The node diff is empty. So every moved line is explained, and AC-1 and AC-18 pass under the amended wording.

## PEPE side test (exploratory, outside this spec and the gate)

Asked for by the trader on 2026-10-28. It adds 1 trial outside the gate (no `gate evaluate`); its bundles were pushed to the VPS 2026-10-29 as a record only.

- 676e17c: pinned `PF_PEPEUSD.KRAKEN` (tick 1e-10, step 1000, margin 0.02 initial and 0.01 maintenance, listed 2023-05-25) and `studies/donchian_4h_pepe.toml` (PEPE alone, from 2023-06-01, slippage 5 bps).
- f91e400: PEPE volumes run into the billions, so their float noise (257548000.0000001) was past the absolute `VOLUME_NOISE` 1e-8 and the 2024-02 fetch stopped. The bound is now the larger of 1e-8 and 1e-12 of the volume. Under 10,000 units it is unchanged, so no BTC or ETH month changes. `trading_core.lab.candles` is in no node closure.
- Laptop runs 2026-10-28 21:01 to 22:49 UTC. Study batch `01a1845e-d4ac-764f-90fb-c872bb054c3d` (net USD, trades): grid-1 +2661.97 (157), grid-2 +2063.27 (143), grid-3 +2372.36 (90), grid-4 +1739.75 (81); buy and hold +435.72%.
- Holdout batch `01a18491-2a66-72ff-b336-a83ea6540713`, grid-3: +323.53 USD (+3.24%), 32 trades, marked max drawdown 5.45%, against buy and hold −31.89% before costs (`01a1849f-593c-7355-a1d5-3403701627be`) and MACD −52.66% (`01a184af-4a06-72b2-80a3-fb1e3dd645cf`).
- Reading: profitable, but 32 holdout trades is fewer than BTC alone, so on its own it would fail the same luck checks. It is a hint for the "More instruments" follow up, not evidence.
