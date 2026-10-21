# 0015. Strategy scoring and promotion gate: rationale

## Context

> ⚠️ Premise note: the gate decides who reaches paper, but the paper trader runs one hard wired strategy (the deferred multi strategy item). So `paper` can hold only one version at a time. That's acceptable for the first candidates. Still, this spec doesn't make paper a pipeline. It records promotions and guards the trader start, and feature 19 or the deferred item has to add parallel paper runs.

Feature 15 sits between the labs (specs 0008 and 0014) and real money (feature 19). Both labs already write typed results for every variant and grid point: `run_results`, fills, positions, and funding. They write them into the **laptop** Postgres only, with `risk_adjusted_ratio` left NULL for this feature. The VPS Postgres holds the paper runs, the `promotions` table, and the live check in `start_run` (spec 0002). Today that check matches a version id, and the version id is the hash of `{git_commit, config}`.

The forces that shaped the design:
- **Thin data, many ideas.** About 55 free Tardis days (one a month) for scalping, and about 4.5 years of 1 minute candles for hour level work. Many candidates (features 16 and 17), each with small grids (the lab caps a grid at 16 points). Selection bias and multiple testing are the main ways to fool yourself here.
- **Costs dominate short horizons.** The research seeds (fee hurdle, buy and hold benchmark) say most short horizon rules lose after fees or don't beat holding.
- **Discipline must be mechanical.** One trader, so no reviewer. The seeds ask for thresholds before results, every attempt counted, and a frozen final holdout. These have to be enforced, not just intended.
- **Deploys are frequent.** Any image change today changes `git_commit`, so it changes the version, so it would reset a 30 day paper record.
- **Project rules.** Pure core logic, no floats for money, fail closed near orders, the dashboard read only and free of Nautilus, secrets only as files, the laptop for compute, the VPS for state.

## Options considered

### Option 1: Score single runs on the laptop, gate on fixed numbers

Fill `risk_adjusted_ratio` on each lab run. Write a `lab gate` that compares a chosen run's numbers (and one out of sample run) with fixed thresholds, and records the promotion in the laptop Postgres.

**Pros**: smallest build, with no transport, no new app, and no new role.
**Cons**: the trader and dashboard on the VPS can't see the promotions. Nothing stops you picking the best of many runs or rerunning the out of sample range until it passes. There's no overfitting measure.

### Option 2: Studies on the laptop, one pure gate on the VPS (chosen)

A study groups every grid point's development run, the benchmarks, and one holdout. Folds are cut from the development runs by date. Bundles are imported into the VPS. A pure stdlib gate computes the walk forward results, bootstrap, PBO, and deflated Sharpe, and writes an append only promotion log that the trader's guards and the dashboard read.

**Pros**: one source of truth where the trader runs. Thresholds and the holdout are enforced by data and constraints. Every trial is counted. The verdict is deterministic and reproducible.
**Cons**: the most moving parts (a new app, role, migration, and push step), and hand written statistics to get right.

### Option 3: Lab writes straight to the VPS Postgres

Like Option 2, but the lab connects to the VPS Postgres over the tailnet, so there's no bundle push.

**Pros**: no copy step, and results appear on the dashboard at once.
**Cons**: it exposes Postgres on the tailnet, ties every lab run to the VPS being up, and puts the lab's long write transactions on the box that trades.

## Rationale

Option 2 is the only one where the promotion is where the trader reads it (the VPS) and where the anti self deception rules are mechanical. Thresholds are frozen per study, there is one completed holdout per `frozen_hash`, the trial count comes from rows, and there's no manual live promotion, enforced both by a CHECK and in code. Option 1 would need all of that added later anyway. Option 3 trades a small copy step for network exposure and coupling, which is a bad trade for a box holding positions.

The sub decisions, each with its runner up:
- **Daily Sharpe** as the ratio (√365, because crypto trades every day). It works for both labs and is what the deflated Sharpe test builds on. Runner up: Sortino, which is kinder to big up days but has weaker significance maths.
- **Rank on walk forward out of sample Sharpe**, never on in sample numbers or win rate. Runner up: holdout Sharpe, which is available only once per version, so it's too late to guide work.
- **Slice development runs into folds** instead of separate runs per range. It costs about 10× less on the slow replay engine, and the matrix PBO needs is the same data. Replay days are closed segments, so slicing is exact there. Runner up: separate cold start runs, which are fully independent but cost far too much compute.
- **Three verdicts with fail first precedence.** Evidence against a strategy outranks missing evidence, and missing data never passes. Runner up: two verdicts, which blur "bad" and "not enough data".
- **Freeze by `frozen_hash`** (the import closure of the code, plus the config). Unrelated deploys keep the evidence, while a logic change anywhere in the closure, shared indicators included, makes a new version. Runner up: a declared file list, which is easy to get wrong silently.
- **Beat buy and hold on Sharpe, not raw return.** Market neutral scalpers can't beat a bull year on return, and the risk adjusted view is the fair one. Runner up: return, which would fail nearly everything for the wrong reason.
- **Pure stdlib statistics, with floats only for statistics.** The sizes are small (at most 16 grid points, at most about 1,600 days, 10,000 resamples), it keeps `trading_core.gate` pure and the gate image small, and money stays Decimal. Runner up: numpy in `gate/`, which is faster but a heavy dependency the size doesn't need.
- **Holdout spent only by a completed run.** A crash reveals no result, so it can't be peeking. Runner up: any attempt spends it, which punishes infrastructure faults.
- **The paper guard has no exemptions.** The placeholder gets a logged manual promotion before the guard ships, so the code that touches order placing has no special path.
- **Benchmarks are real candle runs** (`buy_and_hold`, `macd_baseline`) tagged `benchmark`. Rather than adding a special `run_results` sample, they reuse the lab and get the same costs.

### Amendments after the first verify (2026-10-19)

The first `/check verify` found three gaps, each decided with you on 2026-10-19:

- **A stopped benchmark could never be rerun.** `lab holdout` stopped after the holdout run completed but during `buy_and_hold` left the study stuck on `missing_benchmark`, because the rerun hit the "holdout spent" exit first and no other command runs benchmarks. Chosen: `lab holdout` itself resumes, running only missing benchmarks when this study already holds the completed holdout. Benchmarks don't depend on the studied strategy, so this is not peeking. Runner up: a separate `lab benchmarks <study>` command, clearer by name but one more command for a one line change.
- **Benchmarks counted as trials.** N and the dashboard trial count took every `study_runs` row, so a 2 point study showed 3 trials and the deflated Sharpe got stricter for no reason. Chosen: count only `development` and `holdout` rows. Runner up: keep counting and document it, which would penalise every study by one or two phantom trials.
- **A baseline was judged against itself.** In a study of `macd_baseline`, its own benchmark equals the default grid point, so `beats_macd_baseline` could never pass. Chosen: skip the self benchmark run and mark that check not applicable. Runner up: baselines stay reference only and never promote, which is stricter but would make the happy path study in the test scenarios unable to pass by design.

### Replay studies (2026-10-20 amendment, task 12)

Building task 12 hit three gaps. AC-2 said a study is its run file verbatim, but replay run files have no grid. A replay run reads one `source`, but the plan picked days from "Tardis and recorder days". And the laptop held no replay day before `holdout_start`. Free Tardis data is only the 1st of each month (PF_XBTUSD since 2022-03-22), so there are at most 43 development days and 12 holdout days so far. A replay costs about 28 minutes per day per grid point.

- **Days: every 1st of the month from 2022-04-01 (chosen).** That is 43 days, exactly 15 + 4 × 7, so the default folds give 4 walk forward steps with nothing dropped, and they span the 2022 bear market and the 2024 bull market. Runner up: the last 29 days (from 2023-06-01, 2 folds, about 7 h per grid point). It is cheaper, but 2 folds can't tell a stable strategy from a lucky one. The last 22 days give only 1 fold, which makes walk forward meaningless. A study may still start later through its own `from`, down to one fold (22 days).
- **One source, Tardis only (chosen).** Development can only be Tardis, because the recorder started in October 2026. Adding recorder days to the holdout alone would judge the final version on a different feed (our own capture, with liquidations) from the one it was chosen on, so a gap between lab and holdout could come from the feed rather than the strategy. Runner up: a holdout of Tardis days plus recorder days, which gives far more holdout days but breaks like for like. Recorder days stay for paper parity, where they matter most.
- **Grid capped at 4 for replay (chosen).** It is the same `[strategy.grid]` as candle, so AC-2's "verbatim" holds, but at 10 h per grid point, 16 points would take a week of laptop time. Fewer points also keep the deflated Sharpe trial count low. Runner up: no replay grid at all (one config per study). That is simpler, but every variation becomes a new study and still counts as a trial.
- **Missing days are refused, never skipped (chosen).** The day set is a rule fixed before any result, so what happens to be downloaded can't shape a study. Skipping would quietly let it.
- **Holdout ends at the latest 1st of the month before today (chosen),** not at the newest day in the catalog, for the same reason: forgetting an import should stop the run, not shorten the holdout.
- **A day with no clean hour is dropped by rule, at plan time (chosen after the cross check).** Its gap file is known before any run, so dropping it can't be steered by results. Refusing would block a study forever over one dead Tardis day. Letting it vanish silently mid run would quietly lose a fold. The dropped days are recorded in the plan.
- **Base variant only.** Latency sweeps, queue bounds, and paired entries stay as `lab replay` probes. In a study they would multiply runtime for numbers the gate never reads.

### Threshold provenance (v1, fixed 2026-10-19 before any candidate result)

| Threshold | Value | Why |
|---|---|---|
| Fee hurdle k | 1.5 | Net edge of at least half the cost again leaves room for live slippage the sims miss |
| Max drawdown | 10% at 0.5% risk per trade | Scaled to the proposed live 0.25% risk, about 5%, so ordinary noise rarely trips the live 3% stop |
| PBO max | 0.30 | Stricter than the coin flip 0.5, reachable with small grids |
| Deflated Sharpe min | 0.95 | The usual 95% confidence, after the multiple testing discount |
| Trade floor | 200 replay, 60 candle | Scalpers reach 200 cheaply; hour level makes about 50 to 150 a year |
| Concentration | 60% | Allows one sided strength, catches one month or a few lucky trades |
| Paper | 30 days, 100 or 20 trades, fill rate ±25%, slippage +2 bps | A few regimes and a maintenance window; drift bounds loose enough for a 30 day sample |
| Holdout | 12 months from 2025-10-19; replay: the Tardis 1st of the month days only (amended 2026-10-20, recorder days dropped) | One full year of regimes and funding; one feed for development and holdout (see the 2026-10-20 amendment) |
| CSCV blocks | 10 | 252 splits; replay blocks are still about 4 days |
| Bootstrap | Politis and White mean block (at least 5 days), 10,000 resamples, seeded | Trades and days cluster, so treating them as independent overstates confidence |
