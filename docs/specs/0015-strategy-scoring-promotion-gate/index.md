# 0015. Strategy scoring and promotion gate

**Date**: 2026-10-19 (amended 2026-10-19 after the first `/check verify`: benchmark resume, trial count, self benchmark; amended 2026-10-20 for task 12: replay days, one data source, replay grid; amended 2026-10-21: entry mode in the hash, deploy promotion check, off tick trades per spec 0016)
**Status**: In Progress

## Summary

Every strategy is tested as a **study**: each parameter set runs once over older data, walk forward folds (train on the past, test on the next stretch) are cut from those runs by date, and the chosen version then meets a frozen final year of data exactly once. A pure gate in `packages/core` scores it on return after costs, drawdown, and daily Sharpe (return per unit of day to day swing), with overfitting tests, and gives one of three verdicts: pass, fail, or insufficient evidence. Lab results travel from the laptop to the VPS as bundle files, the gate runs there and logs every attempt as a promotion, the trader refuses to paper trade or go live without the right pass, and the dashboard ranks studies on out of sample numbers. A version is frozen by a hash of its own code and config, so unrelated deploys don't reset its paper record.

## Requirements

**User stories**:
- As the trader, I want every strategy judged by rules written down before I see its result, so I can't fool myself with a lucky or overfitted backtest.
- As the trader, I want the bot to refuse to paper trade or go live with a version the gate has not passed, so a mistake in a deploy can't put an untested strategy on the market.
- As the trader, I want one page that ranks my strategies on unseen data and shows why each one passed or failed, so I know what to work on next.

**Acceptance criteria**:

*Versions and studies*
- **AC-1** (frozen versions): `ensure_strategy_version` also stores `code_hash` and `frozen_hash`. `code_hash` is the sha256 over the sorted `(module path, source bytes)` pairs of the strategy's `trading_core.node.<name>` module plus every `trading_core` module it imports transitively (found by parsing imports with `ast`, never importing), third party modules excluded. `frozen_hash` is the sha256 of the canonical JSON `{code_hash, config, nautilus_version}` (the installed NautilusTrader version, because an engine upgrade changes fills). Two images with different `git_commit` but the same closure files, config, and Nautilus version give the same `frozen_hash`. Changing any closure file, any config value, or the Nautilus version changes it. Every `import` and `from … import` node anywhere in a closure module counts (including ones inside functions, and relative imports resolved against the package). A closure module that calls `importlib` or `__import__` makes `code_closure` raise `DynamicImportError`, and a test enforces it. Rows that existed before the migration keep both NULL.
- **AC-2** (study file and thresholds): a study file is the existing run file of its lab, verbatim (the same strategy, `[strategy.config]`, `[strategy.grid]` with its 16 point cap and sorted key expansion, and the same fees, slippage, latency, and balance fields), plus a `[study]` table: `name`, `lab` (`candle` or `replay`), `entry_mode` (`taker` or `maker`, replay only), optional fold overrides, and `seed`. The `--from` and `--to` overrides are refused, because the plan sets the ranges. A replay run file gains the same optional `[strategy.grid]` as a candle one (same key rules and sorted expansion into `grid-1`, `grid-2`, …), capped at 4 points instead of 16. Plain `lab replay` refuses a run file with a grid (exit 64, "a grid runs only in `lab study`"). In a replay study, `source` must be `tardis`, `entry_mode` may not appear in `[strategy.config]` or `[strategy.grid]` (the `[study]` table owns it), and only the base variant runs (pessimistic queue, the run file's latency; `lab study` takes no variant flags). The study's `entry_mode` becomes the base variant's entry mode. A strategy with no `entry_mode` setting accepts only `taker`, and its variant entry mode stays unset. Each replay grid point runs as its own `replay()` call with one `base` variant whose strategy config is `{**[strategy.config], **grid_point}`, so each point gets its own strategy version and `frozen_hash`. Its `study_runs.grid_point` is its index in the sorted expansion. A replay grid of 2 or 3 points is allowed, but PBO needs at least 4, so it is recorded as not applicable (AC-11). `lab study <studyfile>` validates it and loads `trading_core/gate/thresholds.toml`. It writes one `studies` row holding the resolved plan (the parsed study file plus resolved folds and ranges), `plan_hash` (sha256 of its canonical JSON, the same function as `version_hash`), the thresholds, and `thresholds_hash`. Reusing a study name with a different `plan_hash` exits 64 before any run. Editing `thresholds.toml` later never changes an existing study's thresholds.
- **AC-3** (development runs): every grid point runs once over the development range (all data before `holdout_start`). Each completed run's bundle carries its `study_runs` row (role `development`, `grid_point`, `frozen_hash`, status), its `run_daily_equity` rows, its `run_measures` row, and a `run_results` row with `risk_adjusted_ratio` = daily Sharpe and `ratio_kind` = `sharpe_daily`. A grid point that fails or is interrupted still writes its `strategy_runs` row (status `failed` or `aborted`) and its `study_runs` row, so it counts as a trial, but it writes no `run_results`, `run_daily_equity`, or `run_measures`. Running `lab study` again with the same plan resumes: grid points that already have a `completed` development run are skipped.
- **AC-4** (benchmarks): `lab holdout` runs `buy_and_hold`, plus `macd_baseline` for an `hour_level` study, over the holdout range as role `benchmark` candle runs (the pinned candle instrument, 1 hour bars, the study's fees). No benchmark runs over the development range, because no check reads one. For a replay study, a benchmark's daily series is limited to the days the holdout run has rows for (an inner join on `day`). A benchmark whose strategy is the studied strategy itself (a study of `macd_baseline` or `buy_and_hold`) is not run, because a strategy can't be judged against itself. It is matched by strategy name on purpose, whatever the study's config. One shared rule (the benchmark names for a strategy kind, minus the studied name) feeds both `lab holdout` and the gate, so they can't drift apart. A benchmark that already has a `completed` run in the study is skipped, so a rerun only fills in the missing ones (see AC-5).
- **AC-5** (holdout once): `lab holdout <study>` takes a Postgres advisory lock on the study id. It exits 64 unless every development grid point has a `completed` run (it lists the missing ones). It then picks the final grid point (best development `run_results.risk_adjusted_ratio`, ties to the lowest grid index) and runs it once over the holdout range (role `holdout`), from `holdout_start` to the last complete UTC day in the catalog (replay: the holdout days of AC-24), stored as the run's `data_end_at`. It first looks for a `completed` `holdout` `study_runs` row in this study (by `study_id`). If there is one, it resumes: it never runs the holdout again, skips the frozen hash recheck and the catalog end check (benchmarks don't use the studied code), and runs only the benchmarks (AC-4) that have no `completed` run in the study, over `holdout_start` to the stored holdout run's `data_end_at` (never a recomputed catalog end, so the benchmarks always cover the holdout's days). If that row's `frozen_hash` differs from the final grid point's, it exits 64 with the mismatch. A resume that runs benchmarks exits 0, a failed benchmark exits 1, and with none missing it exits 64 before reading any data. Benchmarks don't depend on the studied strategy, so running a missing one later reveals nothing new about it. Only when this study has no completed holdout does the global check apply: if a `completed` holdout run already exists for that `frozen_hash` in another study, it exits 64 before reading any data, and the partial unique index backs that up. The resume runs under the same advisory lock. A holdout run that fails or is interrupted doesn't use up the holdout, and it writes no daily equity or results, so it reveals nothing.
- **AC-6** (daily series): `equity_close` is equity at each UTC day's close, `marked` for candle runs (open positions valued at the last close) and `realized` for replay runs. Candle: one row per UTC day, with a day's return being `equity_close` over the previous day's `equity_close`, minus 1, and the first day measured against the starting balance. A candle gap day carries the previous equity forward (a return of 0). Replay: one row per replayed UTC day, because each day is a closed segment that starts at `starting_balance_usd`. So `equity_close` is the starting balance plus that day's net P&L, and the day's return is net P&L over the starting balance. `trades_closed`, `fees`, and `funding` are that day's sums from the run's closed positions (by `closed_at`) and funding payments (by `paid_at`). Daily Sharpe is the per day mean over the sample std (ddof 1), and the stored and displayed figure is that times √365. For replay, the √365 figure is a ranking convention only, because the days are about a month apart. The gate converts Decimal to float only for these statistics, sums with `math.fsum` over inputs in day order, and stores results as Decimal, rounded to 6 places.
- **AC-24** (replay days): a replay study uses free Tardis days only (the 1st of each month), never our own recorder days, so development and holdout are captured the same way. Development days are every 1st of the month from the study file's `from` to the day before `holdout_start`. `from` must be a 1st of the month on or after 2022-04-01 (the first whole Tardis day of `PF_XBTUSD`), and there must be at least `first_in_sample_days + step_days` development days (22 by default), else exit 64. The study file's `to` must be before `holdout_start`, as for candle. The resolved development day list is stored in the plan (`plan.days.development`), so `plan_hash` covers it. As for candle, the development run's `to` is set to `holdout_start − 1`, while the plan's `run_file` keeps the file's own `to`. Holdout days are every 1st of the month from `holdout_start` up to and including the latest 1st of the month before the current UTC day, which the CLI passes in when `lab holdout` starts (no wall clock inside core). The holdout run is the stored run file with `from` = `holdout_start` and `to` = the last holdout day, so the Tardis day filter makes 2025-11-01 its first day. Its `data_end_at` is the replay's existing value (the end of its last clean segment), and a resume takes that value's UTC date as the benchmark end (AC-5). If any planned day (development or holdout) has no manifest in `catalog-lab/tardis`, the command exits 2 and prints every missing day plus the `lab import-tardis <from> <to>` command that fetches them. `lab study` runs this check in `load_study`, before the `studies` row is written, so a missing day leaves no row. `lab holdout` runs it right after taking the advisory lock, before any run. A missing day is never skipped. A Tardis day keeps its trade rows priced off the tick out of the catalog, by spec 0016, so those days count here. A day whose manifest exists but whose gap file covers all 24 hours has no clean segment. It is dropped when the plan is made, before any run, by that fixed rule, and listed in `plan.days.dropped` (development) or in the holdout run's `run_config.dropped_days`. The minimum day count applies after the drop. Partly gapped days stay and replay their clean hours.

*Transport*
- **AC-7** (push and import): `scripts/push-bundles.sh <batch dir>...` rsyncs `.bundle.json` files over Tailscale SSH to `/var/lib/kfs/bundles/` on the VPS, then runs `kfs-compose run --rm gate import`. `gate import` parses each file with the shared bundle model. It refuses a whole file (one transaction per file) unless every run has env `demo`, mode `backtest` or `replay`, and trader_id `BACKTESTER-LAB`, and its version carries `code_hash` and `frozen_hash`. A refused file is reported and the others still load, with exit 1 if any were refused. Importing the same files twice adds no rows and raises no error, because every row is inserted through the existing insert and compare helpers. A row whose key exists with different content raises `RecordConflictError`, which refuses that file whole. Versions keep the laptop's `git_commit` as provenance.

*Candidate to paper*
- **AC-8** (evaluation record): each `gate evaluate <study>` call takes a Postgres advisory lock on the study id, then writes exactly one `promotions` row (`from_stage` `candidate`, `to_stage` `paper`, `decided_by` `gate`, the final version's `strategy_version_id` and `frozen_hash`, `study_id`, the study's frozen `thresholds` and `thresholds_hash`, `reasons`, `metrics`, `gate_commit`). It also writes `promotion_evidence` for every development, holdout, and benchmark run of the study, and one `promotion_folds` row per fold. The same database contents and seed always give the same verdict and the same `metrics`.
- **AC-9** (folds): the development range is cut into anchored, expanding folds from the plan. Candle folds use calendar months counted from the first development day (defaults: 12 months first in sample, then 6 month out of sample steps). Replay folds count positions in the sorted list of distinct development days that every completed grid point has a row for (defaults: the first 15 days in sample, then 7 days per step). A trailing step shorter than a full step is dropped. A fold's in sample range runs from the start to its cut, and its out of sample range is the next step. `promotion_folds` stores the first and last real day of each range. Per fold, the grid point with the best in sample daily Sharpe (from the sliced `run_daily_equity`) is chosen and scored on the out of sample slice. The walk forward series is all folds' out of sample slices joined in date order. Its Sharpe is the ranking key.
- **AC-10** (checks): the gate runs every check in the table under *Gate checks* with the study's frozen thresholds. A check whose inputs are missing records an `insufficient` reason instead of running.
- **AC-11** (statistics): all of these use per day (not annualised) Sharpe. PBO is computed by CSCV with S = 10 blocks over the development daily return matrix of every completed grid point, restricted to the days they all share. Each half is ranked by Sharpe (ties to the lowest grid index), and PBO is the share of splits where the logit of the in sample winner's out of sample relative rank is ≤ 0. With fewer than 4 completed grid points, PBO is recorded as not applicable. The deflated Sharpe probability (Bailey and López de Prado) uses: the final version's development Sharpe; T = its number of development days; skew and raw (not excess) kurtosis of its daily returns; N = distinct `frozen_hash` values in any `development` or `holdout` `study_runs` row of this strategy's studies, failed runs included (`benchmark` rows never count, because a benchmark is a yardstick, not an attempt at this strategy); and the variance of the per day Sharpes of the completed trials (failed trials count toward N only). The bootstrap is a stationary block bootstrap. Candle studies use a Politis and White mean block length (at least 5 days). Replay studies use a block length of 1, because their days are about a month apart and don't depend on each other. Both use 10,000 resamples and `random.Random(seed)` with the study's seed. The gate stores the one sided 95% lower bound of mean holdout daily return, and the same lower bound for mean holdout trade return (net P&L over equity before the trade, resampled over the trade sequence). *Amended by spec 0018 AC-19 (2026-10-21): N and the trial Sharpe variance span every study of the same strategy kind and lab, not only this strategy's studies.*
- **AC-12** (verdict): the verdict is `fail` if any check fails. Otherwise it is `insufficient` if any reason is `insufficient`. Otherwise it is `pass`. Reasons are stored as `[{code, check, value, threshold}]`. A missing metric, a missing cost (`fees_total` = 0 with trades, no funding rows on a run that held a position over a charge time, no slippage setting (candle) or latency setting (replay) in `run_config`), a missing development, holdout, or benchmark run, or a NULL `code_hash` each produce an `insufficient` reason and never a pass.
- **AC-13** (preview): `lab gate <study> --dry-run` on the laptop runs the same pure gate against the laptop Postgres and prints the verdict, reasons, and metrics. It writes nothing.

*Paper and live*
- **AC-14** (paper guard): `start_run` with `mode = 'paper'` raises `PromotionRequiredError` when the version's `frozen_hash` is NULL, or when the latest (by `decided_at`, then `id`) `candidate → paper` promotion with that `frozen_hash` isn't `pass`. The trader then logs `paper_promotion_required` at critical and exits 78. A later non pass promotion takes the version back out of paper at its next start.
- **AC-15** (manual promotion): `gate promote --manual --step candidate-paper --version <strategy_version_id> --note <text>` needs a TTY and the strategy name typed back at the prompt. It writes `decided_by` `manual`, verdict `pass`, and no study. `record_promotion` refuses (`ManualLivePromotionError`) any manual promotion with `to_stage` `live`.
- **AC-16** (paper to live evaluation): `gate evaluate --step paper-live --frozen-hash <h>` collects the paper evidence: demo `paper` runs of versions with that `frozen_hash` that started after the latest `candidate → paper` pass. The runs are chained in `started_at` order into one realized equity series that starts from the first run's `run_config.starting_balance_usd` and moves by each closed position's net P&L and each funding payment. `evaluated_at` comes from the caller (the CLI passes the current UTC time), never from inside `trading_core.gate`. It writes one `paper → live` promotion with reasons and metrics, plus evidence on those paper runs and on the study's holdout run. With no `candidate → paper` pass, or no completed holdout behind it, the verdict is `insufficient`. So a version promoted only by hand (with no study) can never reach live, by design. The verdict follows AC-12's precedence (clarified 2026-10-21): every paper check runs even below the `paper_duration` and `paper_trade_floor` floors, and a failed check outranks them. So under 30 days of paper with every other check passing gives `insufficient` with `paper_duration`, while under 30 days with a paper return after costs ≤ 0 gives `fail` with `paper_net_positive` (plus the `paper_duration` reason). Such an early `fail` has no side effect beyond the live guard, which reads only the latest `paper → live` promotion (AC-17), so a later evaluate past the floors can still pass.
- **AC-17** (live guard): `start_run` with `mode = 'live'` matches the latest `paper → live` promotion by `frozen_hash` (not version id). It must be `pass` and `decided_by` `gate`. Today's `git_commit` `unknown` refusal stays.

*Dashboard*
- **AC-18** (ranking): `/strategies` shows one row per study, sorted by walk forward Sharpe (descending, nulls last), with ties broken by walk forward return after costs. Columns: strategy, lab, entry mode, stage, latest verdict, walk forward Sharpe, holdout Sharpe against buy and hold Sharpe, holdout return after costs, holdout max drawdown (with its basis), deflated Sharpe probability, PBO, holdout trades, and win rate (shown, not sortable). Stage is derived when the page is read. A study with no completed holdout shows stage `developing` with `—` in every metric column. A study with a holdout but no evaluation shows stage `candidate`, verdict `not evaluated`, and `—` in the metric columns. The empty state stays when there are no studies.
- **AC-19** (study detail): `/strategies/{study_id}` shows each fold (ranges, chosen grid point, in sample and out of sample Sharpe, return, trades), the bootstrap bounds, concentration shares, benchmark numbers, trial count (distinct `frozen_hash` over this study's `development` and `holdout` runs, benchmarks excluded), and every attempt's reasons. An unknown id returns 404. *Amended by spec 0018 AC-19: it also shows "trials in family (N)" from the promotion metrics.*
- **AC-25** (entry mode in the hash, 2026-10-21): for a strategy that has an `entry_mode` setting (registry `default_entry_mode` set), the version config the lab records for a replay or study run (and so `version_hash` and `frozen_hash`) includes the effective `entry_mode`, which equals the value `_strategy_config` merges into the running strategy: the variant's (`--pair-entry` makes a `taker` variant; a study's base variant takes `[study].entry_mode`), else `[strategy.config]`'s, else the default. A taker study and a maker study of the same strategy, config, and code therefore get different `frozen_hash` values, each spends its own holdout, and each needs its own promotions. A batch whose variants differ in entry mode records one strategy version per entry mode, and each variant's runs point at its own. This is a new `RunFile.version_config(entry_mode)` used by `replay()` and the `lab holdout` freeze only. Parity keeps the current config (it matches the trader's stored config, which has no `entry_mode`), and a `[paper]` run never gets `entry_mode`. A strategy with no `entry_mode` setting keeps its version config unchanged, so `random_taker`, `placeholder_timed`, and benchmark hashes stay the same. Versions recorded before this change keep their hashes; a study whose development ran before it is refused at `lab holdout` by the existing frozen recheck ("start a new study").
- **AC-26** (deploy promotion check, 2026-10-21): `kfs-deploy run`, after pulling the new images and before the drain, runs `compose_new run --rm --no-deps -T trader python -m trader.version` and captures its output (any non zero exit, including 78, refuses). `trader.version` output gains `git_commit` (the trader image's `Settings.git_commit`). The output is then fed to `compose_new run --rm --no-deps -T gate check-version`. `--no-deps` keeps the new release's `migrate` from running before the drain; Postgres is already up from the current release, and `trader.version` needs no secrets. `gate check-version` reads the JSON from stdin and takes `TRADING_ENV` from its own settings, never from the JSON. A missing or `unknown` `git_commit`, a missing `frozen_hash`, or bad JSON exits 1. It records the version through `ensure_strategy_version` with the JSON's `git_commit` passed explicitly (never None), so the row is the same one the trader creates at start. That row stays even when the deploy is refused, by design, because the promote command needs its id. In demo it applies the paper guard by calling the repository's guard (made public as `require_paper_promotion`, the same code `start_run` uses, AC-14). In any other env it exits 1 ("the live deploy check arrives with feature 19"), because the trader only paper trades today. It prints `{version_id, frozen_hash, stage_needed: "paper", verdict}`, where `verdict` is `pass`, `missing`, or the latest promotion's stored verdict, and exits 0 on `pass`, 3 otherwise, and 1 on any error (database unreachable included). Deploy turns every non zero exit into the existing `refuse` (log `preflight refused`, record `preflight_refused`, exit 4) with a reason naming the step and verdict, and on exit 3 it prints `kfs-compose run --rm gate promote --manual --step candidate-paper --version <version_id> --note '...'`. Nothing is paused or rolled back, and there is no flag to skip the check. Bootstrap: the release that ships this check is the first whose trader image prints `git_commit`, and the check always runs the new image, so it applies from that deploy on.
- **AC-20** (promotion log): `/strategies/log` lists every promotion newest first, 50 per page (422 below 1 or not an integer), with time, strategy, version, `frozen_hash` (first 12 hex), step, verdict, decided by, reason codes, and note.
- **AC-21** (paper progress): for each `frozen_hash` whose stage is `paper`, the page shows paper days and trades against the floors, paper return and drawdown, mean trade return against the lab bound, and fill rate and slippage drift, all computed when the page is read.

*Guards*
- **AC-22** (permissions): `gate_app` has SELECT on every table and INSERT on the bundle tables, the new tables, `promotions`, and `promotion_evidence`. It has no UPDATE or DELETE. It cannot INSERT into `control_commands`. `lab_app` gains SELECT and INSERT on `studies`, `study_runs`, `run_daily_equity`, and `run_measures`, and SELECT on `promotions`, `promotion_folds`, and `promotion_evidence`. `trader_app`, `dashboard_app`, and `backup_app` get SELECT on the new tables. Without `postgres_gate_password`, `gate_app` is created `NOLOGIN`.
- **AC-23** (boundaries): `trading_core.gate` imports nothing outside the stdlib and `trading_core.gate` itself, and the boundary test enforces that. The `gate` image and the dashboard import no NautilusTrader. The `gate` service runs in either `TRADING_ENV`, and the rows it imports are always demo.

### Gate checks (candidate → paper)

Thresholds are the v1 values of `thresholds.toml`, frozen into each study. When the studied strategy is a benchmark's own strategy, that benchmark's check (`beats_buy_and_hold` or `beats_macd_baseline`) is not applicable and passes. It follows the `pbo` pattern: a metric `beats_buy_and_hold_applicable` or `beats_macd_baseline_applicable` set to `false`, the benchmark Sharpe metric `null`, and no reason row, so it never reports `missing_benchmark`.

| Code | Check | Fails when | Insufficient when |
|---|---|---|---|
| `trade_floor` | holdout trades | n/a | below 200 (replay) or 60 (candle) |
| `fee_hurdle` | holdout `avg_gross_edge` against `avg_round_trip_cost` | edge < 1.5 × cost | cost missing |
| `holdout_sharpe` | holdout daily Sharpe | ≤ 0 | fewer than 2 days or zero variance |
| `beats_buy_and_hold` | holdout Sharpe against buy and hold Sharpe on the same days | ≤ benchmark | benchmark missing (not applicable, passes, in a study of `buy_and_hold`) |
| `beats_macd_baseline` | hour level only, the same comparison against `macd_baseline` | ≤ benchmark | benchmark missing (not applicable, passes, in a study of `macd_baseline`) |
| `walk_forward_sharpe` | walk forward series Sharpe | ≤ 0 | a fold has no completed grid point |
| `bootstrap_lower_bound` | 95% lower bound of mean holdout daily return | ≤ 0 | fewer than 10 days |
| `deflated_sharpe` | deflated Sharpe probability | < 0.95 | n/a |
| `pbo` | PBO | > 0.30 | n/a (under 4 completed grid points: not applicable, passes) |
| `drawdown_budget` | holdout max drawdown: `run_measures.marked_max_drawdown` for candle, `run_results.max_drawdown` (realized) for replay | > 0.10 | the column is NULL |
| `concentration_fold` | largest fold's share of walk forward net profit | > 0.60 | n/a (skipped when net profit ≤ 0, which another check already fails) |
| `concentration_direction` | long or short share of holdout net profit | > 0.60 | n/a (as above) |
| `concentration_top_trades` | top 5% (rounded up) of holdout trades' share of net profit | > 0.60 | n/a (as above) |

### Gate checks (paper → live)

| Code | Fails when | Insufficient when |
|---|---|---|
| `paper_duration` | n/a | under 30 days since the first evidence run started |
| `paper_trade_floor` | n/a | under 100 trades (scalping) or 20 (hour level) |
| `paper_net_positive` | paper return after costs ≤ 0 | n/a |
| `paper_drawdown_budget` | paper realized max drawdown > 0.10 | n/a |
| `paper_lab_band` | paper mean trade return < holdout trade return lower bound | holdout bound missing |
| `fill_rate_drift` | maker entries only: \|paper fill rate ÷ holdout fill rate − 1\| > 0.25 | n/a (not applicable when the study's entries are taker) |
| `slippage_drift` | paper mean entry slippage − lab entry slippage > 2 bps (lab side: the holdout's measured slippage for replay, the run file's configured slippage in bps for candle) | no paper entry fills with a decision `mid` |

## Decision

**Chosen option**: Option 2: Studies on the laptop, one pure gate on the VPS.

The lab runs each strategy as a study of development runs, benchmarks, and one holdout. Bundles are imported into the VPS Postgres. A pure stdlib gate in `trading_core.gate` turns them into an append only log of promotions, which the trader's paper and live guards and the dashboard read.

**Implementation skills**: `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `htmx` (`.claude/skills/htmx/`) · `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

```
laptop (lab profile)                                  VPS
lab study ─► development runs (every grid point) ─┐
          └► benchmark runs (candle)              ├─► .bundle.json ─► push-bundles.sh ─► gate import ─► Postgres
lab holdout ─► one holdout run (final point) ─────┘                                                     │
lab gate --dry-run (reads laptop Postgres, writes nothing)        gate evaluate ─► promotions + folds ◄──┤
                                                                  gate promote --manual ─► promotions    │
                                                     trader start_run (paper/live guards) ◄──────────────┤
                                                     dashboard /strategies (read only) ◄─────────────────┘
```

**Code layout**:
- `trading_core/gate/` (new pure folder): `thresholds.py` (pydantic model plus loader of the packaged `thresholds.toml`, using `tomllib` on bytes the caller passes in), `series.py` (daily returns, slicing, Sharpe), `folds.py`, `stats.py` (stationary bootstrap, Politis and White block length, CSCV PBO, deflated Sharpe with `statistics.NormalDist`), `checks.py`, `verdict.py` (inputs are frozen dataclasses, output is `GateResult`).
- `trading_core/versioning.py` (new, does file I/O, not pure): `code_closure(strategy)`, `code_hash`, `frozen_hash`.
- `trading_core/recording/`: `bundle_codec.py` (moves `bundle_json` from `lab/report.py` and adds `parse_bundle`), and repository functions `record_study`, `record_study_run`, `gate_inputs(study_id)`, `paper_inputs(frozen_hash)`, the guards, and `record_promotion` extended.
- `apps/gate/` (new workspace app, core only, no `[node]` extra): `gate import | evaluate | promote | versions`.
- `apps/lab/`: `study`, `holdout`, and `gate --dry-run` subcommands; bundles gain the new rows.
- `apps/trader/`: `python -m trader.version` prints `{strategy, kind, config, code_hash, frozen_hash}` without starting the node, for manual promotion before a deploy.
- `apps/dashboard/`: the `/strategies`, `/strategies/{study_id}`, and `/strategies/log` pages, rendered when requested (no SSE).

**Data model sketch** (migration `20261019_0009_gate.py`, additive):

| Table | Key | Fields | Relationships, constraints |
|---|---|---|---|
| `strategy_versions` (+2) | existing | `code_hash text NULL`, `frozen_hash text NULL` | index `(frozen_hash)`, not unique |
| `studies` | `id` uuid7 | `strategy_id`, `name text`, `lab text` (`candle`,`replay`), `code_hash text`, `plan jsonb`, `plan_hash text`, `thresholds jsonb`, `thresholds_hash text`, `created_at` | UNIQUE `name`; FK strategies; immutable |
| `study_runs` | PK (`study_id`,`run_id`) | `role text` (`development`,`holdout`,`benchmark`), `grid_point int NULL` (index into the plan's grid; NULL for benchmarks), `frozen_hash text`, `status text` (the run's terminal status), `created_at` | FK studies; FK `strategy_runs.id` UNIQUE `run_id`; partial UNIQUE `(frozen_hash)` WHERE `role = 'holdout' AND status = 'completed'`; immutable |
| `run_daily_equity` | PK (`run_id`,`day`) | `env`, `day date`, `equity_close numeric`, `basis text` (`marked`,`realized`), `trades_closed int`, `fees numeric`, `funding numeric`, `currency text` | composite FK `(run_id, env)` → `strategy_runs`; immutable |
| `run_measures` | PK `run_id` | `env`, `drawdown_basis text`, `marked_max_drawdown NULL`, `longest_drawdown_hours int NULL`, `exposure NULL`, `long_trades int`, `long_net_pnl`, `short_trades int`, `short_net_pnl`, `profit_factor NULL`, `expectancy NULL`, `top_trades_profit_share NULL`, `avg_gross_edge`, `avg_round_trip_cost`, `currency` | composite FK `(run_id, env)`; immutable |
| `promotions` (+6) | existing | verdict CHECK gains `insufficient`; `frozen_hash text NULL`, `study_id uuid NULL`, `thresholds_hash text NULL`, `reasons jsonb NOT NULL DEFAULT '[]'`, `metrics jsonb NOT NULL DEFAULT '{}'`, `gate_commit text NULL` | FK studies; index `(frozen_hash, to_stage, decided_at)`; CHECK `decided_by = 'gate' OR to_stage <> 'live'`; CHECK `decided_by = 'manual' OR frozen_hash IS NOT NULL` |
| `promotion_folds` | PK (`promotion_id`,`fold`) | `is_start date`, `is_end date`, `oos_start date`, `oos_end date`, `chosen_grid_point int`, `is_sharpe`, `oos_sharpe NULL`, `oos_return`, `oos_trades int`, `oos_net_pnl` | FK promotions; immutable |

Money columns are `numeric` (Decimal). `RunBundle` gains `study`, `study_run`, `daily_equity`, and `measures`, all optional, so a plain `lab backtest` bundle stays valid. `write_run_bundle` inserts the study with `ON CONFLICT (name) DO NOTHING`, then compares `plan_hash`. A mismatch raises `RecordConflictError`.

`avg_gross_edge` is the sum of (net P&L + fees − funding) over trades, divided by the trade count. `avg_round_trip_cost` is (fees_total + max(0, −funding_total)) over the trade count, in USD.

**State transitions** (stage of a `frozen_hash`, derived when read, never stored):
`candidate` → `paper` when the latest `candidate → paper` promotion is `pass`. `paper` → `live` when the latest `paper → live` promotion is `pass` by `gate`. Any later non pass promotion on a step drops the stage back to the one before it. A study's own status is derived too: `developing` (no completed holdout), `held out`, `evaluated` (has a promotion).

**API surface**:

| Command or route | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `lab study <studyfile>` | CLI | study TOML (req), `--seed` (opt) | `studies`, development and benchmark runs, `lab-reports/<batch>/` bundles | `lab_app` | 64 bad file or plan hash clash; 2 missing data; 1 a grid point failed; 78 live env or unknown commit |
| `lab holdout <study>` | CLI | study name (req) | one holdout run plus holdout benchmarks, bundles | `lab_app` | 64 holdout already spent, or study unknown; 1 run failed |
| `lab gate <study> --dry-run` | CLI | study name | verdict, reasons, metrics printed | `lab_app` SELECT | 64 unknown study |
| `scripts/push-bundles.sh <dir>...` | shell | batch dirs | files on the VPS, import summary | Tailscale SSH as `kfs` | nonzero if rsync or import fails |
| `gate import [dir]` | CLI | dir (default `/bundles`) | counts loaded, skipped, refused | `gate_app` | 1 any file refused |
| `gate evaluate <study>` | CLI | study name | `promotions` id, verdict, reasons | `gate_app` | 64 unknown study |
| `gate evaluate --step paper-live --frozen-hash <h>` | CLI | frozen hash | `promotions` id, verdict | `gate_app` | 64 unknown hash |
| `gate promote --manual --step candidate-paper --version <id> --note <t>` | CLI, TTY | version id, note | `promotions` id | `gate_app` | 64 no TTY, empty note, name mismatch, step `live` |
| `gate versions [--strategy n]` | CLI | name filter | version id, frozen hash, stage | `gate_app` SELECT | none |
| `python -m trader.version` | CLI | trader settings | JSON of the version the trader would start | none (no DB) | 78 bad config |
| `GET /strategies` | GET | none | ranking table, paper progress | dashboard session | none |
| `GET /strategies/{study_id}` | GET | uuid path | study detail | dashboard session | 404 unknown, 422 bad uuid |
| `GET /strategies/log?page=n` | GET | page int (opt) | 50 promotions | dashboard session | 422 bad page |

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| `ensure_strategy_version` | `code_hash`, `frozen_hash` | `versioning.code_closure` over the installed `trading_core` source, plus the config argument |
| `lab study` | development range end, `holdout_start` | `thresholds.toml` `holdout_start = 2025-10-19` (today minus 12 months, fixed in v1) |
| `lab study` | fold layout | study file override, else the `thresholds.toml` defaults per lab |
| `lab study` | grid points | study file `grid`, expanded in the listed order; `grid_point` is its index |
| `lab study` | which replay days | `trading_core.lab.study_days.development_days(from, holdout_start)`: every 1st of the month in `from`..`holdout_start − 1`, Tardis only; stored in `plan.days.development` (AC-24) |
| `lab study` | earliest replay `from` | constant `TARDIS_FIRST_DAY = 2022-04-01` in `trading_core.lab.study_days` (PF_XBTUSD on Tardis since 2022-03-22, first whole 1st of the month) |
| `lab study` | replay grid cap | constant `MAX_REPLAY_GRID_VARIANTS = 4` in `trading_core.lab.variants`, beside `MAX_GRID_VARIANTS = 16` |
| `lab study` | replay entry mode | `[study].entry_mode` → the base `VariantSpec.entry_mode` (unset for a strategy with no `entry_mode` setting, which must be `taker`) |
| `lab holdout` (replay) | holdout days and end | `study_days.holdout_days(holdout_start, today)` with `today` = the current UTC date passed by the CLI: every 1st of the month from `holdout_start` to the latest one before `today`; `data_end_at` = the replay's end of last clean segment |
| `lab study`, `lab holdout` (replay) | missing days | planned days with no `catalog-lab/tardis/manifests/<day>.json`; exit 2 listing them and the `lab import-tardis` command (in `load_study` before `record_study`; in `lab holdout` after the lock) |
| `lab study`, `lab holdout` (replay) | dropped days | planned days whose gap file covers all 24 hours (`plan_days` gives them no segment); `plan.days.dropped` for development, `run_config.dropped_days` for the holdout |
| `lab study` (replay) | each grid point's version | one `replay()` call per remaining grid point, `store.prepare` with `{**config, **grid_point}`; `replay()` gains an optional `study` context that attaches the `study_run` to the bundle (as `backtest(..., study=)` does) |
| `lab holdout` (replay) | holdout config, entry mode | the stored plan's `run_file`, `grid[final]`, and `study.entry_mode`; never the CLI. `_holdout_locked` branches on `studies.lab`, and the spent check, frozen recheck, and benchmark list are shared |
| `lab holdout` (replay) | benchmark instrument and fees | `instrument_id`, `maker_fee`, `taker_fee` from the replay run file, passed to the benchmark builder as a plain tuple that both labs fill |
| `lab study` | grid cap per lab | `_grid_problems` takes the cap as a parameter: `MAX_GRID_VARIANTS` (candle), `MAX_REPLAY_GRID_VARIANTS` (replay) |
| `lab holdout` | final grid point | max development `run_results.risk_adjusted_ratio` among completed `development` runs, ties to the lowest index |
| `lab holdout` | run the holdout, or only benchmarks | first a `completed` `holdout` `study_runs` row with this `study_id` → benchmarks only (its `frozen_hash` must equal the final grid point's, else exit 64); otherwise a `completed` holdout row for the final `frozen_hash` in any study → exit 64; none → holdout, then benchmarks |
| `lab holdout` (resume) | benchmark range | `holdout_start` to the stored holdout run's `strategy_runs.data_end_at` |
| `lab holdout` | which benchmarks to run | `buy_and_hold`, plus `macd_baseline` when `strategies.kind` is `hour_level`, minus the studied strategy's own name, minus names with a `completed` `benchmark` run in the study |
| `gate evaluate` | daily returns per run | `run_daily_equity` ordered by day, with the starting balance from `run_config.starting_balance_usd` |
| `gate evaluate` | holdout trades, long and short split, top trade share, edge and cost | `run_results.trade_count`, `run_measures` |
| `gate evaluate` | per trade returns for the trade bound | the holdout run's closed `positions` ordered by `closed_at`, then `opened_ns`. Net P&L = `realized_pnl` (already after fees) + `funding_total` (positive means received). Gross = `realized_pnl + fees_total`. Each trade's return is net over equity before it, rebuilt from the starting balance |
| `gate evaluate` | trial count N | `count(distinct study_runs.frozen_hash)` joined through `studies.strategy_id`, where `role <> 'benchmark'` |
| `gate evaluate` | benchmark Sharpe | `study_runs` role `benchmark` with strategy `buy_and_hold` or `macd_baseline`, `run_daily_equity` restricted to the study run's days |
| `gate evaluate` | a benchmark run's strategy name | `study_runs.run_id` → `strategy_runs.strategy_version_id` → `strategy_versions.strategy_id` → `strategies.name` |
| `gate evaluate` | self benchmark (check not applicable) | the study's `strategies.name` (through `studies.strategy_id`) equal to the benchmark's name |
| `/strategies/{study_id}` | trial count | `count(distinct study_runs.frozen_hash)` for the study, where `role <> 'benchmark'` |
| `gate evaluate` | missing cost flags | `run_results.fees_total`, `funding_payments` against positions spanning charge times, the slippage setting (candle) and latency setting (replay) in `run_config`, under the key names specs 0014 and 0008 already write |
| `gate evaluate` | `gate_commit` | `Settings.git_commit` of the gate image |
| `gate evaluate --step paper-live` | paper daily equity | demo `paper` runs' `positions` and `funding_payments`, starting from `run_config.starting_balance_usd` of the first evidence run, realized basis |
| `gate evaluate --step paper-live` | paper days | first evidence run's `started_at` to now |
| `gate evaluate --step paper-live` | which orders are entries | `orders.origin = 'strategy'` whose `decision_id` points at a decision with `intent = 'enter'` |
| `gate evaluate --step paper-live` | paper and lab fill rate | paper: entries with `post_only` true, count with `status = 'filled'` ÷ all; lab side: `run_fill_stats` maker row of the holdout run |
| `gate evaluate --step paper-live` | entry slippage (bps) | entry `fills.price` (through `fills.order_id` → `orders.decision_id`) against `decisions.inputs.mid`, signed so worse for the trade is positive; fills whose decision has no `mid` are skipped. Lab side: the same on the replay holdout, or the run file's slippage bps for a candle study |
| `gate evaluate --step paper-live` | trade floor by kind | `strategies.kind` (`scalping` or `hour_level`) |
| `start_run` paper, live | latest promotion | `promotions` by `(frozen_hash, to_stage)` ordered by `decided_at` desc, then `id` desc, limit 1 |
| `/strategies` | stage per study | no completed holdout: `developing`; otherwise the latest promotions for the holdout run's `frozen_hash` |
| `/strategies` | ranking numbers | latest `candidate → paper` promotion's `metrics` for the study; NULL when none |
| `/strategies` paper progress | all numbers | the same pure paper functions the gate uses, run when the page is read |

**Key invariants**:
- No promotion, study, study run, daily equity, measure, or fold row is ever updated or deleted (no grants for it). Rows written before the 2026-10-19 amendment (a self benchmark run, benchmarks once counted as trials) stay, and the amended rules simply ignore them.
- The thresholds that judge a study are the ones it was created with.
- At most one completed holdout run per `frozen_hash`.
- No manual promotion to live (DB CHECK plus `ManualLivePromotionError`).
- Paper and live guards match on `frozen_hash`. The latest promotion per step decides.
- Lab bundles can never create paper or live rows on the VPS (import validation plus `strategy_runs` env CHECK).
- Win rate is never a sort key or a check.
- The gate is deterministic: no wall clock or unseeded randomness inside `trading_core.gate`. `evaluated_at` comes from the caller.

**Security model**:
- `gate_app` (new role, secret `postgres_gate_password` under `/run/secrets`, loaded through `trading_core.secrets`) is used only by the one shot `gate` service. It has INSERT and SELECT as in AC-22, and no UPDATE, DELETE, or control stream access.
- Only someone who can SSH to the VPS as `kfs` can run `gate`. A manual promotion needs a live TTY and the typed strategy name, and it is always logged with its note.
- The dashboard stays read only (SELECT), shows no promote button, and imports no Nautilus.
- No Kraken key is involved. Bundles hold no secrets. No regulated data. The repo is public, so `thresholds.toml` and study files are world readable, which is fine.

**Configuration required**:
- `postgres_gate_password` (secret file): `gate_app` login on the VPS, created by `scripts/dev-secrets.sh` on the laptop (optional there) and placed by `place-secrets.sh` on the VPS.
- `GATE_BUNDLE_DIR` (setting, default `/bundles`, mounted from `/var/lib/kfs/bundles`): where `gate import` reads. It has a default, so `/etc/kfs/kfs.env` needs no new line.
- `trading_core/gate/thresholds.toml` (package data, versioned): `version = 1`, `holdout_start = 2025-10-19`, `fee_hurdle_k = 1.5`, `max_drawdown = 0.10`, `pbo_max = 0.30`, `cscv_blocks = 10`, `dsr_min = 0.95`, `bootstrap_resamples = 10000`, `bootstrap_min_block_days = 5`, `bootstrap_confidence = 0.95`, `concentration_max_share = 0.60`, `top_trades_quantile = 0.05`, `[candle] first_in_sample_months = 12, step_months = 6, min_holdout_trades = 60`, `[replay] first_in_sample_days = 15, step_days = 7, min_holdout_trades = 200`, `[paper] min_days = 30, min_trades_scalping = 100, min_trades_hour_level = 20, fill_rate_tolerance = 0.25, slippage_tolerance_bps = 2`.
- Compose: a `gate` service (profile `gate`, so `up` never starts it) built from a core only image target, with `secrets: postgres_gate_password`, the bundle volume, and the `migrate` dependency.

**Critical test scenarios**:
- Happy path: a candle study of `macd_baseline` with a 2 point grid, run through study, holdout, push, import, and evaluate, gives one promotion with folds and evidence, and the study shows on `/strategies`. Verifies **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-7**, **AC-8**, **AC-18**.
- Replay days: `from = 2022-04-01` gives 43 development days ending 2025-10-01 and 4 folds; `from = 2024-01-01` (22 days) is accepted with 1 fold; `from = 2024-02-01` (21 days) exits 64; a day whose gap file covers every hour is listed in `plan.days.dropped`, and with 22 days that drop makes it exit 64; a missing manifest exits 2 and writes no `studies` row; `from = 2022-04-02` or `2022-03-01` exits 64; with `today` 2026-10-20 the holdout is the 12 days 2025-11-01..2026-10-01, and on 2026-11-01 it is still those 12; one missing manifest exits 2 naming that day and runs nothing. A replay grid of 5 points, `source = "own"`, or `entry_mode` in `[strategy.config]` exits 64; `lab replay` with a grid exits 64. Verifies **AC-2**, **AC-24**.
- Known loser: `random_taker` on replay fails on `fee_hurdle` and `holdout_sharpe`. A synthetic series of 20 noise grid points gives PBO above 0.30 and deflated Sharpe below 0.95. Verifies **AC-10**, **AC-11**.
- Folds: a synthetic daily matrix where grid point 0 wins in sample and grid point 1 wins out of sample gives the expected chosen points, walk forward series, and fold rows. Verifies **AC-9**.
- Fail closed: zero fees, a missing benchmark, a missing holdout, or a NULL code hash each give `insufficient` with the right code. A fail plus an insufficient gives `fail`. Verifies **AC-12**.
- Determinism: two evaluations of the same study give identical `metrics` jsonb. Verifies **AC-8**, **AC-11**.
- Frozen version: the same closure on two commits gives equal `frozen_hash`. Editing one indicator file changes it. Verifies **AC-1**.
- Holdout once: a second `lab holdout` exits 64. A crashed first attempt allows a rerun. Two concurrent inserts hit the unique index. A `lab holdout` stopped during a benchmark, run again, runs only the missing benchmarks and no holdout over the holdout's stored range (even after the catalog end moved), then a third run exits 64. A benchmark that fails during a resume exits 1 and reruns no holdout. Another study reaching the same `frozen_hash` exits 64. Verifies **AC-4**, **AC-5**.
- Self benchmark on resume: a stopped `macd_baseline` study resumes with only `buy_and_hold`. Verifies **AC-4**.
- Benchmarks and trials: a study of `macd_baseline` runs no `macd_baseline` benchmark and its `beats_macd_baseline` check passes as not applicable. A 2 point study with its benchmarks gives N = 2 and shows "2 trials". Verifies **AC-4**, **AC-10**, **AC-11**, **AC-19**.
- Import trust: a bundle with a `paper` run or env `live` is refused whole. A repeat import adds nothing. Verifies **AC-7**.
- Paper guard: the trader with no promotion exits 78. After a manual promotion it starts. After a later `fail` it exits 78 again. Verifies **AC-14**, **AC-15**.
- Live guard: a manual live promotion is refused by the CHECK and by the error. Live `start_run` with a gate pass for the same `frozen_hash` on another commit is allowed. Verifies **AC-15**, **AC-17**.
- Paper to live: 29 days with every other paper check passing gives `insufficient` with `paper_duration`. 29 days with a paper return ≤ 0 gives `fail` with `paper_net_positive` and a `paper_duration` reason. A paper mean below the lab bound gives `fail`. Fill rate 40% against lab 60% gives `fail`. Verifies **AC-16**.
- Auth/permission: `gate_app` UPDATE on `promotions` and INSERT on `control_commands` are denied. `lab_app` INSERT on `promotions` is denied. `dashboard_app` INSERT anywhere is denied. Verifies **AC-22**.
- Boundary: `trading_core.gate` importing anything beyond the stdlib fails the boundary test. The `gate` image has no `nautilus_trader`. Verifies **AC-23**.

## Build plan

Tracer Bullet: one candle study goes end to end first (laptop to VPS to promotion to dashboard row), then each segment thickens.

**Slice 1: thin thread (one candle study to a dashboard row)**
1. Migration `0009`: every table and column above, the `insufficient` verdict, the CHECKs, the indexes, `gate_app` through `db_roles` (optional secret, `NOLOGIN` without it), and the grants. Plus `postgres_gate_password` in `dev-secrets.sh`. Satisfies **AC-22**.
2. `trading_core.versioning` and `ensure_strategy_version` storing both hashes, with the trader and lab passing through. Satisfies **AC-1**.
3. `thresholds.toml` plus `gate/thresholds.py`; `gate/series.py` (daily returns, Sharpe); a first `verdict.py` with `trade_floor`, `holdout_sharpe`, `beats_buy_and_hold`. Satisfies **AC-6**, **AC-10** (partial), **AC-12** (partial).
4. Lab: `run_daily_equity` and `run_measures` from both labs' metrics, `run_results` Sharpe, `RunBundle` and `write_run_bundle` extended, `bundle_codec.py` moved to core. Satisfies **AC-3**, **AC-6**.
5. `lab study` (candle) and `lab holdout`, with benchmarks and the holdout once guard. Satisfies **AC-2**, **AC-4**, **AC-5**.
6. `apps/gate` app, image target, and Compose service; `gate import` with strict validation; `scripts/push-bundles.sh`. Satisfies **AC-7**, **AC-23**.
7. `gate evaluate` writing the promotion and evidence; `/strategies` ranking table from promotion metrics. Satisfies **AC-8**, **AC-18**.

**Slice 2: the full candidate gate**
8. `gate/folds.py`, walk forward series, `promotion_folds`. Satisfies **AC-9**.
9. `gate/stats.py`: stationary bootstrap with Politis and White block length, CSCV PBO, deflated Sharpe, the trade return bound. Each one is unit tested against hand worked and published examples. Satisfies **AC-11**.
10. The remaining checks and the missing data reasons; fail before insufficient before pass. Satisfies **AC-10**, **AC-12**.
11. `lab gate --dry-run`; a determinism test. Satisfies **AC-13**, **AC-8**.

**Slice 3: replay studies**
12. `lab study` for replay, with `entry_mode` taker and maker as separate studies, replay development and holdout day selection, and benchmark day limiting. Then run the `random_taker` loser study end to end. Satisfies **AC-2**, **AC-3**, **AC-4**, **AC-10**, **AC-24**. In order:
    1. Spike first: import 2022-04-01 and `lab replay` a scratch copy of `runs/random_taker.toml` over that day, to confirm that 2022 Tardis data (tick and lot size, file layout, gap files) works with the pinned instrument. If it does not, stop and bring the result back to `/architect` before going further.
    2. Core: `trading_core/lab/study_days.py` (pure: `TARDIS_FIRST_DAY`, `development_days`, `holdout_days`, `missing_days`, `dropped_days` from the gap hours) and `MAX_REPLAY_GRID_VARIANTS`, with unit tests. Satisfies **AC-24**.
    3. Run file: the replay `StrategySection` gains `grid`, and `_grid_problems` takes the cap as a parameter (4 for replay); `lab replay` refuses a grid. Satisfies **AC-2**.
    4. `load_study` for `lab = "replay"`: the replay checks of AC-2 and AC-24, the missing and dropped day checks before `record_study`, and the plan with `days.development` and `days.dropped`. Then `run_study` on the replay path: one `replay()` call per remaining grid point as a `base` variant, with `replay()` gaining the optional `study` context; resume works as for candle. Satisfies **AC-2**, **AC-3**, **AC-24**.
    5. `lab holdout` for replay: `_holdout_locked` branches on `studies.lab`. After the lock come `holdout_days` from the CLI's UTC date and the missing and dropped day checks, then the final grid point from the stored plan over those days. Candle benchmarks run over `holdout_start`..the last holdout day with the instrument and fees tuple (the gate already joins them on the holdout's days). Satisfies **AC-4**, **AC-5**, **AC-24**.
    6. Data and proof: `lab import-tardis 2022-04-01 2025-10-01` and `lab import-tardis 2025-11-01 2026-07-01` (52 new days, about 24 GB; the Tardis catalog then holds 55 days, about 26 GB, under the 40 GB `LAB_CATALOG_CAP_GB`). Add `studies/random_taker.toml` (the run file `runs/random_taker.toml` verbatim with `from = 2022-04-01`, `to = 2025-10-18`, no grid, `[study] name = "random_taker", lab = "replay", entry_mode = "taker", seed = 42`). Run study, holdout, push, import, and evaluate (expect `fail` on `fee_hurdle` and `holdout_sharpe`), then tick verify row 24. Satisfies **AC-10**.

**Slice 4: paper and live guards**
13. `python -m trader.version`; `gate versions`; `gate promote --manual` with the TTY and name check; `ManualLivePromotionError`. Satisfies **AC-15**.
14. Deploy without the guard, then manually promote the running placeholder's `frozen_hash` (note `pre gate placeholder`), so the guard has a pass to find. Satisfies **AC-14** (transition).
15. Paper guard in `start_run`; the trader logs and exits 78; live guard moved to `frozen_hash` plus `gate`. Satisfies **AC-14**, **AC-17**.
16. `gate evaluate --step paper-live` with the paper inputs, fill rate, slippage, and lab band. Satisfies **AC-16**.

**Slice 5: dashboard depth and proof**
17. `/strategies/{study_id}`, `/strategies/log`, and paper progress. Satisfies **AC-19**, **AC-20**, **AC-21**.
18. Permission tests for every role, the boundary test for `gate/`, the guard tests, and `verify.md` complete. Satisfies **AC-22**, **AC-23**.

**Slice 6: fixes from the first verify (2026-10-19)**
19. `lab holdout` resumes missing benchmarks over the stored holdout range when this study already has its completed holdout, and skips the studied strategy's own benchmark (one shared benchmark rule for lab and gate); the gate marks a self benchmark check not applicable with the `pbo` style metric; N and the dashboard trial count leave out `benchmark` rows. Tests for each. Satisfies **AC-4**, **AC-5**, **AC-10**, **AC-11**, **AC-19**.

**Slice 7: amendments from task 12 and the first guarded deploys (2026-10-21)**
20. `RunFile.version_config(entry_mode)` used by `replay()` (one `store.prepare` per distinct effective variant entry mode, mapped per variant) and by the `lab holdout` freeze; parity and `[paper]` runs unchanged. Tests: a taker and a maker study of `maker_probe` get different `frozen_hash` values and both can run a holdout; a `--pair-entry` batch records two versions; `random_taker` and parity hashes are unchanged. Satisfies **AC-25**.
21. `trader.version` prints `git_commit`; `require_paper_promotion` made public; `gate check-version` (stdin JSON, exits 0, 3, 1, demo only); the `kfs-deploy` preflight step after the pull, through `refuse`. Tests: `check-version` against a throwaway database for pass, missing, fail, bad JSON, missing `git_commit`, and a live env; the version id it prints equals the one the trader records at start; a laptop drill of the refusal. Satisfies **AC-26**.

## Consequences

**Positive**:
- Every attempt is logged with machine readable reasons, and moving the goalposts after seeing results is blocked by frozen thresholds and the holdout once rule.
- Ordinary deploys no longer reset paper evidence. A real strategy change always does.
- The ranking can't be topped by a curve fit, because it sorts on walk forward out of sample Sharpe.
- No new dependency. The gate is pure stdlib, small, and deterministic.

**Negative / tradeoffs**:
- Strict thresholds will likely reject most early candidates, scalpers especially (fee hurdle plus trade floor). That's the point, but expect many `fail` and `insufficient` rows before a pass.
- The holdout is a one shot per frozen version. A careless rerun of a near miss with one changed value spends a new holdout, and it still counts as a trial.
- Hand written statistics (bootstrap, PBO, deflated Sharpe) carry correctness risk. They need careful tests against known values.
- Results reach the VPS only when you push them, so the dashboard lags the laptop by design.
- The code hash closure includes shared modules, so fixing a shared indicator gives every strategy that uses it a new version, which then needs gating again. The same goes for a NautilusTrader upgrade (it is in `frozen_hash`): the pending Nautilus upgrade resets all paper evidence.
- A version promoted by hand (like the placeholder) can reach paper but never live.
- A study of a benchmark strategy itself (`macd_baseline`, `buy_and_hold`) is judged without that one comparison, so a baseline can be promoted on its other checks alone.
- An interrupted benchmark is rerun after the holdout result is known. That is safe only because benchmarks never depend on the studied strategy; a future benchmark that did would break this.
- Slicing a continuous candle run means a position can be open across a fold cut, and the out of sample slice's first return is measured from the in sample day's close. That is correct for one continuous series, but the fold isn't a cold start.
- Any change to a `trading_core` file in the placeholder's import closure (including `recording/`) gives it a new `frozen_hash`, so that deploy needs a manual paper promotion first. AC-26 turns that from a rollback into a clean refusal, but the promotion step stays manual on purpose.
- Taker and maker studies of one strategy are separate trials with separate holdouts (AC-25), which raises N for that strategy and so lowers its deflated Sharpe.
- The paper trader runs one strategy at a time, so only one version can be gathering paper evidence until multi strategy support lands.
- Replay studies are slow: about 28 minutes per day per grid point, so the full 43 development days take about 10 hours per grid point with 2 workers, and a 4 point grid takes about 40 hours. A grid point stopped partway reruns from its first day.
- The replay holdout starts with only 12 days (one per month) and grows by one a month. That just clears the bootstrap's 10 day floor, so its bounds are wide, and a strategy needs about 17 holdout trades a day to reach the 200 trade floor.
- Our own recorder days (daily since 2026-10-08, with liquidations) never enter a study. They stay for paper parity, so a context feature that needs liquidations can't be studied on replay.
- Development reaches back to 2022, when the market and Kraken's book were different. That is deliberate (more regimes), but the early folds may judge a strategy on conditions that no longer exist.
- The 1st of the month is one fixed calendar day, so anything special about month starts (funding, rebalancing flows) is in every replay day.

**Neutral**:
- A new app (`apps/gate`), a new pure core folder (`trading_core/gate/`), a new role and secret, and a Compose service that only runs when you call it.
- `RunBundle` and the bundle format grow. Old bundles still parse (the new parts are optional), but they can't be promoted.
- The `insufficient` verdict and the `frozen_hash` match change how spec 0002's live gate behaves (AC-17). That is an amendment to spec 0002's AC-15.

## Follow-up

- [ ] `/sync`: root `AGENTS.md` should list `apps/gate` and add `gate/` to core's pure folders; `packages/core/AGENTS.md` should list `versioning.py` and the `gate_app` role; and `apps/gate/AGENTS.md` needs writing.
- [ ] Spec 0002: note on AC-15 that the live gate now matches `frozen_hash` and requires `decided_by = gate` (spec 0015 AC-17).
- [ ] Feature 19: a running drift halt on live (paper or lab drift, reconcile), which this gate only checks once at promotion. (The deploy time promotion check this item used to ask about is now AC-26.)
- [ ] Deferred multi strategy paper trader: needed before more than one version gathers paper evidence at once.
- [ ] Review `holdout_start` once a year in a new thresholds version (v2), as a deliberate commit and never after looking at a result.
- [ ] Features 16 and 17: write each candidate as a study file. Regime gated variants are separate studies, not grid points.
