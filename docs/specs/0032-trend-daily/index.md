# 0032. trend_daily: the 30 day trend book at 5x, rebalanced every day, on the main demo trader

**Date**: 2026-11-11
**Status**: In Progress

## Summary

`trend_daily` is `trend_portfolio` (spec 0030) with one change: it rebalances every day at 00:05 UTC (01:05 Amsterdam) instead of only on Mondays. It holds all 12 demo coins at once, long a coin above its price 30 days ago and short one below it, each sized by its volatility (calmer coins get more), the whole book at up to 5 times the sandbox equity (5x, the owner's choice). It replaces the losing 5 minute scalper `donchian_scalp_tp` on the **main** trader. It has no stops between rebalances and no drawdown brake. It goes straight to the demo as an open ended forward test (no history test first, the owner's choice). The build is small: a thin new module on top of `trend_portfolio`, the trader wiring, and two fixes so a book can live on the main trader (when the main trader holds a demo book, the deploy no longer waits for it to be flat, and the Portfolio page can read it).

## Requirements

**User stories**:
- As the trader, I want the main demo trader to hold the 30 day trend book at 5x and refresh it every day, so that I trade the one idea with an edge at day holds instead of a scalper that loses its fees.
- As the trader, I want every coin set to its target each day, with no stops in between, so that the book follows the rule exactly.
- As the operator, I want deploys and the Portfolio page to work with the book on the main trader, so that nothing about running it needs a manual workaround.

**Acceptance criteria**:

*The strategy*

- **AC-1** (the module): a new node module `trading_core/node/trend_daily.py` with `TrendDaily(TrendPortfolio)` and `TrendDailyConfig(TrendPortfolioConfig)`, `STRATEGY_NAME = "trend_daily"`, `ORDER_ID_TAG = "TD"`. Everything spec 0030 AC-1 to AC-9 pins (the rule, quantity and delta, orders, flips, `check_rebalance`, held orders, retries, decisions, warmup seed, start and resume rebalances, the start cooldown) is inherited unchanged. `rebalance_weekday` is ignored (kept only because the base config requires it). No file in `trend_portfolio`'s or any other registered version's closure is edited (spec 0030 AC-11's rule); a hash baseline is taken first and every existing version's code hash is unchanged at close out. If a change can only go into a closure file, stop and come back to `/architect`.
- **AC-2** (daily rebalance): `TrendDaily` overrides `_on_daily`. Every day at `run_at_utc` (00:05) it plans from closes up to the day that just ended, exactly as the base does, and when the plan was made it rebalances with reason `daily`, at most once per date (the node keeps the date of its last daily rebalance; a second fire on the same date does nothing). The base's Monday `weekly` rebalance never runs. A failed run logs `portfolio_run_failed` as the base does. A `start` or `resume` rebalance does not count as that date's daily one: after a start at 00:03, both the `start` and the 00:05 `daily` rebalance run (intended; the second finds small deltas only).
- **AC-2a** (plan snapshot schedule): `TrendDaily` overrides `plan_snapshot` so the `portfolio_plan` data reports `schedule: "daily"` and `run_at_utc` in place of `rebalance_weekday` (the base's fields otherwise unchanged). The Portfolio page's next rebalance (`dashboard/live/portfolio.py` `next_rebalance`) shows the next `run_at_utc` UTC when `schedule` is `daily`, and the next weekday as today when the field is missing.
- **AC-2b** (late daily bar): when a daily run skips one or more coins with `close_missing`, the plan's data lists them under `close_missing` and the alerter sends one alert per plan date naming them (same dedupe as AC-9).
- **AC-3** (every change trades): no no trade band. Every coin whose delta passes spec 0030 AC-2's size check (at least one `size_increment` and `min_quantity`) gets its order each day.
- **AC-4** (settings, recorded in the version): `lookback_days` 30, `vol_days` 30, `vol_target` 0.155, `gross_cap` 5, `min_history_days` 120, `run_at_utc` "00:05", `retry_max` 3, `retry_wait_s` 60, `quote_wait_s` 60, `start_cooldown_s` 3600, `seed_days` 150, `seed_min_days` 121, `coin_take_profit_usd` and `coin_stop_loss_usd` unset (no dollar exits), `uses_stop` False (no protective stop), and `rebalance_weekday` 0 (recorded in the version because the base config needs it, ignored by `TrendDaily`). The values are new constants `TD_*` in `trader/paper_strategy.py`, not the `TP_*` ones, so a later change to either strategy moves only its own version. The version also records the `RISK_*` values (as `trend_portfolio`'s does), so any later cap change is a new version and a new manual promote.
- **AC-5** (paper wiring): `PAPER_STRATEGY` accepts `trend_daily` with more than one coin, demo only (the config builder refuses outside `TRADING_ENV=demo`). It joins `_MULTI_COIN` and `SEEDED_STRATEGIES`, gets the 1 day seed (spec 0030 AC-8), is built as one instance over every coin with `TrendPortfolioView` for its snapshots, and writes the `portfolio_plan` snapshot. Every place in `trader/main.py` that tests `paper_strategy == trend_portfolio.STRATEGY_NAME` tests membership in a new `BOOK_STRATEGIES = (trend_portfolio, trend_daily)` tuple instead, including the start cooldown lookup (`last_decision_ns`, reason `start`, this trader's id). `paper_version` records name `trend_daily`, kind `trend`, and every AC-4 setting plus `instruments`. `TRADER_SLOT=portfolio` still requires `trend_portfolio` (unchanged); `trend_daily` runs on the `main` slot.

*Running a book on the main trader*

- **AC-6** (book flag): the `trader_state` snapshot gains `book` (boolean): true only when the trader runs a demo paper strategy (`TRADING_ENV=demo` and a paper strategy is built) and `PAPER_STRATEGY` is in `BOOK_STRATEGIES`; false otherwise, including every live trader. It is a new parameter of the `trader_state` writer in `trading_core/node/control.py` (in no strategy closure), passed from `trader/main.py` where the paper strategy is built. A reader treats a missing field as false.
- **AC-7** (deploy and safe reboot): `deploy/host/lib.sh` `kfs_pause_and_wait` reads `.data.book` and `.data.env` from main's `trader_state`. When `book` is true and the env is `demo` it still sends the pause (and waits for its result) but does not wait for flat: it logs INFO `trader holds a book, not waiting for flat` and returns 0, as `--now` does. The restart then starts the book flat and rebuilds it with its start rebalance (spec 0030 AC-4, AC-9). With `book` false or missing, nothing changes. `kfs-safe-reboot` uses the same function, so it gets the same behaviour.
- **AC-8** (Portfolio page): a new dashboard setting `DASHBOARD_PORTFOLIO_SLOT` (`portfolio` or `main`, default `portfolio`) picks which trader's snapshots the Portfolio page reads: `portfolio` reads `portfolio_heartbeat`, `portfolio_trader_state`, `portfolio_positions`, `portfolio_account` and the `slot=portfolio` result (today's behaviour); `main` reads `heartbeat`, `trader_state`, `positions`, `account` and main's results. `portfolio_plan` is the key in both cases (it is already named that on either slot). In `main` mode the page's last command result filters on slot `main` (results with no `slot` field are main's, `control/slots.py` `result_slot`), and its paused and flat panel and realized P&L read main's `trader_state` (its `run_id` is the paper run). The setting is passed to the dashboard in `compose.yaml` (`DASHBOARD_PORTFOLIO_SLOT: ${DASHBOARD_PORTFOLIO_SLOT:-portfolio}`), since the dashboard's env is listed explicitly. The VPS sets `main`. The Overview, P&L, and Trades pages already read main and keep doing so. The page also shows, for the last 7 UTC days, the notional traded and the fees paid per day (from `fills` of main's runs), so the daily churn can be judged.
- **AC-9** (alerts): the alerter's failed coins check (spec 0030 AC-17, `alerter/service.py`) also runs when main's `trader_state.book` is true (the alerter already reads main's `trader_state`), even when `ALERTER_TRADER_SLOTS` does not list `portfolio`. Its observation then uses `service="trader"` and `fields.slot="main"`. The dedupe stays on the plan's `last_rebalance_at`, so a daily rebalance with failed coins sends at most one alert per day (intended). A `liq_distance` refusal (not retried, spec 0030 AC-6) lands in `failed` and so is alerted the same way. Heartbeat and entry block alerts for main are unchanged.

*Switching the VPS*

- **AC-10** (kfs.env, demo only): before the deploy, `/etc/kfs/kfs.env` gets `PAPER_STRATEGY=trend_daily` and `RISK_MAX_ORDER_NOTIONAL_USD` set from a computation, not a guess: the build runs `portfolio_plan` with the AC-4 settings on the last 150 Kraken daily closes of the 12 coins at 1000 USD equity, takes the largest single coin target notional, and sets the cap to the next 500 USD above 1.5 times it (at least 2500). The computed figures go in the PR. `RISK_MAX_TOTAL_EXPOSURE_USD` 9000 and `RISK_MAX_LEVERAGE` 9 stay; they are a known ceiling: once equity passes about 1800 USD, 5 times equity exceeds 9000 and `check_rebalance` clips the grows (logged `rebalance_clipped`). `RISK_MAX_TRADES_PER_DAY` 1000 stays. The dashboard gets `DASHBOARD_PORTFOLIO_SLOT=main`. A backup `kfs.env.bak-<date>-trenddaily` is taken first. `COMPOSE_PROFILES` stays empty (trader-portfolio off).
- **AC-11** (go live on demo): after the merge and the CI images, the deploy exits 4 for the new version; the owner runs the printed `gate promote --manual --step candidate-paper` line with the note `trend_daily`. The switch deploy still meets the old scalper, whose `trader_state` has no `book`, so it would wait for flat: run it as `control_cli pause --slot main --source cli`, `flatten --slot main`, `scripts/deploy.sh`, then `unpause --slot main --source cli`. The expected sequence after it:
  1. `Cached 0 positions`, and all 12 coins seeded (150 daily closes each, at least 121).
  2. The trader starts paused, so the `start` rebalance writes decision rows refused `held:paused` for every grow (reductions none, the book is flat).
  3. The unpause triggers a `resume` rebalance whose decisions are approved and whose orders fill; the book's gross exposure is then about 5x equity (within the `check_rebalance` clips).
  4. The Portfolio page (main mode) shows the book with the daily schedule.
  5. The next 00:05 UTC run logs a `daily` rebalance.

  Later deploys read `book` true and skip the flat wait (AC-7).

## Decision

**Chosen option**: Option 1: a thin `TrendDaily` subclass of `TrendPortfolio` on the main trader.

`trend_daily` inherits all of `trend_portfolio`'s tested rule and order handling and changes only when it rebalances (every day), with the owner's 5x settings, and it replaces `donchian_scalp_tp` as the main trader's `PAPER_STRATEGY`.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no schema change. It reuses the existing tables:
- `strategies` and `strategy_versions`: a new row `trend_daily`, kind `trend`, with its AC-4 config.
- `strategy_runs`: one per trader start, `trader_id` the main trader's.
- `decisions`: one row per coin per rebalance with a delta, reason `daily`, `start`, or `resume` (spec 0030 AC-7's inputs).
- `orders`, `fills`, `positions`: as today, linked by the decision's tag.

Redis: main's `trader_state` gains the `book` boolean (AC-6); `portfolio_plan` is written by the main trader (already its key name on either slot).

**State transitions**: per rebalance, as spec 0030: planned → reductions sent → reductions done → grows sent → done, with a coin moving to `failed` after `retry_max` retries. Daily trigger: at most one `daily` rebalance per UTC date.

**API surface** (no HTTP surface is added; the interfaces are code and config):

| Interface | Kind | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `TrendDaily._on_daily` | clock timer, 00:05 UTC | daily closes, equity, current positions, quotes | `portfolio_targets` logs, plan, one `daily` rebalance | in process | `portfolio_run_failed`, `plan_empty`, `equity_unknown`, `close_missing` |
| `paper_strategy` builder | trader startup | `PAPER_STRATEGY=trend_daily`, `PAPER_INSTRUMENTS`, `RISK_*` | one `TrendDaily`, its version | env config | refuses outside demo, without BTC, or on a bad value |
| `trader_state` snapshot | Redis key | strategy name | `book` boolean | Redis ACL (trader writes, dashboard and deploy read) | missing field reads as false |
| `kfs_pause_and_wait` | deploy shell function | main's `trader_state` | 0 without waiting when `book` is true | root on the VPS | `KFS_DRAIN_*` codes as today when `book` is false |
| Portfolio page | dashboard GET | `DASHBOARD_PORTFOLIO_SLOT` | the book from the chosen slot's keys | dashboard login | `not_running` on a stale heartbeat |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| daily rebalance | each coin's signal, vol, weight, target notional | `trend_portfolio.portfolio_plan` on the stored daily closes (seed plus live `1-DAY-LAST-INTERNAL` bars) |
| daily rebalance | equity | the sandbox account's equity (the base's equity read, 1000 USD start, `SANDBOX_STARTING_BALANCE_USD`) |
| daily rebalance | the date it ran | the clock at the 00:05 fire, UTC date; plan date is that minus one day |
| daily rebalance | mid for sizing, staleness | the coin's latest quote tick, checked against `RISK_STALE_QUOTE_S` |
| order clip | order, exposure, leverage caps | `RISK_MAX_ORDER_NOTIONAL_USD` (computed, AC-10), `RISK_MAX_TOTAL_EXPOSURE_USD` 9000, `RISK_MAX_LEVERAGE` 9 (kfs.env) |
| plan snapshot | schedule, next rebalance | `TrendDaily.plan_snapshot` (`schedule` daily, `run_at_utc`), AC-2a |
| Portfolio page | notional traded and fees per day | `fills` of main's runs, summed per UTC day (AC-8) |
| alerts | coins skipped or failed | the plan's `close_missing` and `failed` lists (AC-2b, AC-9) |
| version | name, kind, settings | `TD_*` constants and `instruments` (AC-4, AC-5) |
| `trader_state.book` | true or false | `PAPER_STRATEGY` in `BOOK_STRATEGIES` (AC-6) |
| deploy drain | wait or skip | `.data.book` of main's `trader_state` (AC-7) |
| Portfolio page | which keys to read | `DASHBOARD_PORTFOLIO_SLOT` (AC-8) |

**Key invariants**:
- At most one `daily` rebalance per UTC date; never a `weekly` one.
- Reductions before grows, and every open or grow passes `check_rebalance` (inherited).
- No protective stop and no dollar exits on any position.
- No existing version's code hash changes.
- Demo only: the builder refuses `trend_daily` outside `TRADING_ENV=demo`.

**Security model**: no new secret, credential, or endpoint. The trader writes only its own Redis keys (the existing ACL covers `trader_state`); the dashboard reads them behind its login; the deploy reads them as root on the VPS. Demo money only.

**Configuration required**:
- `PAPER_STRATEGY=trend_daily` (existing variable, new value)
- `RISK_MAX_ORDER_NOTIONAL_USD` on the VPS, computed per AC-10 (existing variable, new value)
- `DASHBOARD_PORTFOLIO_SLOT`: `portfolio` (default) or `main`, which trader the Portfolio page reads; passed to the dashboard in `compose.yaml`

**Critical test scenarios**:
- Happy path: on a test clock, three 00:05 fires on Tuesday, Wednesday, Thursday each plan and rebalance once with reason `daily`, and a coin whose target moved gets an order each day, verifies **AC-2**, **AC-3**
- Failure case: a second 00:05 fire on the same date (a timer re fire) sends nothing, verifies **AC-2**
- Failure case: the deploy with main's `trader_state.book` true pauses, does not wait for flat, and goes on; with `book` false it still aborts `aborted_not_flat` after the wait, verifies **AC-7**
- Version: `trend_portfolio`'s code hash and version id are the same before and after the build, and `trend_daily`'s version records every AC-4 value, verifies **AC-1**, **AC-4**
- Wiring: `PAPER_STRATEGY=trend_daily` with `TRADING_ENV=live` refuses at startup, verifies **AC-5**
- Dashboard: `DASHBOARD_PORTFOLIO_SLOT=main` shows main's book with the daily next rebalance and fees per day, the default still shows the portfolio slot, verifies **AC-2a**, **AC-8**
- Safety: a live trader with `PAPER_STRATEGY=trend_daily` left in its env reports `book` false, so the deploy still waits for flat, verifies **AC-6**, **AC-7**
- Alerts: a plan with a `close_missing` coin sends one alert naming it, and a second rebalance on the same plan date sends none, verifies **AC-2b**, **AC-9**

## Build plan

Tracer Bullet: the strategy end to end on the laptop first, then the main trader plumbing, then the VPS switch.

1. Take the hash baseline (every registered version's code hash and closure file list), satisfies **AC-1**
2. Write `node/trend_daily.py` (`TrendDaily`, `TrendDailyConfig`, the `_on_daily` and `plan_snapshot` overrides, `daily` reason, the `close_missing` list) with unit tests on a test clock, satisfies **AC-1**, **AC-2**, **AC-2a**, **AC-2b**, **AC-3**
2a. Compute the order cap: run `portfolio_plan` with the AC-4 settings on the last 150 Kraken daily closes at 1000 USD equity, record the largest coin target and the chosen `RISK_MAX_ORDER_NOTIONAL_USD`, satisfies **AC-10**
3. Wire it into `trader/paper_strategy.py` (`TD_*` constants, `trend_daily_config`, `build_trend_daily`, `paper_version`, `_MULTI_COIN`, `SEEDED_STRATEGIES`) and `trader/main.py` (`BOOK_STRATEGIES` in every place that tests for `trend_portfolio`), with tests; run it on the laptop demo and see a `start` rebalance, satisfies **AC-4**, **AC-5**
4. Add `book` to `trader_state` (demo paper book only) and teach `kfs_pause_and_wait` to skip the flat wait on it in demo, with a shell test in `scripts/tests`, satisfies **AC-6**, **AC-7**
5. Add `DASHBOARD_PORTFOLIO_SLOT` (dashboard settings and `compose.yaml`), the Portfolio page's slot reads, daily schedule, and fees and notional per day; add the main slot case and the `close_missing` alert to the alerter, with tests, satisfies **AC-2a**, **AC-2b**, **AC-8**, **AC-9**
6. Check the hash baseline again: every existing version unchanged, satisfies **AC-1**
7. PR, merge, CI images; edit kfs.env with a backup, satisfies **AC-10**
8. Deploy, owner promotes with note `trend_daily`, then pause, flatten, deploy, unpause; check the held `start` rows, the `resume` fills, the page, and the next 00:05 run, satisfies **AC-11**

## Consequences

**Positive**:
- The main trader trades the one idea with evidence at day holds; about 12 orders a day instead of hundreds. Fees per day are unknown until measured: targets scale with equity, so every coin trades a little each day; the page's fees per day (AC-8) gives the figure after the first week, recorded in the spec then.
- Daily rebalancing removes the weekly result's dependence on which weekday it trades.
- Almost all logic is the tested `trend_portfolio`; the new code is a timer override and wiring.

**Negative / tradeoffs**:
- 5x with no brake and no stops: earlier tests show the 30 day trend book at 5x can lose most of the account in a bad year; on the 1000 USD sandbox one bad week can cost hundreds. The owner accepted this for the demo.
- No history test of the daily schedule: earlier tests found hourly checks lost to weekly, so daily may churn more than it earns. Trading every change (no band) adds daily fees on small adjustments.
- The sandbox book lives in the process: every restart starts flat at 1000 USD and rebuilds (a deploy through its `resume` rebalance; a crash restart within an hour of the last start stays flat until the next 00:05, the start cooldown). Results across restarts are only comparable through the database.
- The 9000 USD exposure and 9x leverage caps clip the book once equity passes about 1800 USD; a later cap change is a new version and a new promote.
- donchian_scalp_tp's demo run ends; its numbers stay in the database.

**Neutral**:
- New version and a manual promote, as for every strategy change.
- With no fixed review date (the owner's choice), "is it working" stays a judgment call; at least 4 weeks are needed before the numbers mean much.

## Follow-up

- [ ] If the book does well on demo, a lab study of the daily schedule and the gate verdict are owed before any live money (spec 0015, spec 0028).
- [ ] Consider a drawdown brake if the 5x book has a deep drawdown on demo (declined for now).
- [ ] trader-portfolio (weekly, spec 0030) stays off; one book trader fits the VPS.
