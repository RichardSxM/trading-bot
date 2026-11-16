# 0033. trend_vol: the book sized by its own volatility, its sizing history, and the study it owes before live

**Date**: 2026-11-14
**Status**: In Progress

## Summary

`trend_vol` is the 30 day trend book of spec 0032 with one change: the size of the whole book follows the book's own 30 day volatility toward a target (50% a year on the demo, never past 5x), and a coin within 30% of its target is left alone. It was built and put on the VPS demo on 2026-11-11 without a spec; this spec records it as built. It also adds what is needed to judge it honestly: a Postgres history of every rebalance's sizing (with a small dashboard panel), a fast pure replay of the book for research, two cheap rule candidates checked on that replay before anything is built (a smoother volatility estimate, and averaging the book over several daily cut hours), two research ideas borrowed from Jesse (block bootstrap reruns and a benchmark table), an independent second copy of the book math to catch replay bugs (plus Jesse on the laptop for single coin checks), and finally a lab study and gate verdict. Nothing goes live from this spec; that stays with spec 0028.

## Requirements

**User stories**:
- As the trader, I want `trend_vol` written down as it runs, so that every later change is measured against a known rule.
- As the trader, I want each rebalance's book volatility, gross, and per coin targets kept in Postgres and visible on the Portfolio page, so that I can judge the forward test from facts, not log lines.
- As the researcher, I want any improvement checked on a fast replay with a pass rule fixed in advance, so that I build only what beats the current rule in both halves of the data.
- As the trader, I want a lab study and gate verdict on the exact version the demo runs, so that the path to live rests on the gate, not on a good feeling.

**Acceptance criteria**:

*As built (PR #143, merged 957764e; nothing below changes this code)*

- **AC-1** (pure rule, `trading_core/strategy/trend_vol.py`): `BookVolParams` (`book_vol_target` 0.5, `vol_days` 30, `gross_cap` 5, `band` 0.3) refuses a target or cap at or below 0, `vol_days` below 2, or a band outside 0 up to (not at) 1. `unit_book_vol` is the sample standard deviation (divisor n minus 1) of the last `vol_days` daily simple returns of a book holding the unit weights the whole time, newest aligned with newest; a weighted coin with fewer than `vol_days + 1` closes raises `ValueError`. `book_vol_plan` divides the plan's weights by their gross (a book of gross 1), measures it, and scales it to gross `book_vol_target / sqrt(365) / unit_vol`, at most `gross_cap` (`gross_cap` when unit vol is 0); signals and the split between coins are unchanged, `gross_scale` is updated, and a plan with no weight comes back unchanged with `unit_vol` None and gross 0. `within_band` is true only for an open position on the target's side no further from the target than `band` times the target; false for a band of 0, a flat position, a target of 0, or the other side. No I/O, no Nautilus, no float.
- **AC-2** (node, `trading_core/node/trend_vol.py`): `TrendVol(TrendDaily)`, `STRATEGY_NAME = "trend_vol"`, `ORDER_ID_TAG = "TV"`, inherits everything spec 0032 pins (daily 00:05 UTC schedule, orders, flips, `check_rebalance`, held orders, retries, decisions, seed, start and resume rebalances). `_make_plan` rescales the base plan with `book_vol_plan`; when sizing raises `ValueError` the previous plan and plan day are kept, it logs `plan_empty` and that day does not rebalance (never trades the larger base plan). It logs `book_vol_sized` (day, unit_vol, gross, target). On a rebalance's first step for a coin with a mid, a position `within_band` of its target is skipped with `rebalance_skip_band` and no decision row. `plan_snapshot` adds `book_vol_target`, `rebalance_band`, `unit_vol`, `book_gross` to spec 0032's fields.
- **AC-3** (settings and version): the `TV_*` constants in `trader/paper_strategy.py` (`lookback_days` 30, `vol_days` 30, `vol_target` 0.155 (sets only the split between coins), `gross_cap` 5, `book_vol_target` 0.5, `rebalance_band` 0.3, `min_history_days` 120, `run_at_utc` "00:05", `retry_max` 3, `retry_wait_s` 60, `quote_wait_s` 60, `start_cooldown_s` 3600, `seed_days` 150, `seed_min_days` 121, no dollar exits, `uses_stop` False, `rebalance_weekday` 0 recorded but ignored) are recorded in the version, kind `trend`, demo only. `trend_vol` is in `BOOK_STRATEGIES` in `trader/main.py`. The VPS main trader runs version `01a1ccba-9db0-7444-8af7-7f0ba63b63e6` on the 12 `PAPER_INSTRUMENTS` coins since 2026-11-11 22:47 UTC.
- **AC-4** (hash rule): a hash baseline of every registered version's code closure is taken before the first task, and every one, `trend_vol`'s included, is unchanged at each close out, except where a task below names a new module. No file in `trend_portfolio`, `trend_daily`, or `trend_vol`'s closure is edited by this spec. If a change can only go into a closure file, stop and come back to `/architect`.

*Sizing history (thin thread first)*

- **AC-5** (tables): one Alembic migration adds `book_plans` and `book_plan_coins` exactly as in *Feature design*, through `db.py`'s metadata, with grants: `trader_app` INSERT and SELECT, `dashboard_app` SELECT, `backup_app` SELECT. Rows are written through new `recording.repository` functions (`record_book_plan`, idempotent: the same key and content is a no op, a different content raises `RecordConflictError`).
- **AC-6** (writer): a new task `write_book_plans` in `trader/main.py` (outside every strategy closure; there is no single snapshot loop, `write_heartbeats` is one task per reader), started only when `PAPER_STRATEGY` is in `BOOK_STRATEGIES`, takes `portfolio.plan_snapshot`, the paper run id, and the DB sessions, and polls on the snapshot interval. The first time it sees a `last_rebalance_at` it has not handled, it builds the `book_plans` row and its coin rows **once** from that snapshot and keeps that object; it writes and retries that same object (never a fresh read, since `_make_plan` may replace the plan before the write lands) in one transaction in a worker thread (`asyncio.to_thread`), not through `RecordingWriter`. A failed write retries every tick (the unique key makes it safe), logs `book_plan_write_failed` at most once a minute, and never blocks entries, orders, or the event loop. A snapshot with no plan, no `last_rebalance_at`, or no equity writes nothing. A snapshot coin with no plan item (its fields None) gets no coin row. `gross_cap` comes from the strategy config the trader built. Non vol strategies write null vol fields.
- **AC-7** (alert): a new alerter rule `book_plan_stale` reads Postgres: for the active book run (main's `trader_state.run_id`, when `trader_state.book` is true), it alerts once per run and day when the newest `book_plans` row is older than 24 h 20 min, or when the run is older than 1 h 20 min (the 1 h start cooldown plus margin) and has no row. This survives restarts and snapshot expiry and also catches a missed daily run. A `RecordConflictError` on a book plan write is reported through `report_conflict` as any other conflict.
- **AC-8** (dashboard): the Portfolio page gains a read only panel of the last 30 `book_plans` rows of the run it shows (newest first): UTC date and time, reason, unit vol (annualised, as a percent), book gross, and three counts per row from that run's decisions with `decided_ns` from the row's `rebalanced_ns` up to the next row's (or now): **orders sent** (approved decisions), **refused** (refused decisions, such as held grows), **no order** (plan coins with no decision: band, too small, missing close, stale quote). Empty state: "No rebalances recorded yet". The dashboard reads Postgres only, as it does today.

*Research tools*

- **AC-9** (pure book replay, `trading_core/lab/book_replay.py`, new file): replays a book strategy day by day by calling the same pure functions the trader runs (`portfolio_plan`, `book_vol_plan`, `within_band`, and for a candidate its own new pure functions), from hourly closes cut at a given UTC hour, at 7 bps a side on traded notional, with hourly funding from the candles tree where it has rates (days without rates are counted and reported). It returns daily equity and, per full period, per half, and per year: end multiple, Sharpe (daily, times sqrt 365), Sortino, Calmar, max drawdown, average gross, closed positions, and correlation with BTC. All Decimal. A new `lab book` command group in `apps/lab` drives it from the lab candles tree (`lab fetch-candles` for the 12 coins, 1 minute candles aggregated to hourly closes). Mechanics, fixed:

  | Item | Rule |
  |---|---|
  | Signal and plan | closes cut at hour H (the hourly close ending at H:00) |
  | Fill | the one minute close ending at H:05 (the trader plans at 00:00 and trades at 00:05; no same bar fill) |
  | Equity for the plan | marked equity (cash plus unrealised at the fill prices), as the trader's balance plus unrealised |
  | Quantity | `target_quantity` and the order delta rules with each coin's pinned instrument (`size_increment`, `min_quantity`) |
  | Band price | the fill price |
  | Start | flat, first plan on the first day every coin it needs has 120 days of history |
  | Funding | hourly, `-signed_qty * mid * rate` as `lab/funding.py` |
  | Sortino | mean daily return over the downside deviation (target 0), times sqrt 365 |
  | Calmar | CAGR over max drawdown |

- **AC-9a** (two fetch rules for the 12 coins, amended 2026-11-16; amends spec 0014 AC-1, AC-3, AC-4 and spec 0022 AC-2a for every coin): the first fetch of the 12 coins on 2026-11-15 refused three. DOGE and TRX traded on a tick of 0.000001 from their listing until 2026-08-20 07:22 UTC and on 0.00001 since (the pinned tick), so every older month has far more than `OFF_TICK_BODY_MAX` off tick candles. (Second amendment, 2026-11-16: the refetch on the first `until` of 07:00 refused 2026-08 on the 22 candles opening 07:00 to 07:21, all on the old tick; the 07:21 candle opens on the old tick and the first candle wholly on the new tick opens 07:22, read from the raw Kraken candles.) QNT holds one glitch, every candle opening 2023-08-17 21:44 to 22:01 UTC touching the raw price 92.56186715334, off its 0.01 tick, so that month passes `OFF_TICK_BODY_MAX` too. The market moved from 96.05 to 93.91 inside the glitch, and the candles 22:02 to 22:04 are quiet at 93.91, so the span must also cover them: a quiet candle right after the span is checked against 96.05 and refused (the refetch on a 19 minute span refused 22:03 for this). The first candle after the span, 22:05, trades (volume 10.77), so no quiet check reaches back past it. Two rules, both data in the pin, both read only by the fetcher:
  - **Tick history.** A pin may carry `_tick_history`, a list of `{"price_increment": "<tick>", "until": "<UTC ISO minute>"}` in time order: each tick holds for candles opening before its `until` and at or after the previous entry's `until`; the pin's `price_increment` holds from the last `until` on. `PF_DOGEUSD` and `PF_TRXUSD` get one entry, `{"price_increment": "0.000001", "until": "2026-08-20T07:22:00Z"}`, and their `_note` says it was read from the candles (the fetches' bad rows and the raw candles), not from a Kraken notice. Every check that uses a tick (the AC-1 rounding, `price_noise`, the AC-2a body rounding and clamp, the `OFF_TICK_BODY_MAX` pre pass, the quiet check, the wholly inside one tick rule) uses the tick of the candle's own open minute, so a month that straddles a change builds each candle on its own tick. Stored prices keep the tick of their time; nothing is rounded to the newer tick at fetch.
  - **Dropped spans.** A pin may carry `_drop_spans`, a list of `{"from": "<UTC ISO minute>", "until": "<UTC ISO minute>", "why": "<text>"}`: raw candles opening at or after `from` and before `until` are removed before the build (before the pre pass, the duplicate rule, and the quiet check, so the first candle after the span is checked against the last one before it). The missing minutes then become an ordinary AC-4 gap row and count in `missing_minutes` and `gap_count`. `PF_QNTUSD` gets one span, `{"from": "2023-08-17T21:44:00Z", "until": "2023-08-17T22:05:00Z", "why": "21 minutes: 18 touching 92.56186715334, off tick, then 3 quiet at 93.91 that the move inside the glitch leaves unmatched; refused by the 2026-11-15 and 2026-11-16 fetches"}`. A span covers only minutes a fetch has refused, or the quiet minutes right after them that the raw candles show would be refused next, and ends before the first candle that trades; a wider span, or a new one, is a spec amendment.
  - **Shape.** `build_candles` keeps its `tick` argument (the current tick) and gains keyword arguments `ticks` (earlier `TickPeriod`s, default none) and `drop` (`DropSpan`s, default none), both pure values in `trading_core/lab/candles.py`; `fetch_month` reads them from the pin through a new loader function in `lab/instruments` that drops every key starting with `_` before `CryptoPerpetual.from_dict` (today it drops only `_note`). The loader refuses a history out of time order, a tick at or below 0, or a span whose `until` is not after its `from`. A month that uses a history tick logs `lab_candles_tick_history` (month, ticks used); a dropped span logs `lab_candles_span_dropped` (month, from, until, raw candles removed). The manifest gains no field. A coin with neither key builds exactly as today, so every stored month of the other nine coins stays byte identical and none is fetched again.
  - **Consumers.** Readers that need one tick (`hour_marks`, `candle_stream` for Nautilus, so the AC-10 book mode) keep the pin's current tick and round an older, finer price as they do any price today. That moves a DOGE or TRX price by at most half of 0.00001, about 1 bp at their lowest prices in the window. The pure replay and `book_independent.py` read the stored prices as they are; AC-10a's 10 bps a day tolerance covers the difference.
  - **Proof.** The refetch of DOGE, TRX, and QNT passes with these two keys and no other change; any remaining bad row stops the fetch and comes back here. `trading_core/lab/candles.py` and `apps/lab` are outside every node closure, so AC-4 holds.
- **AC-10** (lab book mode): the candle lab gains a book mode in `apps/lab` (lab engine code, not a closure file): one strategy instance over every coin of the run, the registry's builder able to pass `seeds` (daily closes from the candles tree before `from`, `seed_days` of them) and `last_start_ns` None, and `from` meaning the first traded day. `trend_vol` (and later `trend_vol_r2`) are registered in it. Its start rebalance and cooldown behave as in the trader.
- **AC-10a** (replay parity, two engines): on one pinned window (2024-01-01 to 2024-06-30, 12 coins, `trend_vol` as built), run once with band 0.3 and once with band 0: (1) the pure replay against a Nautilus book mode run, and (2) the pure replay against an independent Decimal reimplementation of the whole book in `apps/lab/crosscheck/book_independent.py` that imports nothing from `trading_core`. Pass: every day's return within 10 bps, cumulative return within 1 percentage point, and the position side equal per coin per day except days where a coin is within 1% of its band edge. A fail blocks AC-12 onward. These runs count as trials (ledger).
- **AC-11** (Jesse, laptop only): a Compose profile `jesse` (never deployed, like `lab`) runs Jesse in its own container (Python 3.13, its own Postgres, plus Redis if its image needs one; never ours) with its password from a secret file `secrets/jesse_password` (added to `scripts/dev-secrets.sh`, mounted at `/run/secrets`; the container writes Jesse's `.env` at start, inside the container only). A script `apps/lab/jesse/cross_check.py` uses `jesse.research` to backtest the single coin 30 day sign rule at gross 1, daily, same fee, on BTC and ETH from our own candles tree, and compares with the pure replay of the same single coin book: the side matches on every day, and the total net return is within 2 percentage points. This is an extra check on single coin legs; the book level second engine is AC-10a's independent reimplementation. The Jesse MCP is connected to Claude Code in local scope only (`claude mcp add`, token from the secret file, never in the public repo) for ad hoc looks; it is never part of a pass condition.

*Pre check (before any rule change is built)*

- **AC-12** (pre registration): before the first run, a committed file `apps/lab/studies/trend_vol_precheck.toml` fixes, as literal values: the window start and end dates (start = the first day all 12 coins have 120 days of history; end = the last complete UTC day before the commit), the midpoint date splitting the traded days (warmup excluded) into halves, the cost (7 bps a side), the trials, and the pass rule.
  - Baseline: `trend_vol` as built, run at cut hours 00, 06, 12, 18; the reference Sharpe per half is the **mean of those four**.
  - EWMA trials: half life 20 days, neighbours 10 and 40, for coin vols and book vol both. EWMA variance: λ = 0.5^(1/half life), zero mean squared daily returns, seeded with the sample variance of the first 30 returns, updated over every available close; eligibility unchanged (`min_history_days` 120).
  - Tranche trials: K = 4, neighbours 2 and 8. K sub books with closes cut every 24/K hours from 00 UTC; each is planned and vol sized on its own at the full target and holds 1/K of equity; the book's target per coin is the sum; the band applies to the net position against that summed target; fees are paid on the netted trade.
  - Pass: net of costs and funding, the candidate's Sharpe beats the baseline reference in both halves, and neither neighbour is below it in either half. Tranches must also cut timing luck: the K = 4 book's Sharpe spread across start offsets 00 to 05 UTC (6 runs) must be under half the baseline's spread across its four cut hours.
  - Each name runs once: a committed ledger `apps/lab/studies/precheck_ledger.toml` lists every pre check name already run, and `lab book precheck` refuses a name in it (a file hash would break on a whitespace edit). Results go to `docs/specs/0033-trend-vol/precheck.md`. The same report shows, for information only, each trial on the free Binance 2018 to 2022 daily data used on 2026-11-07.
- **AC-13** (what a pass builds): if EWMA passes, build `trend_vol_r2` (new files `strategy/trend_vol_r2.py`, `node/trend_vol_r2.py`, tag `TV2`, kind `trend`, constants `TV2_*` as the `TV_*` ones plus `TV2_EWMA_HALF_LIFE_DAYS = 20`, demo only) whose pure rule recomputes each eligible coin's weight with the EWMA vol in `portfolio_plan`'s formula (signal times `vol_target` over vol, over N) and sizes the book with an EWMA book vol, everything else inherited from `TrendVol`. Its wiring edits `trader/paper_strategy.py` and `trader/main.py`; before that, the build confirms neither is in any registered version's closure (the closure follows imports from the strategy module, `versioning.py`). If tranches pass, stop and come back to `/architect` for the node design (hourly closes, seeds, restarts). If nothing passes, no r2 is built and the study runs on `trend_vol`.

*Study and gate*

*In sample means every day up to and including 2025-10-18 (the day before `holdout_start`), everywhere below.*

- **AC-14** (benchmark table): `lab book benchmark` writes, for the in sample window and the full window, one row each for the study candidate, hold BTC, hold all 12 equally, and `trend_daily` (spec 0032) at the same average gross, with the AC-9 measures. Report only, into `precheck.md`.
- **AC-15** (block bootstrap reruns): `lab book bootstrap` keeps the first 120 days real (warmup), then builds each path from in sample daily returns of the 12 coins (and their daily funding) in shared 120 day moving blocks: block starts drawn uniformly with one fixed seed (7), the same block dates for every coin. It rebuilds price paths, reruns the candidate rule on each, and reports the 5th, 50th, and 95th percentile of Sharpe and max drawdown. Default 1000 paths; the build times one rerun first, and if 5000 reruns (five targets) would take over 8 hours it uses 500 and records that. The same path set is reused for every target. A sensitivity line repeats the AC-16 pick with 60 and 180 day blocks (report only). Report only.
- **AC-16** (study target): on in sample days only, run AC-15 for book vol targets 4, 6, 8, 10, 12% a year and take the largest whose 95th percentile max drawdown is under 10%. That target is fixed in the study file before the study is created. If none is under 10%, stop and record the result; no study is created. The pick uses no holdout day (the gate then caps the realised holdout drawdown, a different measure).
- **AC-17** (candidate pick): when `trend_vol_r2` exists, the study candidate is whichever of `trend_vol` and `trend_vol_r2` has the higher in sample Sharpe in the AC-9 replay at the AC-16 target; a tie goes to `trend_vol`. Both count as trials.
- **AC-18** (thresholds v4): `gate/thresholds.toml` becomes version 4 with `[trend] prior_trials = 119` (the 13 tsmom pre check trials plus the 106 in the rationale.md ledger) and no other value changed; `gate/thresholds.py` `SUPPORTED_VERSIONS` gains 4 and `test_gate_thresholds.py` covers v4. Committed before the study is created. Older studies keep their frozen version. The gate's N stays `prior_trials` plus the database family trials for kind `trend` (`gate_queries.py`), so tsmom's study rows count too where present.
- **AC-19** (study): `apps/lab/studies/trend_vol.toml` (or `trend_vol_r2.toml`) runs the candidate in book mode on the 12 coins through `lab study`, with: the AC-16 target; grid `lookback_days = [20, 30, 40, 60]`; `[strategy.config]` listing every `TV_*` (or `TV2_*`) setting (`vol_days` 30, `vol_target` 0.155, `gross_cap` 5, `rebalance_band` 0.3, `min_history_days` 120, and the rest); `starting_balance_usd` 10000, `leverage` 5, `[risk]` `max_leverage` 9, `max_total_exposure` 50000, `max_order_notional` computed as spec 0032 AC-10 does (largest single coin target at the AC-16 target times 1.5, rounded up to the next 500); the lab's fees and slippage as `tsmom.toml`; `from` the first day all 12 coins have 120 days of history; `to` 2025-10-18; run seed 7. The study notes record that the holdout (from 2025-10-19) was seen by the 2026-11-11 replay and the AC-12 pre check. `lab holdout` runs once, and the gate verdict is recorded in the spec's Follow-up and in `precheck.md`.
- **AC-19a** (trade count first): before the study is created, the AC-9 replay counts the candidate's closed positions in the holdout window at the AC-16 target. Under `[candle] min_holdout_trades` (60): stop, record it, and create no study (the gate cannot pass, and changing thresholds now would come after seeing a result).
- **AC-20** (paper stage): on a study pass, the studied version is deployed to the VPS demo main trader (kfs.env `PAPER_STRATEGY` and constants, your `gate promote --manual --step candidate-paper`), with pause, flatten, deploy, unpause. "The studied version" means the paper version's frozen hash (code hash plus recorded settings, as `strategy_versions` stores it) equals the study version's; the build makes the trader's recorded settings match the study's `[strategy.config]`, and if the promote refuses, it stops. The gate's paper stage (at least 30 days) starts at the new version's first `book_plans` row. The 50% days since 2026-11-11 stay in `book_plans` as informal evidence only. On a study fail, the demo keeps `trend_vol` at 50% as an open forward test, and the fail is recorded.

## Decision

**Chosen option**: Option 1: record the build as is, add the sizing history as the first thin thread, and change the rule only through a pure replay pre check, then the study and gate.

`trend_vol` stays exactly as shipped; book_plans gives the forward test a history; EWMA and tranches are built only if they beat the current rule in both halves; the study runs at a target the bootstrap shows fits the gate's 10% drawdown, and on a pass the demo switches to that version for the paper stage.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`)

## Rationale

Reasoning, options, the trial ledger, and the research: see [rationale.md](rationale.md).

## Feature design

**Data model sketch** (one migration, additive):

`book_plans`
| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key (uuid7, like other rows) |
| `env` | text | no | env check, env foreign key pattern |
| `run_id` | uuid | no | → `strategy_runs` (N:1) |
| `plan_day` | date | no | the UTC day whose closes made the plan |
| `reason` | text | no | `start`, `resume`, `daily`, `weekly` (check constraint) |
| `rebalanced_at` | timestamptz | no | from `rebalanced_ns` |
| `rebalanced_ns` | bigint | no | |
| `equity` | numeric | no | USD |
| `n` | integer | no | eligible coins |
| `gross` | numeric | no | the base plan's gross field |
| `unit_vol` | numeric | yes | daily, at gross 1; null when not vol sized or no weight |
| `book_gross` | numeric | yes | null when not vol sized |
| `book_vol_target` | numeric | yes | annual; null when not vol sized |
| `rebalance_band` | numeric | yes | null when not vol sized |
| `gross_cap` | numeric | no | |
| `created_at` | timestamptz | no | |

Unique (`env`, `run_id`, `rebalanced_ns`); index (`run_id`, `rebalanced_ns`).

`book_plan_coins`
| Column | Type | Null | Notes |
|---|---|---|---|
| `book_plan_id` | uuid | no | → `book_plans` (N:1), part of primary key |
| `instrument_id` | text | no | → `instruments` (N:1), part of primary key |
| `eligible` | boolean | no | |
| `signal` | integer | no | −1, 0, 1; 0 when not eligible (check constraint) |
| `vol` | numeric | yes | |
| `weight` | numeric | no | |
| `target_notional` | numeric | no | USD |
| `close_date` | date | yes | |

(No `failed` column: `failed` is reset when a rebalance starts, so the snapshot cannot give its final value; failures already live in decisions and the failed coins alert.) Every coin row's `instrument_id` must have an `instruments` row; a missing one raises `RecordConflictError`.

Both tables are immutable and kept forever (about 13 rows a day).

**State transitions**: none new. The research path is a sequence with stop points: parity (AC-10) → pre check (AC-12) → r2 or not (AC-13) → target (AC-16) → thresholds v4 (AC-18) → study and holdout (AC-19) → paper stage or recorded fail (AC-20). A fail at a stop point ends the path there and is recorded.

**Interfaces**:
| Surface | Kind | Inputs | Outputs | Errors |
|---|---|---|---|---|
| `record_book_plan(session, row, coins)` | repository | `BookPlanRow`, `Sequence[BookPlanCoinRow]` | none | `RecordConflictError` on a different row at the same key |
| `write_book_plans` task (trader) | internal | `plan_snapshot`, paper run id, sessions, strategy config | rows in Postgres | logs `book_plan_write_failed` |
| alerter rule `book_plan_stale` | alert | main's `trader_state` (`book`, `run_id`), newest `book_plans` row and run start for that run | at most one alert per run and day | none |
| Portfolio page panel | dashboard read | run id (as the page already resolves it) | last 30 rows plus traded and skipped coins | empty state |
| `lab book parity` | lab CLI | window, strategy, band | parity report against Nautilus book mode and `book_independent.py`, exit 1 on a fail | |
| `lab book precheck <toml>` | lab CLI | the pre registration file | `precheck.md` section, the name added to `precheck_ledger.toml`; refuses a name already in the ledger | |
| `lab book benchmark`, `lab book bootstrap` | lab CLI | candidate, window, target(s), seed | report sections | |
| `apps/lab/jesse/cross_check.py` | script in `jesse` profile | coin, window | pass or fail plus figures | |

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| book_plans write | `run_id` | the paper run id the trader already holds (its `trader_state`) |
| | `plan_day`, `reason`, `rebalanced_ns`, `equity`, `n`, `gross` | snapshot fields `plan_day`, `last_reason`, `last_rebalance_at`, `equity`, `n`, `gross` |
| | `unit_vol`, `book_gross`, `book_vol_target`, `rebalance_band` | `trend_vol` snapshot fields (AC-2); absent → null |
| | `gross_cap` | the strategy config the trader built (`gross_cap`) |
| | coin rows | snapshot `coins[]` with a plan item: `instrument_id`, `eligible`, `signal`, `vol`, `weight`, `target_notional`, `close_date` |
| Portfolio panel | orders sent, refused, no order | derived: that run's decisions in the row's time window, by `risk_verdict` (AC-8) |
| | unit vol as annual % | derived: `unit_vol` times sqrt 365 |
| alert | active book run | main's `trader_state.run_id` and `book` |
| | age of the newest row, run age | newest `book_plans.rebalanced_at` and `strategy_runs` start for that run, versus now (UTC) |
| pure replay | hourly closes, funding | lab candles tree (spec 0014), aggregated from 1 minute candles |
| candle fetch | tick per minute, dropped spans | the pin's `_tick_history` and `_drop_spans` (AC-9a), else its `price_increment` |
| | costs | 7 bps a side, fixed in the pre registration file |
| pre check | trials, halves, pass rule | `trend_vol_precheck.toml` (AC-12) |
| study | book vol target | AC-16 result, written into the study file |
| | prior trials | thresholds v4 (AC-18): tsmom's 13 plus the rationale.md ledger |
| | risk block, seeds | AC-19 values; order cap computed from the AC-9 replay at the AC-16 target |
| Jesse check | candles | our candles tree, passed as arrays to `jesse.research` |
| | password, MCP token | `secrets/jesse_password` |

**Key invariants**:
- No strategy closure file changes (AC-4); new behaviour lives in new modules or outside closures.
- Recording sizing history never blocks trading (AC-6): it is a record, not an order.
- Every look at real data is a counted trial; the pre check and holdout run once.
- The study's target, grid, and trial count are committed before the study is created.
- Demo only: nothing here places a live order; `trend_vol_r2`, like `trend_vol`, refuses outside `TRADING_ENV=demo`.

**Security model**: `trader_app` writes book_plans; `dashboard_app` and `backup_app` only read. The dashboard stays read only on Postgres and Redis. The Jesse password and MCP token exist only as a laptop secret file and in Claude Code's local scope config; the repo is public, so neither is ever committed (gitleaks runs on every commit). The `jesse` profile never runs on the VPS and never sees Kraken keys or our databases.

**Configuration required**:
- No new trader or dashboard env vars. The demo switch (AC-20) changes `PAPER_STRATEGY` and constants only.
- `secrets/jesse_password`: laptop only, made by `scripts/dev-secrets.sh`.
- Compose profile `jesse`: laptop only.

**Failure and edge cases**:
- Postgres down: book_plans retries the same frozen row each tick; trading carries on; if no row lands the AC-7 alert fires within about a day (or 1 h 20 min for a new run).
- Two rebalances on one plan day (a start, then the 00:05 daily): two rows, both kept.
- A trader restart before a row is written loses that row (the in process book starts flat anyway, and the snapshot expires); nothing recovers it. The new run's `start` row follows its cooldown, and AC-7 alerts if it does not.
- Concurrency: `_make_plan` and `_rebalance` run in one synchronous callback on the loop the task polls from, so a snapshot is never torn; the only race (a newer plan before a retried write) is closed by freezing the row (AC-6).
- A coin with no mid at the first step is not band checked and goes through the base step (as built).
- Kraken changes a coin's tick again, or another glitch appears: the fetch refuses the month as today, and the fix is a new `_tick_history` entry or `_drop_spans` span through a spec amendment (AC-9a), never a looser `OFF_TICK_BODY_MAX`.
- The pure replay meets a coin with too few closes: that coin is ineligible that day, exactly like `portfolio_plan`.
- Kraken maintenance (503s): the bot fails closed as today; a missed daily run writes no row, and the next one does.

**Critical test scenarios**:
- Happy path: a laptop demo run's 00:05 rebalance writes one `book_plans` row and 12 coin rows, and the panel shows them with orders sent, refused, and no order counts, verifies **AC-5**, **AC-6**, **AC-8**.
- Failure: Postgres stopped during a rebalance, orders still go out, and the row written after Postgres is back is the frozen one even though a newer plan was made meanwhile, verifies **AC-6**.
- Alert: a book run with no row for 24 h 20 min, and a new run with no row after 1 h 20 min, each alert once, verifies **AC-7**.
- Idempotence: the same snapshot written twice gives one row; a changed equity at the same key raises `RecordConflictError`, verifies **AC-5**.
- Unit: `book_vol_plan` and `within_band` edge cases (zero weight, zero vol, cap hit, band edges, other side), verifies **AC-1**.
- Node: sizing `ValueError` keeps the old plan and logs `plan_empty`; a coin within band gets no decision, verifies **AC-2**.
- Hash: baseline equals close out for every registered version, verifies **AC-4**.
- Replay: no same bar fill (a signal at H:00 fills at the H:05 close); parity within 10 bps a day on both bands against both engines; a second `precheck` of a ledgered name is refused, verifies **AC-9**, **AC-10a**, **AC-12**.
- Fetch rules: a month straddling a tick change (old tick candles before `until`, new after) builds with no bad row, and the same month without `_tick_history` fails the `OFF_TICK_BODY_MAX` pre pass; a candle before `until` that is off the old tick is still rounded or refused as AC-2a says; a 21 minute dropped span gives one gap row of 21 missing minutes and the next candle is quiet checked against the last one before the span; a history out of order, a tick of 0, and an empty span are refused by the loader; a coin with neither key builds byte identical rows, verifies **AC-9a**.
- Thresholds: v4 loads with `prior_trials` 119, v3 studies still load as v3, verifies **AC-18**.
- Permission: `dashboard_app` INSERT on `book_plans` is denied, verifies **AC-5**.

## Build plan

Tracer Bullet: the sizing history goes end to end first (trader to Postgres to dashboard and alerts) and ships on its own, then the research path thickens one stop point at a time.

1. [x] Hash baseline of every registered version (main a8a5a6d, `scripts/spikes/hash_check.py`), satisfies **AC-4**.
2. [x] Record the as built facts: unit and node tests already exist (`test_strategy_trend_vol.py`, `test_node_trend_vol.py`, `test_paper_trend_vol.py`); confirm they cover AC-1 to AC-3 and add any missing case, satisfies **AC-1**, **AC-2**, **AC-3**.
3. [x] Thin thread: migration, rows, `record_book_plan`, the `write_book_plans` task, the Portfolio panel, the `book_plan_stale` alert, laptop demo run, then PR and a VPS deploy on your go ahead (pause, flatten, deploy, unpause; no promote, the version is unchanged), satisfies **AC-5**, **AC-6**, **AC-7**, **AC-8**.
4. [x] Candles for the 12 coins, the pure book replay with its mechanics table, and the `lab book` group; the two fetch rules (`ticks`, `drop`, the loader change, the DOGE, TRX, and QNT pin keys) with their tests, then refetch DOGE, TRX, and QNT and record the per coin fetch figures (months, gap rows, roundings, the history months, the dropped span) in `verify.md`, satisfies **AC-9**, **AC-9a**.
5. Lab book mode (one instance, seeds) with `trend_vol` registered, the independent Decimal reimplementation, and the parity runs on both bands, satisfies **AC-10**, **AC-10a**.
6. Jesse profile, secret, cross check script on BTC and ETH, MCP in local scope, satisfies **AC-11**.
7. Commit the pre registration file and the empty ledger, then run the pre check once and write `precheck.md`, satisfies **AC-12**.
8. If EWMA passed, confirm the wiring files are in no closure, then build `trend_vol_r2` (pure rule, node, wiring, tests, hash check); if tranches passed, stop for `/architect`, satisfies **AC-13**.
9. Benchmark table, time one rerun, bootstrap reruns, the target pick on in sample days, the candidate pick, and the holdout trade count, satisfies **AC-14**, **AC-15**, **AC-16**, **AC-17**, **AC-19a**.
10. Thresholds v4 with the code and test change, the study file, `lab study`, `lab holdout` once, the gate verdict recorded, satisfies **AC-18**, **AC-19**.
11. On a pass, the demo switch (frozen hash match, your promote) and the paper stage start; on a fail, record it and keep the demo as is, satisfies **AC-20**.

## Consequences

**Positive**:
- The forward test gets a queryable history from the first deploy, without touching the running version.
- Rule changes cost a replay, not a build, and the pass rule is fixed before results are seen.
- The study and gate judge the exact version that would go live, at a size the drawdown cap allows.

**Negative / tradeoffs**:
- The study's target (about 8%) is far below the demo's 50%, so a passed study means a much smaller book on demo, and the 50% run's days are not gate evidence.
- 119 prior trials make the deflated Sharpe check hard to pass; a fail is the likely outcome given the H2 weakness and negative 2026.
- The holdout is already seen; the only clean out of sample evidence is the paper stage after the switch.
- Jesse adds a second stack on the laptop (its own Postgres, Python 3.13, a secret) for a check that covers only single coin legs; the independent Decimal book is the stronger second engine.
- The lab gains a book mode, real engine work before any study can run.
- The study may never be created: AC-16 (no target under 10%) and AC-19a (too few closed positions) are both plausible stops.
- book_plans uses its own write path, a second pattern beside `RecordingWriter`.

**Neutral**:
- One additive migration; nothing existing is transformed.
- `trend_vol_r2` exists only if EWMA passes; tranches need another `/architect` round.

## Migration plan

**Strategy**: strangler, additive.
**Phases**:
1. book_plans migration and writer deploy (no version change).
2. Research tools on the laptop only.
3. On a study pass, a demo version switch (new version, your promote).
**Rollback**: phase 1: redeploy the previous sha (the tables stay, unused; downgrade drops them if wanted). Phase 3: restore the `kfs.env` backup and redeploy; if the old release rejects a new strategy name (`trend_vol_r2`), the rollback fails as on 2026-11-11, so redeploy the new sha with the old `PAPER_STRATEGY` instead.
**Risks**: the deploy health gate failing on a slow recorder (redeploy `--now`); the trader starting flat on every restart (intended).

## Follow-up

- [ ] `verify.md` for the VPS checks of AC-3, AC-6, AC-7, AC-8 (written by `/check verify`).
- [ ] Gate verdict: record it here when AC-19 runs.
- [ ] Spec 0028 (go live) reads this verdict before any live step.
