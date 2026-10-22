# Review, feat/strategy-scoring-promotion-gate (specs 0015 and 0016, as merged on origin/main 0ffc03d), 2026-10-22

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: about 126 files in the range 76cb0a6^..origin/main (spec 0010 fix cc349cd and the spec 0018 docs skipped), reviewed as a feature on main
**Verdict**: Changes requested

## Summary

You added a pure gate in `trading_core/gate`, frozen version hashes, a study and holdout flow in the lab, a bundle importer and `gate` app on the VPS, paper and live guards in `start_run`, a deploy check, and the dashboard pages. The statistics, the verdict precedence, the money handling (Decimal everywhere except the statistics), the UTC handling, and the fail closed guards are in good shape and well tested. The headline problems are about trust: the database and the gate do not enforce that a study has exactly one holdout or that the evidence rows agree with each other, and the deploy promotion check runs the new image's code against the old schema, which can deadlock a release.

## Major

### 🟠 One holdout per study is not enforced, and the gate picks one by accident, `packages/core/src/trading_core/migrations/versions/20261019_0009_gate.py:109`
**Problem**: The only database guard is a partial unique index on `study_runs.frozen_hash` for completed holdouts. Two completed holdout rows in the same study with different frozen hashes (two different grid points) are both allowed. The `lab holdout` command prevents this in normal use, but the import path does not. `CandidateInputs.holdout()` (`gate/inputs.py:127`) then takes the first completed holdout sorted by `grid_point or 0`, and `study_evidence` (`recording/gate_queries.py:183`) does the same, so the gate scores the lowest numbered grid point, not the one the development ranking chose.
**Why it matters**: The one time holdout is the core promise of the feature. A second holdout run (from a bug, a hand edited bundle, or a restored laptop database) would let you try grid points on unseen data and the gate would silently pick whichever sorts first.
**Suggested fix**: Add a partial unique index on `study_runs (study_id)` where role is `holdout` and status is `completed`. In `study_evidence` and `evaluate_candidate`, treat more than one completed holdout as an `insufficient` or hard error instead of taking the first.

### 🟠 The gate trusts the evidence rows without cross checks, `packages/core/src/trading_core/recording/gate_queries.py:102` and `:183`
**Problem**: Several facts the gate relies on are never compared with each other: (1) `study_runs.frozen_hash` is never checked against the frozen hash of the run's own strategy version, and `check_bundle` (`apps/gate/src/gate/importer.py:44`) and `_check_bundle_rows` only compare status; (2) nothing checks that the holdout run is the grid point with the best development ratio (`final_grid_point` exists in the lab only); (3) nothing checks that development runs end before `holdout_start` and the holdout starts at it, so a development run that reached into the holdout dates would feed the walk forward series and the deflated Sharpe unnoticed; (4) the promotion's `frozen_hash` comes from the holdout run's version row, while the unique index and the trial count use `study_runs.frozen_hash`.
**Why it matters**: The gate exists to judge lab evidence independently. Today a version can only slip past it through a lab bug or a hand made bundle, but then nothing catches it, and the paper guard is keyed on exactly this hash.
**Suggested fix**: In `study_evidence` and the importer, require `study_runs.frozen_hash == version.frozen_hash`, require the holdout grid point to equal the development argmax (reuse `final_grid_point`), and require the daily equity days of development runs to be before and of the holdout on or after `holdout_start`. Each mismatch should become an `insufficient` reason or a refused file.

### 🟠 The deploy promotion check runs new code against the old schema, with no way around it, `deploy/host/kfs-deploy:128` (function `promotion_check`) and `apps/gate/src/gate/check_version.py:91`
**Problem**: The check runs the NEW release's `gate check-version` with `--no-deps`, before the new `migrate` has run. `ensure_strategy_version` and `require_paper_promotion` load whole ORM rows from `strategy_versions` and `promotions`. Any release that adds a column to those tables (migration 0009 itself did) makes the new image's query fail against the old database, `check-version` exits 1, and deploy refuses with exit 4. The same happens on the very first deploy that ships the gate: the `gate_app` role does not exist until the new `migrate` runs. The spec's bootstrap note only covers `git_commit`.
**Why it matters**: With "no flag to skip the check", a release that changes those tables cannot be deployed by `kfs-deploy` at all. You would have to run the new migrate by hand first, which is exactly the sort of off script step a deploy tool should avoid.
**Suggested fix**: Either run the new `migrate` (additive migrations only) before the check and before the drain, or make `check-version` read with narrow column selects and run as a role that already exists, or add an explicit, logged, documented override for the bootstrap deploy. Add a deploy test with a stale schema.

## Minor

### 🟡 One malformed bundle value aborts the whole import, `packages/core/src/trading_core/recording/bundle_codec.py:83` and `apps/gate/src/gate/importer.py:79`
**Problem**: `_decode` calls `UUID(...)`, `datetime.fromisoformat(...)`, `date.fromisoformat(...)` and `Decimal(...)` outside the `try` in `_row`. A bad value raises `ValueError` or `decimal.InvalidOperation`, which is not `BundleFormatError`. `import_folder` catches only `OSError, BundleFormatError, BundleRefused, RecordingError, SQLAlchemyError`, so the exception escapes and the files after it are never imported, despite the docstring "never raises for one bad file".
**Why it matters**: The file comes from an untrusted path (a tar over SSH into a root script). One corrupt file should be refused alone.
**Suggested fix**: Wrap each scalar conversion in `_decode` and raise `BundleFormatError` with the field path, and add a test with a bad UUID, a bad date, and a bad decimal.

### 🟡 Resuming a study after editing `thresholds.toml` crashes instead of exiting 64, `apps/lab/src/lab/study.py:452`
**Problem**: `plan_hash` does not cover the thresholds, but `record_study` compares the whole row. A resumed `lab study` with a changed `thresholds.toml` but the same plan raises `RecordConflictError`, which `lab/main.py` never catches.
**Why it matters**: The spec says editing the file never changes an existing study. The behavior is right (it refuses), but you get a traceback instead of the documented exit 64 message.
**Suggested fix**: Compare `thresholds_hash` next to `plan_hash` in `run_study` and refuse with a clear message.

### 🟡 `decided_at` is taken before the lock or the prompt, `apps/gate/src/gate/main.py:96` and `:157`, `apps/gate/src/gate/evaluate.py:52`
**Problem**: `now` is read when the command starts. `evaluate` then waits for the study's advisory lock, and `promote --manual` waits for you to type the name. The row gets the earlier time. "Latest promotion" is decided by `decided_at`, so an evaluation that finished while another waited can end up older than the one that committed first. Paper evidence also uses `started_at > decided_at`.
**Why it matters**: Ordering errors here mostly fail closed, but they can make an older pass look newer than a fail.
**Suggested fix**: Take the time after the lock is held and after the prompt is confirmed.

### 🟡 A second study of the same version can revoke its paper pass, `packages/core/src/trading_core/recording/gate_queries.py:188`
**Problem**: With no holdout, `study_evidence` falls back to the first completed development run and `gate evaluate` writes an `insufficient` candidate to paper promotion against that run's frozen hash. If another study already passed that same hash, the new row is the latest non pass promotion and takes the version out of paper at the next start.
**Why it matters**: It fails closed, but it is a surprising way to lose a pass. The second study can never spend its own holdout (the unique index refuses it).
**Suggested fix**: Do not write a promotion for a study with no completed holdout, or write it without a `frozen_hash` that can match another study's.

### 🟡 Paper trade returns and the lab bound may not use the same definition, `packages/core/src/trading_core/recording/gate_queries.py:392` against `:96`
**Problem**: The lab side (`holdout_trades`) uses `realized_pnl + funding_total` per trade. The paper side feeds `realized_pnl` alone as the trade event and counts funding as separate events, so the per trade return in `paper_record` leaves funding out. Please check whether paper positions carry funding in `realized_pnl`.
**Why it matters**: `paper_lab_band` compares the two means. A small systematic difference shifts that check.
**Suggested fix**: Build the paper trade return the same way (realized plus the position's funding), or document why they match.

### 🟡 The code closure has known blind spots, `packages/core/src/trading_core/versioning.py:80`
**Problem**: The hash covers `.py` files reached by `import` nodes. Data files read at runtime, string based imports (config paths, `getattr(builtins, "__import__")`), and third party package versions other than NautilusTrader do not change it. `if TYPE_CHECKING` imports are included, which widens the closure for no reason.
**Why it matters**: A change in one of those places would keep the same frozen hash and so keep the paper pass.
**Suggested fix**: State these limits in the module docstring and the spec, and skip `TYPE_CHECKING` blocks.

### 🟡 Several hot paths are slower than they need to be, `packages/core/src/trading_core/lab/daily.py:76`, `gate/stats.py:106`, `recording/gate_queries.py:70` and `:487`
**Problem**: `_sums` and `realized_daily` scan every position for every day (days times positions). The trade return bootstrap does 10,000 resamples of `n` Python steps, so a long scalping holdout with thousands of trades takes minutes. `_funding_missing` runs for every run though only the holdout uses it. `study_detail` ranks every study to find one. The dashboard strategies page recomputes paper progress on every request.
**Why it matters**: None of it is wrong, but the gate runs in a 512 MB container and the dashboard on a small VPS.
**Suggested fix**: Bucket positions by day once, cap or vectorize the trade bootstrap, compute funding flags only for the holdout, and query one study directly.

### 🟡 Migration grants and downgrade are a little loose, `packages/core/src/trading_core/migrations/versions/20261019_0009_gate.py:221` and `:228`
**Problem**: `GRANT SELECT ON ALL TABLES ... TO gate_app` covers only tables that exist today, and no default privilege is set, so a future table will silently be unreadable by the gate. The downgrade revokes `lab_app` SELECT on `promotions` and `promotion_evidence`, which may have existed before this migration.
**Why it matters**: A later migration will fail at runtime in the gate instead of at deploy, and a downgrade can remove a pre existing grant.
**Suggested fix**: Add `ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON TABLES TO gate_app`, and revoke only what this migration granted.

### 🟡 The receive script runs `tar` as root on a stream with no limits, `deploy/host/kfs-bundles:25`
**Problem**: Extraction is filtered to `*.bundle.json` and kept in a temp folder, and only regular files are installed, which is good. There is no size cap, no check for link members, and the stream is not limited, so a bad or oversized archive can fill the disk of the VPS state folder.
**Why it matters**: The sender is your own laptop over Tailscale, so the risk is low, but the script runs as root.
**Suggested fix**: Cap the input size (for example with `head -c`), reject non regular members with `tar --no-same-owner --warning=error` plus a listing check, and check free space first.

## Nits

- ⚪ `packages/core/src/trading_core/gate/checks.py:67`, `text_of` is public but has no docstring (AGENTS.md asks for one on every public function in core).
- ⚪ `packages/core/src/trading_core/gate/checks.py:82`, `:166` and `apps/lab/src/lab/study.py:598`, bare `assert` guards in production code vanish under `python -O`; raise a typed error instead.
- ⚪ `apps/dashboard/src/dashboard/web/page_routes.py:~195`, when the promotion log read fails it renders the strategies page with an error, not the log page.
- ⚪ `packages/core/src/trading_core/recording/bundle_codec.py:191`, `BundleFile.rows` rereads the whole file once per row list (about 14 passes over a file that can be hundreds of MB); fine today, worth one pass later.

## Strengths

- The statistics are careful and deterministic: seeded bootstrap, `math.fsum` everywhere, CSCV and the deflated Sharpe follow the published formulas, and floats are confined to `gate/series.py` and `gate/stats.py` with Decimal in and out.
- Verdict precedence (fail, then insufficient, then pass) is simple, central (`decide`), and every missing input turns into an `insufficient` reason rather than a pass. The paper floors correctly stay below a failed check.
- The frozen hash design is sound: the `ast` based closure, the `DynamicImportError` refusal, the NautilusTrader version in the hash, and the entry mode in the version config (AC-25) all close real holes.
- The guards fail closed in the right places: `require_paper_promotion` and `_require_live_gate` key on the frozen hash and the gate as decider, manual promotion to live is blocked in code and by a CHECK, and the roles get exactly the grants the spec lists.
- Bundle import streams with `ijson`, rejects floats for money, re derives `_at` fields, and is idempotent through the insert and compare helpers; the advisory lock around holdout and evaluate is correct.
- `trading_core.gate` is truly pure and a boundary test enforces it.

## Test coverage

Core coverage is strong: unit tests exist for folds, series, stats, thresholds, verdict, paper, versioning, daily, study days, and the bundle codec, plus integration tests for roles, study bundles, paper evidence, and the gate app (import, evaluate twice, manual promotion, check-version). The lab has a full candle study run with resume and a spent holdout, and the deploy check has stub based shell tests.

Gaps: no test for the replay holdout path (`_replay_holdout`, `_resume_benchmarks`, the benchmark day limit), none for `last_complete_day`, none for a bundle with a malformed UUID, date, or decimal value (finding above), none for two holdouts in one study or for mismatched `study_runs` and version hashes (the first two majors), and none for the deploy check against an old schema. The pure statistics tests are mostly hand worked cases; one test against a published deflated Sharpe or PBO example would add confidence.
