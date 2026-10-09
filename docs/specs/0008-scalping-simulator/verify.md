# Verify: scalping simulator · spec 0008 · written 2026-10-08
_Steps derived from spec 0008 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. Everything runs on the laptop; bring the stack down afterwards._

## Commands
- [ ] `docker compose build` then `docker compose run --rm --no-deps -T lab pytest -q` → all pass → AC-3 to AC-15, AC-17, AC-18
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_lab_*.py` → all pass (segments, markouts, fill stats, funding, metrics, loser band, parity, variants) → AC-4, AC-6, AC-9, AC-10, AC-14, AC-15
- [ ] `docker compose run --rm --no-deps -T recorder pytest -q` → all pass after `BookCarry` and `HourSink` moved to `trading_core.catalog` → AC-1
- [ ] `scripts/typecheck.sh` → clean
- [ ] `docker compose run --rm migrate` → `run_fill_stats` exists; `\dp run_fill_stats` shows `lab_app` with `ar` (SELECT, INSERT) only → AC-11, AC-17
- [ ] `docker compose config lab` → `profiles: [lab]`, `mem_limit: 3g`, mounts `catalog-lab` and `lab-reports`, secret `postgres_lab_password` only, `TRADING_ENV: demo` → AC-16, AC-17

## Real data
- [ ] On the VPS after deploy: `docker compose run --rm migrate` passes without `postgres_lab_password`, and `\du lab_app` shows `Cannot login` → AC-17
- [ ] `docker compose run --rm --no-deps lab import-tardis 2026-09-01 2026-09-01` → `catalog-lab/tardis/manifests/2026-09-01.json` exists, 24 delta hours each starting with snapshot rows, `.raw/2026-09-01/` gone → AC-1
- [ ] The same command again → `lab_import_day_skipped`, no file changes → AC-1
- [ ] `lab import-tardis 2026-10-01 2026-10-01` → `lab_import_day_done` shows `snapshot_diffs` near 51, no `CLEAR` row after any hour's start, a `reconnect` gap only around the 924 s outage; a replay's `lab_segment_planned` keeps about 22 of 24 hours → AC-1, AC-4
- [ ] `lab import-tardis 2026-09-02 2026-09-02` → exit 64 before any download → AC-1
- [ ] `LAB_CATALOG_CAP_GB=1 lab import-tardis 2026-08-01 2026-08-01` → exit 3, `lab_catalog_full` → AC-2
- [ ] `lab replay apps/lab/runs/random_taker.toml` on 2026-09-01 → total net P&L negative, report shows expected loss, band, and `pass`; `select mode, env, status from strategy_runs order by started_at desc limit 1` → `replay | demo | completed` → AC-11, AC-14
- [ ] `select * from run_fill_stats where run_id = '<that run>'` → one `taker` row, `fill_rate` 1, markouts present → AC-10
- [ ] `lab import-tardis 2026-08-01 2026-08-01`, then `lab replay apps/lab/runs/random_taker.toml --from 2026-08-01 --to 2026-09-30` → two days replayed (only the first of month days are required) → AC-3
- [ ] `lab replay apps/lab/runs/random_taker.toml --to 2026-10-01` with 2026-10-01 not imported → exit 2, the missing days listed, no new `strategy_runs` row, no new report folder → AC-3
- [ ] `/usr/bin/time` around one full clean Tardis day of `random_taker` → under 75 min; one full clean own day → under 135 min; `maker_probe.toml --pair-entry` over one Tardis day with `LAB_WORKERS=2` → under 85 min. In every run `lab_unit_done` shows `ru_maxrss` under 1 GB, and `docker inspect` shows `OOMKilled: false` → AC-16

## Variants and maker effects
- [ ] `lab replay apps/lab/runs/maker_probe.toml` on 2026-09-01 with `--pair-entry --queue-bound --sweep-latency` → five variants under one `batch_id`, `index.md` shows `base` and `entry=taker` side by side with fill rate and markouts at 1 s, 5 s, 30 s → AC-6, AC-10, AC-18
- [ ] In that batch, `queue=optimistic` has a fill rate at or above `base`, and `latency=4x` a net P&L at or below `base` (a sanity check, not an assertion) → AC-5, AC-6
- [ ] `random_taker.toml --pair-entry` → exit 64, `entry_mode` missing → AC-6

## Data handling
- [ ] `fetch-catalog.sh --into catalog-lab/own <day with a known gap>` then a replay → the report's skipped hours list that hour with `gap:<kind>`, and the hours on either side form two segments; any position open at the first segment's end shows exit reason `segment_end` → AC-4, AC-8
- [ ] An own source run's report headline says "before funding", and `run_config.funding_source` is `none`; a Tardis run holding across a funding time has a `funding_payments` row with source `computed` → AC-9

## Failure and determinism
- [ ] The same run file twice → `diff` of the two report JSONs, after dropping `run_id`, `batch_id`, `started_at`, `ended_at`, is empty; the same again with `LAB_WORKERS=1` against `LAB_WORKERS=2` → empty → AC-12
- [ ] With `LAB_TEST_STRATEGIES=1`, a batch whose `latency=0` variant runs `raise_probe` (raising in the first hour) → that variant's report says `failed`, no `strategy_runs` row for it, the other variants complete, exit 1 → AC-13
- [ ] During a two variant batch, `docker kill -s KILL` one worker process (find it with `docker top`) → only that unit's variant fails, the other completes → AC-13, AC-16
- [ ] Ctrl-C during a batch → no worker processes left, unfinished variants marked `interrupted` in `index.md`, no bundle for them, exit 130 → AC-13

## Parity
- [ ] `scripts/fetch-paper-decisions.sh <from> <to>` over a period the paper loop ran → CSV in `lab-reports/paper/` with `decision`, `run` (with config hash), `pause`, and `block` rows → AC-15
- [ ] `fetch-catalog.sh --into catalog-lab/own <from> <to>`, then `lab parity apps/lab/runs/placeholder_paper.toml lab-reports/paper/<from>_<to>.csv` → ratio ≥ 0.95, unmatched decisions listed → AC-15

## Safety
- [ ] `docker compose run --rm -e TRADING_ENV=live lab replay apps/lab/runs/random_taker.toml` → exit 78 → AC-17
- [ ] As `lab_app`: `update orders set status = 'filled'` and `insert into control_commands ...` → both `permission denied` → AC-17
- [ ] `grep -rn "KrakenLiveExecClientFactory\|kraken_api" apps/lab/src` → nothing → AC-17

## Afterwards
- [ ] `docker compose down`; `docker compose run --rm lab prune` when the data is no longer needed

## Milestone 2 (thin thread) · added 2026-10-08 by /develop
_The `lab` service's entrypoint is `lab`, so its tests run with `--entrypoint pytest` (the first Commands step above needs that too)._
- [ ] `docker compose --profile lab build lab` then `docker compose run --rm --no-deps -T --entrypoint pytest lab -q` → 29 pass (run file, stream, fixture replay) → AC-3, AC-4, AC-5, AC-7, AC-11, AC-14, AC-17
- [ ] `docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres` → 1 pass: a replay of the fixture writes one `strategy_runs` row as `lab_app` plus `base.md`, `base.json`, `base.bundle.json`, `index.md` → AC-11
- [ ] `docker compose run --rm -T migrate pytest -q -m integration` → all pass, including `lab_app` privileges (SELECT and INSERT on the 13 bundle tables only, `permission denied` on `UPDATE orders`, `DELETE FROM fills`, `INSERT INTO control_commands`, `SELECT control_commands`) and a bundle with fill stats and risk events → AC-11, AC-17
- [ ] `docker compose run --rm -e TRADING_ENV=live --no-deps lab replay /app/apps/lab/runs/random_taker_own.toml` → exit 78 → AC-17
- [ ] `docker compose run --rm --no-deps lab replay /app/apps/lab/runs/random_taker_own.toml --pair-entry` → exit 64, the flag listed as not built yet → AC-3
- [ ] A run file with `maker_fee = 0.0002` (a float) and `latency_ms = -1` → exit 64 listing both problems at once, nothing written → AC-3
- [ ] Real hours: with `catalog-lab/own` holding 2026-10-08 17:00 and 18:00 plus that day's manifest and gap file, `docker compose --profile lab run --rm -T lab replay /app/apps/lab/runs/random_taker_own.toml` → `lab_segment_planned` shows 1 segment, skipped hours split as `gap:reconnect` 4, `gap:recorder_down` 1, `gap:silence` 1, `missing` 16; `lab_hour_replayed` RSS under 1 GB; about 120 closed trades, net P&L negative, loser check `pass` (2026-10-08 with a stand in manifest: 9.89 M events in 603 s, 119 trades, −124.52 USD, band −1.149 to −0.873, mean −1.046, pass) → AC-4, AC-14, AC-16
- [ ] Replace the local stand in manifest with the VPS one (`scripts/fetch-catalog.sh --into catalog-lab/own 2026-10-08 2026-10-08`) and replay again → the same skipped hours unless the real manifest lists incomplete hours for 17:00 or 18:00 → AC-4

### Value sourcing checks (Milestone 2 rows)
- [ ] Fees: in `base.bundle.json`, every fill's `fee` equals `taker_fee × quantity × price` rounded to cents; rerun with `taker_fee = "0.001"` → fees double, the `instruments` row still shows `taker_fee` 0.0005 (the pin) → AC-5, AC-11
- [ ] Latency: every fill's `filled_ns` is at least its order's decision `decided_ns` + 25 ms; with `latency_ms = 100` the gap grows to 100 ms → AC-5
- [ ] Segment id tag: every `client_order_id` contains `-S1-`; a range with two segments shows `-S2-` on the second's orders → AC-7
- [ ] Quantity: each entry's quantity is `notional_usd ÷ mid` floored to 0.0001 BTC (about 0.0124 at 80,600 USD) → *random_taker*
- [ ] Random side: the same run with `--seed 8` changes the sides, and `--seed 7` again repeats the first run's sides exactly → *random_taker*
- [ ] Spread at submit: the loser band uses the quote at each order's creation; set `latency_ms = 0` and the expected value barely moves (spreads come from submit time, not fill time) → AC-14
- [ ] Segments end with the data: no fill's `filled_ns` is after the last delta of the segment's last hour → AC-4
- [ ] `run_config` carries `funding_source = "none"`, `queue_model = "pessimistic"`, the run file's sha256, and `nautilus_version` 1.231.0 → AC-9, AC-11

## Milestone 3 result and Milestone 4 (fill realism and variants) · added 2026-10-09 by /develop
_Task 13 ran 2026-10-09 on Tardis 2026-09-01: 1439 trades, net −1,446.54 USD, expected −1.0099 per trade, band −1.0597 to −0.9601, mean −1.0052, **pass**; the unit took 3,868 s for 40.5 M events with `ru_maxrss` 806 MB. It wrote no funding row: `random_taker` enters on whole minutes and holds 30 s, so it is flat at every hourly boundary. With the segment end close, the Milestone 2 step "no fill's `filled_ns` is after the last delta" now holds for every fill except the `segment_end` close, which fills at the last delta + latency._

### Commands
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_lab_variants.py tests/test_lab_markouts.py tests/test_lab_fill_stats.py` → all pass → AC-6, AC-10
- [ ] `docker compose --profile lab run --rm --no-deps -T --entrypoint pytest lab -q` → 48 pass (adds the segment end close, the rest at the last mid, the final batch, and `maker_probe` with `entry=taker`) → AC-6, AC-8, AC-10, AC-18
- [ ] `docker compose run --rm --no-deps lab replay /app/apps/lab/runs/random_taker.toml --pair-entry` → exit 64, "the strategy has no entry_mode setting" → AC-6
- [ ] `maker_probe.toml` with `entry_mode = "taker"` and `--pair-entry` → exit 64, "already sets entry_mode" → AC-6
- [ ] `random_taker_own.toml --sweep-latency` with `latency_ms = 0` → `lab_segment_planned` lists only `base` → AC-6

### Real data
- [ ] `lab replay /app/apps/lab/runs/maker_probe.toml --pair-entry` on 2026-09-01 → one `batch_id`, `base.md` and `entry-taker.md`; `index.md` shows net P&L, fees, trades, maker and taker fill rate, and markouts at 1 / 5 / 30 s side by side; `base` maker fill rate under 100 %, `entry=taker` taker fill rate 100 % → AC-6, AC-10, AC-18
- [ ] `select liquidity, orders_placed, orders_filled, fill_rate, markout_1s_bps, markout_5s_bps, markout_30s_bps from run_fill_stats where run_id = '<base run>'` → a `maker` row (and a `taker` row only if some post only entry filled as taker) → AC-10
- [ ] A Tardis run with `hold_s = 3600` (copy `random_taker.toml`) → a `funding_payments` row with source `computed` per hour held, and `positions.funding_total` equal to their sum → AC-9
- [ ] In any replay, each segment's last decision with an open position is intent `exit`, reason `segment_end`, its order reduce only and filled; `positions` has no `open` row; `run_config.segment_end_partial` is `false` → AC-8

### Value sourcing checks (Milestone 4 rows)
- [ ] Maker or taker for an entry order: a post only entry that filled is `maker` in its fill's `liquidity`; an unfilled one counts in `orders_placed` of `maker` only → AC-10
- [ ] Entry or exit: exit orders (`time` and `segment_end` decisions) never appear in `run_fill_stats` counts → AC-10
- [ ] Mid at t: a markout uses the last quote at or before `fill_ns + h`; a fill less than 30 s before its segment's last delta is counted in the report's "Left out" column for 30 s → AC-10
- [ ] Segment end close price: the close fills against the last book (its `price` within the last quote's spread); only when the book cannot fill it does a `-rest` fill at the last mid appear → AC-8

### Acceptance criteria coverage (Milestone 4)
- AC-6: variant tests (core), exit 64 steps, the `--sweep-latency` step · AC-8: the segment end tests and the real data step · AC-10: fill stats and markout tests, the `run_fill_stats` query · AC-18: the `--pair-entry` real day batch

## Milestone 5 (hardening, tasks 17, 18, 20) · added 2026-10-09 by /develop
_Task 19 (parity, AC-15) is not built: the paper block windows have no end time in the database, and the run file to paper config hash mapping is unnamed. It is owed to `/architect` first. Its steps above stay open._

### Commands
- [ ] `docker compose --profile lab run --rm --no-deps -T --entrypoint pytest lab -q` → 56 pass (adds `test_determinism`, `test_failure`, `test_pool`, `test_safety`) → AC-12, AC-13, AC-16, AC-17
- [ ] `test_determinism`: the fixture batch with `--queue-bound` under `LAB_WORKERS=1` and `2` (with `base` held back 3 s) gives equal report and bundle JSON once ids and wall clock stamps are removed, and bundles land `base` first → AC-6, AC-12
- [ ] `test_failure`: `raise_probe` (`LAB_TEST_STRATEGIES=1`) with `--pair-entry` fails `base` with `ProbeError`, no bundle, `entry=taker` completes, exit 1; a worker that exits without a result fails only its variant with `WorkerDiedError`; SIGTERM gives exit 130 and `interrupted` reports for both variants → AC-13
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q -m integration tests/integration/test_roles.py` → `lab_app` is refused UPDATE on `orders`, DELETE on `fills`, INSERT and SELECT on `control_commands` → AC-17

### Real data
- [ ] `lab replay /app/apps/lab/runs/maker_probe.toml --pair-entry` (2026-09-01, `LAB_WORKERS=2`) → wall clock under 85 min, every `lab_unit_done` `ru_maxrss` under 1 GB, no OOM kill, both units run side by side (their `lab_hour_replayed` lines interleave) → AC-16
- [ ] Fetch a real own day (`scripts/fetch-catalog.sh --into catalog-lab/own <day> <day>`, replacing the local stand in manifest of 2026-10-08), then `lab replay /app/apps/lab/runs/random_taker_own.toml --from <day> --to <day>` → under 135 min → AC-16
- [ ] Press Ctrl-C during a real batch → exit 130 within seconds, no `lab-*` worker process left (`docker top`), `index.md` marks the unfinished variants `interrupted`, no new `strategy_runs` row for them → AC-13
- [ ] During a `--pair-entry` batch, `kill -9` one worker inside the container → only its variant reports `failed` (`WorkerDiedError`), the other completes, exit 1 → AC-13

### Value sourcing checks (Milestone 5 rows)
- [ ] Worker count: `LAB_WORKERS=1` runs units one at a time (no interleaved `lab_hour_replayed` lines); `LAB_WORKERS=5` refuses to start (1 to 4) → AC-16
- [ ] Units: `lab_segment_planned` `units` equals segments × variants, and each unit logs its own `variant` and `segment` → AC-16
- [ ] Write order: reports and bundles land in variant order (`base` first) whatever order the units finish in; `strategy_runs.started_at` order matches → AC-6

### Acceptance criteria coverage (Milestone 5)
- AC-12: `test_determinism` · AC-13: `test_failure`, `test_pool`, the Ctrl-C and kill steps · AC-16: `test_pool`, the two timings · AC-17: `test_roles`, `test_safety`, exit 78 (`test_runfile`) · AC-15: open (owed to `/architect`)
