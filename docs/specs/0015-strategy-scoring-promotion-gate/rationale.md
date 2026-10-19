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
| Holdout | 12 months from 2025-10-19; replay: those Tardis days plus every recorder day | One full year of regimes and funding; recorder days are closest to live |
| CSCV blocks | 10 | 252 splits; replay blocks are still about 4 days |
| Bootstrap | Politis and White mean block (at least 5 days), 10,000 resamples, seeded | Trades and days cluster, so treating them as independent overstates confidence |
