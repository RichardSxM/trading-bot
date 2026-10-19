# Verify: strategy scoring and promotion gate · spec 0015 · created 2026-10-19
_Steps derived from spec 0015 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. Laptop steps use the `lab` profile; VPS steps run as `kfs` with `sudo -n kfs-compose`._

## Versions and studies
- [ ] Build the trader image on two commits that differ only outside the placeholder's import closure, run `python -m trader.version` in each → equal `frozen_hash`, different `git_commit`; touch one file in the closure → `frozen_hash` changes → AC-1
- [ ] `lab study studies/macd_baseline.toml` with a 2 point grid → one `studies` row, 2 `development` runs with `run_daily_equity`, `run_measures`, and `run_results.ratio_kind = sharpe_daily`, and no benchmark runs yet → AC-2, AC-3, AC-6
- [ ] Rerun the same `lab study` after it completes → every grid point skipped, no new runs → AC-3
- [ ] Edit the grid and rerun under the same study name → exit 64, nothing run → AC-2
- [ ] Kill the container during one grid point → that run is `aborted` with a `study_runs` row; the trial count includes it → AC-3, AC-11
- [ ] `lab holdout macd_baseline` → one holdout run of the best development grid point plus `buy_and_hold` and `macd_baseline` `benchmark` runs over the holdout range; run it again → exit 64 before any data is read → AC-4, AC-5
- [ ] On a study with one `aborted` development run, `lab holdout` → exit 64 listing that grid point → AC-5
- [ ] Kill the first holdout attempt on a fresh study, then rerun → allowed, and one completed holdout results → AC-5

## Transport
- [ ] `scripts/push-bundles.sh lab-reports/<batch>` → files in `/var/lib/kfs/bundles/`, import summary with every file loaded; run it again → 0 new rows → AC-7
- [ ] Hand edit a bundle's run to mode `paper` → that file is refused, the others load, exit 1 → AC-7

## Gate
- [ ] `gate evaluate macd_baseline` → one promotion (`candidate → paper`), evidence on every study run, fold rows, reason codes with values; evaluate again → identical `metrics` → AC-8, AC-9, AC-10, AC-11
- [ ] `lab gate macd_baseline --dry-run` on the laptop → the same verdict and metrics, no new rows → AC-13
- [ ] A replay `random_taker` study (taker) → `fail` with `fee_hurdle` and `holdout_sharpe` → AC-10 _(waits on build plan task 12, replay studies)_
- [ ] A study missing its holdout → `insufficient` with `missing_run` (check `holdout`); set one run's fees to zero in a scratch DB → `insufficient` with `missing_cost` → AC-12

## Paper and live
- [ ] Laptop stack: the trader with no `candidate → paper` promotion for its `frozen_hash` → critical `paper_promotion_required`, exit 78 → AC-14 _(waits on build plan task 15, the paper guard; run task 14 on the VPS first)_
- [ ] `gate promote --manual --step candidate-paper --version <id> --note test` with `-T` → refused; with a TTY and the wrong name → refused; correct → a pass row, and the trader starts → AC-15
- [ ] Try a manual promotion to live → `ManualLivePromotionError`; an SQL insert as owner → CHECK violation → AC-15, AC-17
- [ ] `gate evaluate --step paper-live --frozen-hash <h>` with under 30 days of paper → `insufficient` with `paper_duration` → AC-16

## Dashboard
- [ ] `/strategies` → one row per study sorted by walk forward Sharpe; win rate shown but not sortable; a study with no evaluation shows `not evaluated` → AC-18
- [ ] `/strategies/<study_id>` → folds, bounds, benchmarks, trials; a random uuid → 404 → AC-19
- [ ] `/strategies/log?page=0` → 422; `page=1` → newest first → AC-20
- [ ] With a version in paper → days and trades against the floors, band and drift shown → AC-21

## Guards
- [ ] As `gate_app`: UPDATE `promotions` → permission denied; INSERT `control_commands` → denied. As `lab_app`: INSERT `promotions` → denied → AC-22
- [ ] `docker compose run --rm --no-deps gate python -c "import nautilus_trader"` → ModuleNotFoundError; the core boundary test covers `gate/` → AC-23
