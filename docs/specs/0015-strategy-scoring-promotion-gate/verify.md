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
- [x] A replay study with `--pair-entry` (or two studies, one per `entry_mode`) → two versions with different `frozen_hash` values, each config holding its effective `entry_mode`; a `random_taker` or parity version keeps its old hash → AC-25 _(proved 2026-10-21 on the laptop with `lab replay runs/maker_probe.toml --pair-entry`: run 599ffeb9 points to a version with `entry_mode` maker and hash e9355ce9…, run 5b004c71 to one with `entry_mode` taker and hash 1ce70f0d…; `random_taker` registered hash d2c85bf4…, the same as before, with no `entry_mode` in its config)_

## Transport
- [x] `scripts/push-bundles.sh lab-reports/<batch>` → files in `/var/lib/kfs/bundles/`, import summary with every file loaded; run it again → 0 new rows → AC-7
- [x] `scripts/push-bundles.sh` with the `random_taker` replay study batches (bundles of 150 MB and 480 MB) → every file loaded, then `gate evaluate random_taker` on the VPS → `fail` → AC-7 _(proved 2026-10-21 on release 4b8e04d, which streams each bundle: loaded 3, already there 1, refused 0, under `mem_limit: 512m`; `gate evaluate random_taker` → `fail`, promotion 01a15e35-ad1c-7263-8ac8-07e70547773b)_
- [x] Hand edit a bundle's run to mode `paper` → that file is refused, the others load, exit 1 → AC-7

## Gate
- [x] `gate evaluate macd_baseline` → one promotion (`candidate → paper`), evidence on every study run, fold rows, reason codes with values; evaluate again → identical `metrics` → AC-8, AC-9, AC-10, AC-11
- [x] `lab gate macd_baseline --dry-run` on the laptop → the same verdict and metrics, no new rows → AC-13
- [x] A replay `random_taker` study (taker, 1 grid point, so `pbo` is not applicable) → `fail` with `fee_hurdle` and `holdout_sharpe` → AC-10 _(proved 2026-10-21 with `lab gate random_taker --dry-run`, which AC-13 makes the same verdict; the VPS import and evaluate of these bundles passed on 2026-10-21, see the AC-7 row)_
- [x] `lab study studies/random_taker.toml` → the `studies` row's `plan.days.development` lists the 43 days 2022-04-01..2025-10-01 (less any in `plan.days.dropped`); `lab holdout random_taker` on 2026-10-20 runs the 12 days 2025-11-01..2026-10-01, and its `data_end_at` falls on 2026-10-01 or 2026-10-02 at midnight → AC-24
- [x] Move one planned day's manifest out of `catalog-lab/tardis/manifests` under a new study name → `lab study` exits 2 naming that day and the `lab import-tardis` command, and writes no `studies` row and no run → AC-24
- [x] A scratch replay study with 5 grid points, or `source = "own"`, or `entry_mode` in `[strategy.config]`, or `from = 2024-02-01` → exit 64; `from = 2024-01-01` → accepted; `lab replay` on a run file with a grid → exit 64 → AC-2, AC-24
- [x] A study missing its holdout → `insufficient` with `missing_run` (check `holdout`); set one run's fees to zero in a scratch DB → `insufficient` with `missing_cost` → AC-12

## Paper and live
- [x] Laptop stack: the trader with no `candidate → paper` promotion for its `frozen_hash` → critical `paper_promotion_required`, exit 78 → AC-14
- [x] `gate promote --manual --step candidate-paper --version <id> --note test` with `-T` → refused; with a TTY and the wrong name → refused; correct → a pass row, and the trader starts → AC-15
- [x] Try a manual promotion to live → `ManualLivePromotionError`; an SQL insert as owner → CHECK violation → AC-15, AC-17
- [x] `gate evaluate --step paper-live --frozen-hash <h>` with under 30 days of paper and a paper return after costs above 0 (every other paper check passing) → `insufficient` with `paper_duration`; with under 30 days and a return ≤ 0 → `fail` with `paper_net_positive` and a `paper_duration` reason → AC-16, AC-12 _(proved 2026-10-21 on 5647e6e: on the VPS, `gate evaluate` for the placeholder in paper (0.38 days, 22 trades, return −0.005190) gave `fail` with `paper_net_positive` and `paper_duration`; the insufficient case has no real paper record yet, so `evaluate_paper` ran inside the deployed gate image on a 10 day, 120 trade record where every other check passes: profit → `insufficient` with only `paper_duration`, loss → `fail`, 31 days with profit → `pass`)_
- [x] On the VPS, `kfs-compose run --rm --no-deps -T trader python -m trader.version | kfs-compose run --rm --no-deps -T gate check-version` → a `"verdict": "pass"` line, exit 0; `tail -1 /var/log/kfs/deploys.jsonl` shows the release with `promotion check passed` → AC-26 _(proved 2026-10-21 on f48cf56: `check-version` printed `"verdict": "pass"` and exit 0; the `promotion check passed` line is in `/var/log/kfs/host.jsonl` at 14:13:14 UTC, not in `deploys.jsonl`)_
- [x] Laptop: change one setting in the placeholder's config, then pipe its `trader.version` into `gate check-version` on a fresh DB → exit 3 with `missing`; `kfs-deploy` of such a release → exit 4 before the drain, printing the `gate promote --manual` command, and the trader keeps running the old release → AC-26 _(proved 2026-10-21: a changed placeholder setting moved `frozen_hash` from b649c703… to f26f8374…, and `check-version` gave `missing` with exit 3; the exit 4 refusal before the drain is proved by the 8 promotion check cases in `host-scripts-test.sh`, which stub docker)_

## Dashboard
- [x] `/strategies` → one row per study sorted by walk forward Sharpe; win rate shown but not sortable; a study with no evaluation shows `not evaluated` → AC-18
- [x] `/strategies/<study_id>` → folds, bounds, benchmarks, trials; a random uuid → 404 → AC-19
- [x] `/strategies/log?page=0` → 422; `page=1` → newest first → AC-20
- [x] With a version in paper → days and trades against the floors, band and drift shown → AC-21 _(proved 2026-10-21 on 5647e6e: `/strategies` rendered by the deployed dashboard image from the VPS database, with only the login stubbed, gave status 200 and paper rows "0.38 of 30" days, "22 of 100" trades, mean trade vs lab bound, fill rate drift, and slippage paper vs lab)_

## Guards
- [x] As `gate_app`: UPDATE `promotions` → permission denied; INSERT `control_commands` → denied. As `lab_app`: INSERT `promotions` → denied → AC-22
- [x] `docker compose run --rm --no-deps gate python -c "import nautilus_trader"` → ModuleNotFoundError; the core boundary test covers `gate/` → AC-23
