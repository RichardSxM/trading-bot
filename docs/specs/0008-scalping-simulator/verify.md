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
