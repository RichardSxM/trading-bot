# Verify: strategy scoring and promotion gate · spec 0015 · created 2026-10-19
_Steps derived from spec 0015 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. Laptop steps use the `lab` profile; VPS steps run as `kfs` with `sudo -n kfs-compose`._

## Versions and studies
- [x] Build the trader image on two commits that differ only outside the placeholder's import closure, run `python -m trader.version` in each → equal `frozen_hash`, different `git_commit`; touch one file in the closure → `frozen_hash` changes → AC-1
- [x] `lab study studies/macd_baseline.toml` with a 2 point grid → one `studies` row, 2 `development` runs with `run_daily_equity`, `run_measures`, and `run_results.ratio_kind = sharpe_daily`, and no benchmark runs yet → AC-2, AC-3, AC-6
- [x] Rerun the same `lab study` after it completes → every grid point skipped, no new runs → AC-3
- [x] Edit the grid and rerun under the same study name → exit 64, nothing run → AC-2
- [x] Kill the container during one grid point → that run is `aborted` with a `study_runs` row; the trial count includes it → AC-3, AC-11
- [x] `lab holdout macd_baseline` → one holdout run of the best development grid point plus a `buy_and_hold` `benchmark` run over the holdout range, and no `macd_baseline` benchmark (the study is of `macd_baseline` itself); run it again → exit 64 before any data is read → AC-4, AC-5
- [x] Stop `lab holdout` on a fresh study after its holdout run completes but during a benchmark, then rerun → no new holdout run, only the missing benchmarks run, over the holdout run's `data_end_at`; run it once more → exit 64 → AC-4, AC-5
- [x] On a study whose holdout landed without its benchmarks, make the resumed benchmark fail (for example, move the catalog away) → exit 1, no new holdout run, and the next good rerun still runs only the missing benchmark → AC-5
- [x] `gate evaluate macd_baseline` → no `beats_macd_baseline` reason, metric `beats_macd_baseline_applicable` = `false`; `/strategies/<study_id>` for a 2 point study shows "2 trials", and the promotion's N counts the distinct non benchmark `frozen_hash` values across every study of the strategy (2 on a clean DB), with benchmarks left out → AC-10, AC-11, AC-19
- [x] On a study with one `aborted` development run, `lab holdout` → exit 64 listing that grid point → AC-5
- [x] Kill the first holdout attempt on a fresh study, then rerun → allowed, and one completed holdout results → AC-5

## Transport
- [x] `scripts/push-bundles.sh lab-reports/<batch>` → files in `/var/lib/kfs/bundles/`, import summary with every file loaded; run it again → 0 new rows → AC-7
- [ ] `scripts/push-bundles.sh` with the `random_taker` replay study batches (bundles of 150 MB and 480 MB) → every file loaded, then `gate evaluate random_taker` on the VPS → `fail` → AC-7 _(open 2026-10-21: `gate import` is killed for memory, exit 137, under the gate's `mem_limit: 512m`, because it parses each whole file at once)_
- [x] Hand edit a bundle's run to mode `paper` → that file is refused, the others load, exit 1 → AC-7

## Gate
- [x] `gate evaluate macd_baseline` → one promotion (`candidate → paper`), evidence on every study run, fold rows, reason codes with values; evaluate again → identical `metrics` → AC-8, AC-9, AC-10, AC-11
- [x] `lab gate macd_baseline --dry-run` on the laptop → the same verdict and metrics, no new rows → AC-13
- [x] A replay `random_taker` study (taker, 1 grid point, so `pbo` is not applicable) → `fail` with `fee_hurdle` and `holdout_sharpe` → AC-10 _(proved 2026-10-21 with `lab gate random_taker --dry-run`, which AC-13 makes the same verdict; the VPS import of these bundles is the open row below)_
- [x] `lab study studies/random_taker.toml` → the `studies` row's `plan.days.development` lists the 43 days 2022-04-01..2025-10-01 (less any in `plan.days.dropped`); `lab holdout random_taker` on 2026-10-20 runs the 12 days 2025-11-01..2026-10-01, and its `data_end_at` falls on 2026-10-01 or 2026-10-02 at midnight → AC-24
- [x] Move one planned day's manifest out of `catalog-lab/tardis/manifests` under a new study name → `lab study` exits 2 naming that day and the `lab import-tardis` command, and writes no `studies` row and no run → AC-24
- [x] A scratch replay study with 5 grid points, or `source = "own"`, or `entry_mode` in `[strategy.config]`, or `from = 2024-02-01` → exit 64; `from = 2024-01-01` → accepted; `lab replay` on a run file with a grid → exit 64 → AC-2, AC-24
- [x] A study missing its holdout → `insufficient` with `missing_run` (check `holdout`); set one run's fees to zero in a scratch DB → `insufficient` with `missing_cost` → AC-12

## Paper and live
- [x] Laptop stack: the trader with no `candidate → paper` promotion for its `frozen_hash` → critical `paper_promotion_required`, exit 78 → AC-14
- [x] `gate promote --manual --step candidate-paper --version <id> --note test` with `-T` → refused; with a TTY and the wrong name → refused; correct → a pass row, and the trader starts → AC-15
- [x] Try a manual promotion to live → `ManualLivePromotionError`; an SQL insert as owner → CHECK violation → AC-15, AC-17
- [ ] `gate evaluate --step paper-live --frozen-hash <h>` with under 30 days of paper → `insufficient` with `paper_duration` → AC-16

## Dashboard
- [x] `/strategies` → one row per study sorted by walk forward Sharpe; win rate shown but not sortable; a study with no evaluation shows `not evaluated` → AC-18
- [x] `/strategies/<study_id>` → folds, bounds, benchmarks, trials; a random uuid → 404 → AC-19
- [x] `/strategies/log?page=0` → 422; `page=1` → newest first → AC-20
- [ ] With a version in paper → days and trades against the floors, band and drift shown → AC-21

## Guards
- [x] As `gate_app`: UPDATE `promotions` → permission denied; INSERT `control_commands` → denied. As `lab_app`: INSERT `promotions` → denied → AC-22
- [x] `docker compose run --rm --no-deps gate python -c "import nautilus_trader"` → ModuleNotFoundError; the core boundary test covers `gate/` → AC-23
