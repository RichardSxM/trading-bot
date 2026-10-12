# Review, feat/market-context-feed, 2026-10-12

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 56 files, branch vs main (merge base 93b4806)
**Verdict**: Changes requested

## Summary
Spec 0009 end to end: native mark/index/funding recording, a raw Kraken websocket actor for open interest and liquidations, compactor conversion with silence/reconnect/recorder_down gaps and cross-hour uid dedupe, a pure `ContextBook` read the same live and in replay, own-data funding in the lab, and Tardis context import. The design is clean and follows the spec and the core/node/catalog boundaries closely; I found no correctness blocker. The headline issue is that the Nautilus-facing `ContextFeedActor` and the trader/recorder wiring have no test at all, while all branching around precision, the instrument cache, and publishing lives there.

## Major
### 🟠 `ContextFeedActor` and the process wiring are untested, `packages/core/src/trading_core/node/context_feed.py:223-280`
**Problem**: `test_node_context_feed.py` drives only `ContextFeed` with a fake `emit`. `ContextFeedActor._publish` (unknown_product / instrument_not_loaded skips, `make_qty`/`make_price` at instrument precision, `publish_data` with `context_data_type`), `on_start`/`on_stop`, the recorder's `outages_path` lambda (`apps/recorder/src/recorder/main.py:142`) and the trader's `placeholder_config`/actor wiring (`apps/trader/src/trader/main.py`) are not referenced by any test.
**Why it matters**: AC-3 (instrument-precision `Quantity`, instrument_not_loaded skip, the exact `DataType` metadata the strategy and backtest subscribe with) and AC-12 are only checked by the gate, not by regression tests. A wrong run folder in the outages lambda would silently lose every `reconnect` gap.
**Suggested fix**: Add a test that builds the actor against a real Nautilus cache/msgbus (or calls `_publish` with a stub cache) and asserts the published object, precision, and both skip reasons; add a small test that the recorder's outages path equals the streaming writer's run folder, and that `placeholder_config` sets `use_market_context`.

## Minor
### 🟡 A skipped liquidation is remembered as seen, `packages/core/src/trading_core/node/context_feed.py:169-173`
**Problem**: `RecentIds.add` runs before `emit`. If `emit` returns `instrument_not_loaded` (likely right at startup, when the 100 row `trade_snapshot` arrives), the uid is already remembered, so a later resend (reconnect snapshot) is dropped too.
**Why it matters**: A real liquidation can be lost permanently for the run. Open interest has no such problem.
**Suggested fix**: Remember the uid only after `emit` returns `None`.

### 🟡 `--context` backfill deletes before it downloads, `apps/lab/src/lab/tardis_import.py:~190`
**Problem**: `import_context_day` removes the day's funding/mark/index/OI hour files, then downloads. A failed download leaves a done day (day manifest present) with no `funding_rate_update`.
**Why it matters**: A replay of that day raises `FundingRateMissingError` until the command is rerun (loud, recoverable, laptop only).
**Suggested fix**: Download first, then remove and rewrite.

### 🟡 Context rows are merged and warmed for every replay, `apps/lab/src/lab/stream.py:181-195,226-245` and `units.py:~344`
**Problem**: `stream.warmup()` reads up to two hours of five tables and `_hour_events` merges all context rows even when `use_market_context` is false.
**Why it matters**: Extra IO and engine events (and changed per hour event counts) for runs that never read the context.
**Suggested fix**: Skip both when the strategy config has `use_market_context` off, or note the changed counts in the run notes.

### 🟡 Day manifest can close before a context stream's hours are counted, `apps/recorder/src/recorder/compactor/days.py:36-60` (low confidence)
**Problem**: `close_days` covers a day by `order_book_deltas` hours only; `_rewrite_incomplete_days` rewrites a closed manifest only for late rows. Context hours or `reconnect` gaps committed after the day closed are not in the manifest.
**Why it matters**: Per stream row counts and gap seconds (AC-7) could undercount in rare timing. Same pass conversion likely hides it.
**Suggested fix**: Either require the context streams' hours before closing, or rewrite the manifest when later gap or hour commits touch a closed day; add a test.

### 🟡 catalog imports node and node imports catalog, `packages/core/src/trading_core/catalog/layout.py:16` and `node/context_types.py:20`
**Problem**: `catalog.layout` imports `node.context_types`, which imports `catalog.context`. It works because `catalog.context` does not import `layout`.
**Why it matters**: A cycle waiting for the next import added to either side.
**Suggested fix**: Put the two table name strings in `layout.py` as constants and keep a test that they equal `class_to_filename(...)`.

### 🟡 Resolved overlaps drop the weaker gap whole, `packages/core/src/trading_core/marketdata/gaps.py:120-140`
**Problem**: A `silence` gap that only partly overlaps a `reconnect` is removed entirely, hiding its non overlapping time from the gap file and manifest seconds.
**Why it matters**: Gap seconds are understated in that case. This matches the spec wording, so it is a design note more than a bug.
**Suggested fix**: Document it in the spec, or trim instead of drop.

## Nits
- ⚪ `packages/core/src/trading_core/node/context_feed.py:55`, `type Connect = Callable[..., Any]` is a bare `Any`; same for `table: Any` in `catalog/context.py`. Repo rule says no bare Any; add a narrow Protocol or comment.
- ⚪ `packages/core/src/trading_core/marketdata/gaps.py:48`, `Gap` lost `order=True` and defines only `__lt__`; `>`/`<=` now fail. Use `functools.total_ordering` or note it.
- ⚪ `apps/lab/src/lab/actors.py:124`, docstring still says funding samples are "none on own runs".
- ⚪ `apps/lab/src/lab/parity.py:143`, funding is still tied to `run.source == "tardis"`; fine for paper parity, but a comment saying why would help given AC-14 removed that tie elsewhere.
- ⚪ `packages/core/src/trading_core/node/strategy.py:~210`, `market_context()` fallback calls `snapshot(exc.latest_ns)` on any `LiveClock`; fine, but the log carries `behind_ns` only, not the code path.

## Strengths
- `ContextBook` is small, pure, and honestly look-ahead safe: pruning only on apply, fixed `Decimal` context for basis, `known` fails closed, and the replay-equals-book test pins AC-13.
- `ContextFeed` isolates every failure: parse errors counted once a minute, handler errors swallowed and logged, outage lines appended with fsync, backoff and reset tested with fake connect/sleep.
- Compactor changes keep the single commit point: dedupe uids and silence state live in `done.json`, outage ingestion is idempotent by line count and gap dedupe key.
- Old gap files and manifests stay readable (`stream` defaults to `order_book_deltas`) and the lab planner only reads book gaps, so context gaps cannot split replay segments.

## Test coverage
Strong for the pure core (`ContextBook`, parsing, gaps, settings), the compactor (rerun identity, dedupe, gaps), lab stream merge/warmup/look-ahead equality, own funding all-or-nothing, runfile `[context]`, and Tardis import/backfill. Gaps: `ContextFeedActor` publishing and lifecycle, recorder and trader wiring (outages path, `placeholder_config`, trader actor), `SilenceDetector` edge at run boundaries beyond the compactor test, and day-closure ordering for context streams.
