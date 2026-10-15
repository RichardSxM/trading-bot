# Review, feat/leverage-liquidation-guard (spec 0011, merged range 073a7e4..2cd1767), 2026-10-15

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 99 files (excluding uv.lock), range review of main (073a7e4 to 2cd1767)
**Verdict**: Approve with nits

## Summary
The change turns `check_entry` into the single guard (risk sizing, three caps, liquidation estimate, funding term), books demo funding from the `RecordingActor`, folds it into the loss limits and the dashboard, and wires config through `GuardedConfig`. The pure maths in `risk/` matches the spec formulas (AC-1, 2, 5, 7, 9, 11, 15) and keeps to the Decimal-only, no-I/O rules. I found no blocker or major defect. What remains is a handful of coverage gaps on the funding booking branches, one dead seed, one dashboard/loss-limit day mismatch, and the integration test command in `packages/core/AGENTS.md`, which fails as written.

## Minor
### 🟡 Documented integration test command fails as written, `packages/core/AGENTS.md:37`
**Problem**: The line `docker compose run --rm -T migrate pytest -q -m integration` has no path (the same text is in the docstring of `packages/core/tests/integration/conftest.py:4`). Without a path pytest collects all of `tests/`, including modules that import pyarrow and Nautilus, which the `migrate` image (built from `trading-core` without the `node` extra) does not have. Collection errors out before `-m integration` can deselect anything. Not run here (no Docker access); this is judged from `compose.yaml` (the `migrate` service builds `PACKAGE: trading-core`) and the `node` extra in `packages/core/pyproject.toml:20`, and it matches the report you gave me.
**Why it matters**: The documented gate for integration tests (spec 0011 AC-14 and AC-16 rely on them) cannot be run by following the docs.
**Suggested fix**: Document `docker compose run --rm -T migrate pytest -q -m integration tests/integration` in both places, or make the root conftest ignore non integration modules when the node extra is absent.

### 🟡 Funding booking branches without a test, `packages/core/src/trading_core/node/recording.py:487` (`_book_funding`) and `:468` (`_on_funding_alert`)
**Problem**: `tests/test_node_funding.py` covers the booking, re-arm, open/close at the boundary, no rate, and rate too old. Not covered: a stale or missing quote at the boundary (`fresh` false, `quote_missing` in the `funding_unbooked` details), the `funding_position_unknown` skip (no row id for the position), the early wake branch of `_on_funding_alert` (`now < boundary`, only the placeholder has a test for its own `_when_due`), and a partial fill straddling the boundary through `qty_at`.
**Why it matters**: These are the branches that decide whether money is booked or silently skipped. The spec says the placeholder never crosses an hour, so no VPS run exercises them either (verify.md step 4).
**Suggested fix**: Add engine level cases for each of the four.

### 🟡 Funding ledger seed is always zero, `apps/trader/src/trader/main.py:342`
**Problem**: `FundingLedger(run_funding_total(session, run_id))` reads the sum for a run that `start_run` created one line above, so it has no payments. Each trader start is a new run, and the demo sandbox balance is also reset at start (demo cache purge), so the zero is consistent, but the query and `queries.run_funding_total` do nothing in this flow.
**Why it matters**: It looks like it protects equity across restarts (AC-17 wording) and does not. A future change that reuses a run id would silently start depending on it.
**Suggested fix**: Either drop the seed and say so in AC-17, or add a comment that it is zero by construction today and only matters if a run is ever resumed.

### 🟡 Dashboard "today" disagrees with the loss limits' "today", `apps/dashboard/src/dashboard/data/pnl.py:49` and `:81`
**Problem**: Overview P&L today sums `realized_pnl + funding_total` of positions by `closed_at` and adds the open positions' whole `funding_total`. The loss limits (AC-16) count each payment on its `paid_at` day. A position opened yesterday 23:00 and closed today 00:20 shows yesterday's 00:00 funding as today's loss on the dashboard but not in the loss limits, and an open position carries all its funding into "today" even if it was paid yesterday.
**Why it matters**: Two screens that should agree on the day's loss can differ by one funding payment around midnight UTC. Small for the placeholder, larger once strategies hold for hours.
**Suggested fix**: Sum funding for the today figure from `funding_payments.paid_at`, as the seed does, or note the approximation in AC-18.

### 🟡 No upper bound on the fee the guard sizes with, `packages/core/src/trading_core/settings.py:147`
**Problem**: `sandbox_taker_fee` is `ge=0, lt=1`. A typo such as `0.5` passes validation and makes `loss_per_unit` huge, so every entry sizes to `zero_quantity` and only the decisions table shows why. The spec's Follow-up already asks for a sanity cap for the live source.
**Why it matters**: Fail closed, so safe, but it is a silent stall in demo and the same pattern will be copied for the live fee.
**Suggested fix**: Cap it at a realistic ceiling (for example 0.01) now, in the same validator style as the other `RISK_*` settings.

## Nits
- ⚪ `apps/lab/src/lab/actors.py:55`, `LabRecorder` passes dummy `stale_quote_s` and an empty `FundingLedger` to a base class it does not use for those; a smell that the base class carries paper only wiring (snapshots, funding) the lab then switches off.
- ⚪ `docs/specs/0011-leverage-liquidation-guard/index.md:4`, status is still "In Progress" although the feature is verified and merged, and the Follow-up items for spec 0002's sign note and spec 0006's "before funding" note are unticked.
- ⚪ `packages/core/src/trading_core/node/random_taker.py:178`, `entry_quantity(...)` can raise `ValueError` inside the `try` that only catches `InvalidRiskInput`; it was equally unguarded before, but the try now invites the assumption it is covered.
- ⚪ `packages/core/src/trading_core/node/recording.py:272`, `_RATES_KEPT = 8` is a magic number tied to an assumption about how fast `FundingRateUpdate`s arrive around a boundary; a one line why in the comment would help.

## Strengths
- The pure guard is clean: one function, Decimal throughout, the refusal order of AC-11 is a single readable path, `GuardFigures` records everything AC-12 asks for, and `InvalidRiskInput` fails closed.
- The funding booking is idempotent by design (row built once, spool replays the same row, `funding_total` bumped only when `record_funding` reports an insert, upsert leaves the column alone), and the re-arm bug (#42) was fixed with the cancel before set pattern plus a regression test.
- The fee source fix (#41) removed a real divergence by making `GuardedConfig.taker_fee` the one value feeding both the guard and the fills, and the live path refuses rather than sizing with Kraken's published 0.
- verify.md is honest about what was and was not proven on the VPS (funding is engine level only).

## Test coverage
Strong for the pure logic: `test_risk.py`, `test_risk_funding.py`, `test_risk_limits.py`, `test_node_guard.py` (cache readers), `test_settings.py` (AC-19), `test_node_funding.py` (engine level booking), and real Postgres integration tests for `record_funding`, `funding_total`, and the loss seed. Gaps are the four booking branches listed above; nothing in the suites runs a real funding rate through the Kraken data client into the `RecordingActor`, so that live path is unproven (the strategy already subscribes the same feed, which lowers the risk). I did not run the suites (Docker); the reported counts (core 846, trader 73, dashboard 510, lab 89, integration 102) are taken as given.
