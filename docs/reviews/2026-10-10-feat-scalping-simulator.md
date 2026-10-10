# Review, feat/scalping-simulator, 2026-10-10

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 124 files changed (about 45 read in full or by diff, the rest skimmed), branch vs main (merge base b40d208)
**Verdict**: Changes requested

## Summary

Spec 0008 adds a laptop-only replay lab: a Nautilus backtest per clean segment, a spawn-based worker pool, a pure `trading_core/lab` scoring layer, a least-privilege `lab_app` role, and bundle writes through the spec 0002 repository. It also carries the unmerged spec 0007 sequence-hole watch and compactor refactor. The structure is sound and fails closed in the right places: live is refused at the entry point, the engine is a `BacktestEngine` with no Kraken adapter (test-pinned), and `lab_app` has SELECT/INSERT on exactly the bundle tables. One major issue (provenance: `git_commit` can be `"unknown"`) plus several minors. The known items from verify and debug are triaged below: (a) is fixed and complete, (b) and (c) are minor, (d) is not a defect, (e) to (i) are minor or nits.

## Major

### 🟠 Lab accepts `git_commit = "unknown"`, so strategy versions silently collapse across code revisions, `apps/lab/src/lab/replay.py:721`
**Problem**: `replay()` passes `settings.git_commit` straight into `ensure_strategy_version` and `run_config.git_commit` (line 251). `compose.yaml` builds with `GIT_COMMIT: ${GIT_COMMIT:-unknown}`, and the lab service has no guard, so a plain `docker compose build` yields `"unknown"`. `Settings` notes that live refuses `unknown`, but the lab (which is the evidence source for feature 15 comparisons) does not. This is known item (f).
**Why it matters**: `version_hash = hash(git_commit, config)`. Every replay built without `GIT_COMMIT`, across any number of code changes, lands on the same `strategy_versions` row. Two runs that used different simulator or strategy code look identical in Postgres and in `run_config`. That breaks the "same inputs, same outputs" and traceability intent of AC-11 and AC-12, and it fails open rather than closed.
**Suggested fix**: Refuse to start `replay` and `parity` (exit 78, same family as the live refusal) when `git_commit` is `unknown`, or have the compose build derive it. Add a test that `unknown` is refused. Document `GIT_COMMIT=$(git rev-parse HEAD) docker compose build lab` in the lab commands.

## Minor

### 🟡 Fee test tolerates ±0.01 while the AC and verify say exact, `apps/lab/tests/test_replay.py:131`
**Problem**: Known item (b). Nautilus computes the commission in float inside Rust and rounds to the USD precision; the test rebuilds it with `Decimal` half-even. A value near a half cent (1.00 vs 0.99 at taker_fee 0.001) differs by one cent, so the test was loosened. `settle_at_mid` (`units.py:199`) uses `Decimal` half-even, so segment-end fills and engine fills can round differently.
**Why it matters**: The spec and verify.md promise "exactly `taker_fee × quantity × price` rounded to cents". The code cannot honour that for engine fills, and the loose test hides how often it differs.
**Suggested fix**: Decide the contract: either state "within one cent (engine float rounding)" in AC-5 and verify.md, or recompute the fee in the recorder from Decimal. Count and assert how many fills differ rather than allowing any.

### 🟡 Funding amounts are unrounded, so `net_pnl` has long decimals, `packages/core/src/trading_core/lab/funding.py:99`
**Problem**: Known item (c). `-signed_qty * mid * rate` is stored with full Decimal precision in `funding_payments` and summed into `net_pnl`. Trade P&L and fees are cents.
**Why it matters**: Reports mix 2-dp and 20-dp figures, and the Postgres `numeric` stores noise digits. It is not a correctness bug.
**Suggested fix**: Quantise each payment to the currency precision (USD cents, or whatever Kraken books) when building the `FundingRow`, and say which rounding in the spec.

### 🟡 `final_ts` can precede trades after the last delta, `apps/lab/src/lab/units.py:304`
**Problem**: `data_end` is the last `ts_init` of the last hour's deltas file only. The stream also yields that hour's trades. A trade later than `data_end + latency + 1 ms` (for `latency=0` that is 1 ms) would be fed before the final synthetic batch with a larger timestamp, and `engine.run(end=final_ts)` would cut or misorder it. I could not confirm this occurs in the real data; the unit test does not cover a trailing trade.
**Why it matters**: Out-of-order data in a backtest engine is either an error (variant fails) or a silently dropped event.
**Suggested fix**: Take `data_end` as the max of the last delta and the last trade `ts_init` of the final hour (and have `_final_batch` compare against it), and add a test with a trade after the last delta.

### 🟡 `prune` swallows errors and reports bytes it may not have freed, `apps/lab/src/lab/tardis_import.py:551`
**Problem**: `shutil.rmtree(root, ignore_errors=True)` then logs and prints `freed` computed before deletion. A permission or busy-file failure is invisible. It also deletes with no confirmation and no check for a running replay.
**Why it matters**: AGENTS.md says never swallow an exception; the cap check (AC-2) trusts `prune` to free space.
**Suggested fix**: Use an `onexc` handler that records failures, recompute `tree_bytes` after, and exit non zero when something stayed.

### 🟡 Bare `Any` in the importer, `apps/lab/src/lab/tardis_import.py:268`
**Problem**: `HourFiles.open(schema: Any)`, `write(rows: Any)`, `by_hour`, `in_order`, `_arrow`, and `DeltaWriter._schema: Any` are bare `Any`. AGENTS.md forbids it; pyarrow types (`pa.Schema`, `pa.Table`) exist.
**Suggested fix**: Type these as `pa.Schema` and `pa.Table`; use a narrow `# type: ignore[code]` with a reason where a stub gap remains.

### 🟡 Variants share one `started_at`, `apps/lab/src/lab/replay.py:622`
**Problem**: Known item (e). Every variant's `started_at` is the batch start although later variants queue behind earlier ones; `ended_at` is per variant. Durations on later variants are overstated.
**Suggested fix**: Record each variant's first unit start in the pool outcome, or document `started_at` as the batch start in the spec and report.

### 🟡 Interrupt discards variants that already finished, `apps/lab/src/lab/replay.py:672`
**Problem**: Variants resolved out of order but not yet written (an earlier variant still running) get an `interrupted` report and no bundle. Hours of compute are lost, and the write order rule is the cause.
**Why it matters**: Documented in the module docstring, so not a bug, but costly at 3,868 s per unit. Consider writing resolved variants during the shutdown (they are complete) if the order rule can be relaxed.

### 🟡 The segment end close pauses the gate, which AC-7 says is never paused, `apps/lab/src/lab/actors.py:237`
**Problem**: Known item (g). `pause_in_memory()` at `data_end` makes any later entry decision a recorded `paused` rejection. AC-7 says the gate is "loaded and not paused". These rows then show up in `decisions` (and in parity's excluded boundaries).
**Suggested fix**: Amend AC-7/AC-8 to say the gate pauses at the segment end, or suppress entry decisions after `close_at_ns` instead.

### 🟡 `lab_app` migration depends on `db_roles` having run, `packages/core/src/trading_core/migrations/versions/20261008_0005_lab.py:75`
**Problem**: `GRANT ... TO lab_app` fails with "role does not exist" on any database where `db_roles` has not run first (the CI or a hand run of `alembic upgrade head`). The docstring says so; the migration does not check. The core integration conftest was updated, so I treat this as acceptable, but it is a trap for the next person.
**Suggested fix**: Guard the grant with a `pg_roles` existence check, or fail with a clear message.

### 🟡 Latency variant beating base is unexplained, `apps/lab/runs/maker_probe.toml`
**Problem**: Known item (d), not a code defect. In 1.231.0 latency applies strategy to venue only, so `latency=4x` delays both a post only order reaching the book and its cancel. On a thin maker probe that can reduce adverse selection and raise net P&L by chance or by a real effect. The report prints no uncertainty.
**Suggested fix**: Print the fill count and a standard error beside each variant's net P&L and markouts, and say in the report that a single sample difference is not a result. No change to the model is needed unless a unit test shows the cancel latency is wrong.

## Nits

- ⚪ `docs/specs/0008-scalping-simulator/verify.md`, the roles test runs in `migrate`, not `trader` (known item i); the `instruments` table has no fee columns, so the "row still shows taker_fee 0.0005" check needs a different source (the pinned JSON).
- ⚪ `docs/specs/0008-scalping-simulator/index.md` AC-1 says `gaps/<day>.parquet` is written; `write_gaps` writes nothing for a zero gap day and `read_gaps` already tolerates that (known item h). Reword the AC.
- ⚪ `apps/lab/src/lab/replay.py:162` the `@<run_id>` suffix makes `event_key` non deterministic across reruns; fine, but note it in the spec since AC-12 says "identical rows apart from ids".
- ⚪ `apps/lab/src/lab/replay.py:181` `_band_trades` and several private helpers have no docstring (not required outside core).
- ⚪ `apps/lab/src/lab/main.py:93` `configure_logging(env=os.environ.get("TRADING_ENV", ""))` passes a raw env var before settings validation; harmless because `_refuse_live` already ran.

## Strengths

- Known item (a) is fully resolved: the lab writes only two env wide unique keys, `risk_events (env, event_key)` and the `kraken_log` partial index on funding (the lab writes `computed`, which is run scoped). All other unique constraints are per run (`orders`, `fills`, `positions`, `funding computed`). `_scoped` covers the first, and `_check_bundle_rows` was extended to risk events, so a foreign run id cannot slip in.
- Safety is layered and tested: `_refuse_live` before argument parsing, compose pins `TRADING_ENV=demo`, rows are hard coded `Env.DEMO`, the process imports no Kraken adapter (`test_safety.py`), and `lab_app` privileges are asserted both ways in `test_roles.py` (UPDATE, DELETE and `control_commands` refused).
- The worker pool is carefully built: spawn context, parent reads the pipe before joining, EOF read as `WorkerDiedError`, variant scoped failure, and a signal shield so a bundle and its report land together.
- The `trading_core/lab` layer is pure with no I/O or Nautilus imports, small, documented, and unit tested per module (loser band, markouts, fill stats, funding, segments, variants, parity).
- Sequence tracker (0007) is compact and handles the hard cases (split holes, late arrivals, fail safe tail); `_fill` cannot index past the runs.

## Test coverage

Strong at the pure layer (568 core tests, a test file per `lab` module) and at the unit and failure paths (67 lab tests: determinism, failure isolation, pool, stream, tardis import, parity, run file). Gaps: only one Postgres backed test exercises the real `PostgresStore`/`lab_app` path; no test for `git_commit == "unknown"` handling (the finding above); no test for a trade after the last delta (units.py:304); no test for `prune` failure; the fee test's tolerance weakens the exact fee AC. Recorder changes carry their own tests (104 pass).
