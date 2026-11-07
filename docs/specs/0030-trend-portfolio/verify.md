# Verify: trend portfolio · spec 0030 · updated 2026-11-07

_Build record from `/develop`. The verify steps for `/check verify` are added at the end of the build._

## Build record

### Task 1: hash baseline (AC-11), 2026-11-07

Every `trading_core.node` module with a `STRATEGY_NAME` (21 modules) had its `code_closure` and `code_hash` recorded before any edit, at commit c2a4f1c. The union of their closures is 84 files. After task 2, all 21 hashes were unchanged, and `trading_core.node.trend_portfolio`'s closure adds only its 3 new modules (`node/trend_portfolio.py`, `risk/rebalance.py`, `strategy/trend_portfolio.py`).

### Task 2: laptop smoke as the main slot, 2026-11-07 00:00 to 00:06 UTC

`PAPER_STRATEGY=trend_portfolio`, BTC, ETH, SOL, the portfolio caps (exposure 5000, order 1000, leverage 2.5, trades 100). Version `01a1b34e-99b5-7077-8ebe-13c9b55636f6` (kind `trend`, frozen 7a0f87de4b71), promoted by hand on the laptop database (promotion `01a1b34f-1cd6-7360-b26e-a5767de9e2ec`), which confirms a `trend` version is accepted (AC-10). Run `01a1b34f-886e-73e4-9288-e95fe4763154`.

- Seeds: `warmup_seed_fetched` with 150 daily bars for each coin; no `warmup_seed_failed` or `seed_stale` (AC-8).
- Start: after the clean start check, one `portfolio_targets` line per coin, close date 2026-11-06, all eligible and short. The trader started paused, so the start rebalance wrote 3 `held:paused` decision rows and sent nothing (AC-4, AC-5, AC-7).
- Resume: `unpause` gave a `resume` rebalance: 3 approved decision rows, 3 market sells filled (ETH 0.116, SOL 3, BTC 0.0046), each order linked to its decision (AC-4, AC-7).
- Daily run: fired at 00:05:00 UTC on a Saturday, logged the targets again, and sent nothing (AC-4).
- `demo:snapshot:portfolio_plan` held the plan per coin with target quantities (AC-13).
- Finding, fixed in the same task: the reconcile actor sets `order_unresolved` while any entry order waits for its answer (spec 0023 AC-14), so a grow sent then would have been held until a resume. A grow blocked only by `order_unresolved` now waits for the retry instead (`rebalance_deferred`).

The stack was taken down after the check.

### Task 3: laptop smoke with both traders, 2026-11-07 08:05 to 11:22 UTC

`trader` as **main** (`six_vote_trend`, ETH and BTC, `TRADER-DEMO`) and `trader-portfolio` as **portfolio** (`trend_portfolio`, ETH, SOL, BTC, `PORTFOLIO-DEMO`), on migration `0015_command_slot`. All 21 strategy hashes unchanged; six vote needed no new promotion, the portfolio did (its hash changed with the slot wiring).

- Names: each trader has its own run; the portfolio writes `portfolio_` snapshots, reads `demo:trader:portfolio:pause|halt`, and reads control in group `trader-portfolio` started at `$` (AC-12 to AC-14).
- Start: the portfolio sent 3 approved short rebalance orders, all filled.
- `pause --slot all`: two `executed` results, one per slot, both REDUCING. `unpause --slot all` lifted both; the portfolio's `resume` rebalance gave 3 `rebalance_skip_small` (AC-14).
- `stop --slot all`: both halted with `manual_stop` and 0 positions. `flatten --slot all`: main executed, the portfolio answered `failed: not_for_slot` (AC-14). `resume --slot all` executed for both.
- P&L and Trades scoped to `TRADER-DEMO` leave out the portfolio's 3 round trips and 6 fills; the journal carries a `trader` column (AC-15, AC-16).
- Redis ACL: no change needed (the trader has `~*`, the dashboard and alerter read `*:snapshot:*`).
- Finding, fixed in c64abea: both traders wrote the same `halt:<id>` and `resume:<id>` risk event keys, so the second write hit `record_conflict` and held the portfolio's entries. The portfolio's keys (halt, resume, trade_cap) now end in `:portfolio`; main's are unchanged. Redone after the fix: `stop --slot all` and `resume --slot all` both executed, four distinct keys in `risk_events`, no `record_conflict`, and the portfolio back to ACTIVE with `entries_blocked` empty and 3 positions open again.

The stack was taken down after the check.

### Task 6: the Portfolio page, laptop check with both traders, 2026-11-07 20:29 to 20:46 UTC

Same two trader setup as task 3 (`trader` main on six vote, ETH and BTC; `trader-portfolio` on ETH, SOL, BTC), built at 5aed95d. The portfolio's code hash changed in task 4, so its version `01a1b78d-d4a1-75cc-aefd-fe433fdfda12` was promoted by hand on the laptop database (promotion `01a1b7b3-d5ac-7081-b468-b8de744233f2`). Six vote's hash is unchanged. All 21 strategy hashes are unchanged after this task.

The board was rendered by the real app's poller inside the dashboard container (real Redis and Postgres, the same partial the page and `/events` serve), since the laptop login password is yours.

- Before a plan: "No plan yet" rows from the positions alone, the summary with equity 1000 USD and realized 0 (the Postgres query by `run_id` answered).
- With the plan: 3 rows in the trader's coin order (ETH, SOL, BTC), each "Short" in words, with target weight (−26.14 %, −27.06 %, −35.41 %), target USD, and target size; next rebalance 2026-11-09 00:05:00Z · 01:05 Amsterdam (AC-20, AC-21).
- `pause --slot portfolio` then `unpause --slot portfolio`: a `resume` rebalance sold 3 positions. The board then showed short 0.117 ETH, 3.03 SOL, 0.0046 BTC (matching the targets), unrealized P&L per coin, equity 999.76, gross leverage 0.88×, last rebalance 20:44:29Z · 21:44 Amsterdam (`resume`), and last command "unpause · confirmed" (AC-20, AC-21).
- No portfolio heartbeat shows "Portfolio trader not running" (unit tests: never seen, and a stale one with the time last seen), and Redis down shows "Portfolio unavailable" (AC-19).
- `trader_state` now carries `run_id` when there is a paper run; the page reads the run's realized P&L by it.
- Finding, fixed in 5aed95d: the portfolio slot wrote its plan as `demo:snapshot:portfolio_portfolio_plan` (the writer added the slot prefix to a name that already had it). The page and the alerter's `portfolio_coins_failed` read `portfolio_plan`, so that alert could never have fired. The plan now lands on `portfolio_plan` in either slot, pinned by a test.

The stack was taken down after the check.
