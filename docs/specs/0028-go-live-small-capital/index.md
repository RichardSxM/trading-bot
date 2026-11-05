# 0028. Go live with small capital: a live trader beside demo, a per coin canary, your typed sign off, and live risk stops

**Date**: 2026-11-05 (cross check fixes applied the same day)
**Status**: Proposed

## Summary

This builds everything real money needs, but no strategy trades live until a version earns the gate's paper to live pass and you sign off on it in a terminal. A second trader (`trader_live`) runs beside the demo stack with your Kraken key. First it runs a tiny canary on each coin to prove orders, stops, fees and latency against Kraken's own records. The account is capped at 100 USD with demo sized caps, 0.5% risk per trade, a 2% daily stop, a 10% drawdown halt and a 50 USD final stop. Demo paper trades exactly the same hashed settings, so the version that passes paper is byte for byte the version that goes live.

## Requirements

**User stories**:
- As the trader owner, I want live trading to start only for a version that passed paper at exactly the live settings and that I signed off in a terminal, so configuration alone can never put money at risk.
- As the trader owner, I want a cheap canary on every coin before any strategy order, so the first real order is not a strategy trade.
- As the trader owner, I want hard live limits (capital cap, daily stop, drawdown halt, final loss stop) that size from at most 100 USD, so a deposit or profit never raises my risk silently.
- As the trader owner, I want every fee and funding payment matched to Kraken's account log, and a halt on anything unexplained, so live P&L never drifts from Kraken's.
- As the trader owner, I want live and demo side by side with separate dashboards and a LIVE banner, so they can never be confused.

**Acceptance criteria**:

*Modes and separation*
- **AC-1** (live modes): `LiveMode` (one `Literal` in `settings.py`) becomes `off`, `drill`, `canary`, `trade`, and `allowed_exec_clients` allows the Kraken execution client for the live trader in `drill`, `canary` and `trade`; `off` refuses it as today. `canary` loads only `live_canary`; `drill` only `reconcile_drill` (unchanged); `trade` loads the strategy named by `LIVE_STRATEGY` on the coins in `LIVE_INSTRUMENTS`. A demo trader ignores `LIVE_MODE`.
- **AC-2** (side by side): on the VPS, `compose.live.yaml` adds two services, `trader_live` and `dashboard_live`, with `TRADING_ENV=live`. They share Postgres, Redis and the recorder with the demo stack, which keeps running unchanged with `TRADING_ENV=demo`. Only `trader_live` mounts the Kraken key files. `trader_live` logs in to Redis as a new ACL user `trader_live` (password as a secret file, like `trader`), limited to `live:*` keys and its Nautilus cache prefix `trader-TRADER-LIVE*`. The demo `trader` user is narrowed from `~*` to `demo:*` and `trader-TRADER-DEMO*` once task 1 has listed every key it uses. `dashboard_live` logs in as the existing `dashboard` user, whose grants are already per pattern. Every row and key the live trader writes carries `env = live`. Starting or stopping `LIVE_MODE` never pauses the demo paper loop.
- **AC-3** (deploy): `scripts/deploy.sh` drains `trader_live` only when it is flat (no open position, no resting entry). It waits up to 30 minutes, then aborts with exit 4 and leaves the old `trader_live` running. `--now` never applies to it. The deploy runs the live version check (AC-9) for `trader_live` when `LIVE_MODE=trade`, and rolls back with exit 4 on a refusal, as the demo check does. Order: recorder, demo trader, `trader_live`, then both dashboards, all `--no-deps`. A `trader_live` restart with a position open relies on spec 0023's start reconcile to keep or replace its stop. Every new setting without a default is added to `/etc/kfs/kfs.env` before the deploy (the runbook step says so).
- **AC-4** (LIVE banner): every page of `dashboard_live` shows a red LIVE banner, and the demo dashboard a DEMO tag. Each dashboard reads and writes only its own env's keys, rows and `{env}:control` stream.

*Canary*
- **AC-5** (canary steps): with `LIVE_MODE=canary`, `live_canary` runs these steps for each coin in `LIVE_INSTRUMENTS`, one coin at a time. It uses the canary size: the larger of the instrument's `min_quantity` and `size_increment`, raised to Kraken's minimum notional if any. Every canary order passes the normal risk guard; nothing is bypassed. A coin whose canary size exceeds `RISK_MAX_ORDER_NOTIONAL_USD`, or whose estimated canary cost (two taker fees plus the spread) exceeds 1 USD, fails at once with no order sent.
  1. Place a post only limit 10% away from the mid of the trader's own latest quote, wait for Kraken's acknowledgement, cancel it, and wait for the cancel acknowledgement.
  2. Open the canary size with a market order, and place its reduce only stop 2% away with a mark price trigger. Confirm through Kraken's open orders call that the stop rests with trigger signal `mark` and the full size.
  3. Close with a market order, cancel the stop, and confirm through Kraken's open positions and open orders calls that the coin is flat with no open order.
  4. Within 3 minutes, find both trades in Kraken's account log with fees that match the fills (AC-13 rules).

  Each step has a 30 s time cap. A failed step stops the canary, flattens, and marks the run `fail`. If the close fails, the canary retries a reduce only market close 3 times, then halts with a critical alert. A Kraken 5xx or a maintenance window marks the run `aborted`, not `fail`; a rerun is a new run row. The trader exits after the last coin, like the drill.
- **AC-6** (canary record): each canary is a `strategy_runs` row (`env = live`, strategy `live_canary`, kind `canary`) with one `run_measures` row per coin and step. It records pass or fail, plus the insert and cancel acknowledgement latency in ms for step 1 and the stop placement latency for step 2. The run passes only if every coin passes.
- **AC-7** (canary prints): the canary prints per coin step results, the median insert and cancel latency, the fees Kraken charged, and the coin's canary size in USD. A `journal export live` for that UTC day lists the canary trades with fees equal to the account log's.

*Sign off and live start*
- **AC-8** (sign off): `gate promote --manual --step paper-live --version <id>` is accepted only from a terminal and only when:
  - (a) the latest gate `paper → live` promotion for that version's frozen hash (by `decided_at`, then `id`) has verdict `pass` (spec 0015 AC-16, thresholds unchanged);
  - (b) a passing canary run covers every coin of the version's instruments, at the installed NautilusTrader version.

  It prints the version, the coins, every `risk_*` value in its config, the live fees, and the canary latency. It then asks you to type the capital cap, the coin list, and the date the spec 0023 live drill passed. On a match it appends a promotions row: `paper → live`, verdict `pass`, `decided_by = manual`, and `confirms_promotion_id` set to that gate pass. `thresholds` holds the approved values. `metrics` holds the gate pass's paper metrics, the canary run id, and the sign off time. Anything else exits 64 and writes nothing.
- **AC-9** (live start check): with `LIVE_MODE=trade`, the live trader refuses to start (exit 1, critical alert `live_start_refused` naming the reason) unless every one of these holds:
  - The running version's frozen hash has a manual sign off whose confirmed gate pass is still the latest gate `paper → live` row for that hash. A later gate row that is not `pass` revokes the sign off.
  - The frozen hash equality covers every hashed `risk_*` value and the coins.
  - `LIVE_MAKER_FEE` and `LIVE_TAKER_FEE` equal the approved fees, which are not hashed.
  - The fee check (AC-14) passes.
  - The Kraken account holds no collateral other than USD.

  `gate check-version` accepts `TRADING_ENV=live` and returns 0 only under the same sign off rule.
- **AC-10** (key): the live key is created with spec 0003's key creation checklist (trade permission, no withdrawal, IP restricted to the VPS). The first `trader_live` start logs `kraken account confirmed` (spec 0003 AC-8).

*Risk*
- **AC-11** (capital cap): with `RISK_CAPITAL_CAP_USD` set, sizing equity is the smaller of account equity and the cap. Account equity is `balance_total` from the trader's Nautilus account state, the same value the existing account recording writes to `account_snapshots`. The cap is applied when read, never stored. Risk per trade, the daily percent stop and the drawdown halt all use this capped equity. This holds in demo and live.
- **AC-12** (daily, drawdown and total stops):
  - **Daily:** with `RISK_DAILY_LOSS_PCT` set, the daily limit is the smaller of `RISK_DAILY_LOSS_LIMIT_USD` and that percent of the capped equity at 00:00 UTC. Hitting it is the existing daily loss halt.
  - **Drawdown:** with `RISK_DRAWDOWN_PCT` set, the trader halts when capped equity falls that far below its high since the start point (the sign off time in live, the run start in demo). The halt reason is `drawdown_limit`; it flattens and sends a critical alert.
  - **Total loss (live):** the spec 0010 limit counts from the later of `RISK_TOTAL_LOSS_SINCE` and the sign off time, so the hashed date can stay equal to demo's.
  - **Halt reasons:** `HaltReason` gains `drawdown_limit`, `ledger_mismatch` and `cost_drift`. A halt key holding a reason the running code doesn't know reads as `unknown`, which stays halted (today's safe behaviour).
- **AC-13** (ledger): the live trader reads Kraken's account log every 60 s and stores each new entry in `account_log_entries`, idempotent per `env`, Kraken id and kind. The match rules:
  - **Polling start:** it polls from the newest stored Kraken id, or from the sign off time if none is stored, so a restart never replays old history.
  - **Fee match:** a trade entry's fee is matched to the `fills` row whose `trade_id` equals the entry's execution id. Task 2 confirms that the adapter stores Kraken's fill id in `trade_id`. Partial fills are separate fills, each with its own log entry.
  - **Funding:** in live, each funding entry creates the live `funding_payments` row, so live funding is never estimated.
  - **Halt rule:** the trader halts with reason `ledger_mismatch` and a critical alert in any of these cases. The clock is the trader's, which is checked at boot.
    - A fill has no log entry once the newest read entry's `booked_at` is more than 60 s past the fill's time.
    - A log trade has no fill 60 s after it was read.
    - A fee differs by more than the larger of 1% of the fee and 0.0001 USD.
    - Any entry is in a currency other than USD.
  - **While flat:** a ledger halt with no open position flattens nothing.
- **AC-14** (fees): live sizes with `LIVE_MAKER_FEE` and `LIVE_TAKER_FEE`, and `SANDBOX_*` fees are set equal so demo pays the same. At start and daily at 00:05 UTC, the live trader reads Kraken's fee schedule and the account's 30 day volume. At start, a Kraken maker or taker rate above the setting refuses the start (reason `fee_above_setting`). During the day it blocks new entries with the same reason and sends a warning alert. The ledger (AC-13) remains the real guard, because promotions or rebates can make the charged rate differ from the tier.
- **AC-15** (log outage): when the account log has not been read successfully for 15 minutes, the live trader blocks new entries (block `account_log_stale`; open positions keep their stops) and sends a warning alert. The block clears on the next successful read once every entry matches.
- **AC-16** (cost drift):
  - **Live cost per trade:** fee bps plus entry slippage bps, both computed as `entry_slippage_bps` does (spec 0015).
  - **Paper baseline:** the sign off row's `paper_slippage_bps` plus the paper record's mean fee bps. Demo fees come from the settings, so this compares live against the model the paper assumed.
  - **Halt:** once 20 live positions have closed, if the mean cost of the last 20 exceeds 1.5 times the baseline, the trader halts with reason `cost_drift` and a critical alert. The window restarts after a review resume.
- **AC-17** (review halts): the trader's control consumer refuses a `resume` from source `dashboard` for a `drawdown_limit`, `ledger_mismatch` or `cost_drift` halt, with result `refused` and reason `needs_cli_review`. That reason is new in the control result format. The consumer accepts one only from the control CLI on a terminal with a typed, non empty reason, which is stored on the `resume` risk event. The review set lives in `trading_core/control/halt.py`. Other halts resume as today.

*Kraken execution*
- **AC-18** (mark price stops): every stop the node bases place uses `TriggerType.MARK_PRICE`. Task 3 first proves the sandbox fires a mark triggered stop on a mark price update. If it cannot, the build stops and returns to `/architect` before any stop code changes. The lab's candle engine keeps trade price stops, because it has no mark series, which is a known small lab to live difference.
- **AC-19** (partial entries): when an entry fills partly, its reduce only stop is placed for the filled size. On each further fill, a new stop for the new total is placed first, and the old one is cancelled after Kraken acknowledges the new one, so the position is never without a stop. At the entry's time limit the unfilled rest is cancelled; a late fill after that cancel resizes the stop once more. This replaces spec 0006's 5 s partial close.
- **AC-20** (dead man's switch): in live, the trader arms Kraken's dead man's switch (timeout 60 s) before placing any order that is not reduce only. It refreshes the switch every 20 s while such an order rests. Once none rests (filled or cancelled), it disarms the switch (timeout 0). If an arm or refresh is not confirmed within 20 s, the trader cancels its resting entries, blocks new entries (block `dead_man_switch_unconfirmed`), and sends a warning alert. The block clears on the next confirmed call. Accepted gap: if the trader dies while an entry rests, the switch also cancels other coins' stops; the restarted trader's reconcile (spec 0023) replaces them. A long VPS or network loss leaves those positions without a stop until it returns.

*Alerts, hashes, latency*
- **AC-21** (alerts): alert rules exist for `live_start_refused`, `drawdown_limit`, `ledger_mismatch`, `cost_drift` (critical), and `account_log_stale`, `dead_man_switch_unconfirmed`, `fee_above_setting` (warning). Each live fill arrives as a silent push with side, size, price and fee (spec 0013 AC-8) through the existing alerter, with LIVE in the title.
- **AC-22** (hash record): `verify.md` lists every registered strategy's code and frozen hash before task 3 and after each of tasks 3, 4 and 5, all at the same settings. Each move is explained by that task's change and nothing else. Adding the new `risk_*` settings moves no hash while they are unset. Setting the demo to the live values (task 5) is a separate, listed change.
- **AC-23** (latency): `DEFAULT_LATENCY_MS` in `apps/lab/src/lab/candle_parity.py` becomes the canary's measured median insert latency, rounded up to the next 5 ms. It stays 25 until a canary has passed. A run file's `[parity].latency_ms` still overrides it.

## Decision

**Chosen option**: Option 1: Build the live machinery now, demo trades the exact live config, and live trading unlocks only with a gate pass plus your typed sign off.

Live runs as `trader_live` beside the demo stack. A per coin canary proves the Kraken path first, Kraken's account log is the source of truth for fees and funding, and the live limits size from at most 100 USD.

## Feature design

**Data model sketch**:

| Entity | Key fields | Notes |
|---|---|---|
| `account_log_entries` (new) | `id` uuid PK · `env` text · `kraken_log_id` text, required · `kind` text (`fee`, `funding`, `realized_pnl`, `transfer`, `other`) · `instrument_id` FK `instruments`, null for transfers · `amount` numeric, required · `currency` text, required · `booked_at` timestamptz, required · `execution_id` text, null (Kraken's fill id on trade entries) · `fill_id` FK `fills`, null · `funding_payment_id` FK `funding_payments`, null · `matched_at` timestamptz, null · `created_at` | Unique (`env`, `kraken_log_id`, `kind`): one Kraken entry carrying a fee and a realized P&L becomes two rows. Index on (`env`, `matched_at`) for the unmatched scan. Many entries to one fill or one funding payment. Kept forever (small, money record). |
| `promotions` (existing) | new column `confirms_promotion_id` FK `promotions`, null | The check `no_manual_live` becomes `decided_by = 'gate' OR to_stage <> 'live' OR confirms_promotion_id IS NOT NULL`, plus a check that only a manual row may set `confirms_promotion_id`. `gate` code checks the confirmed row is a gate `paper → live` pass for the same frozen hash. No manual only route to live, as spec 0015 intends. |
| `strategies` / `strategy_runs` / `run_measures` (existing) | `live_canary`, kind `canary` | Migration replaces `ck_strategies_kind_valid` to add `canary`. Run status adds `aborted` if the status check needs it. Measure names: `<coin>.<step>.pass`, `<coin>.insert_ms`, `<coin>.cancel_ms`, `<coin>.stop_ms`. |
| `risk_events` (existing) | new halt and block reasons | `drawdown_limit`, `ledger_mismatch`, `cost_drift` (halts), `account_log_stale`, `dead_man_switch_unconfirmed`, `fee_above_setting` (blocks), `live_start_refused`. Task 1 lists the column checks, and the migration widens each one that constrains these values. |
| `HaltReason` (Redis halt key, JSON) | three new values | Unknown values read as `unknown` and stay halted. |
| High water mark | none stored | Max of min(`balance_total`, cap) in `account_snapshots` for this env since the start point. |

One migration: the new table, the promotions column and checks, the strategy kind check, and the widened checks.

**State transitions** (`LIVE_MODE` on the VPS, set by you in `/etc/kfs/kfs.env`):
`off` → `drill` (spec 0023 drill passes) → `canary` (all coins pass) → `off` → (gate pass, sign off) → `trade`. Within `trade`, the trader is `trading` ↔ `blocked` (stale log, unconfirmed switch, fee above setting; clears by itself) or `halted` (daily, total, drawdown, ledger, cost drift, kill switch). Review halts leave `halted` only by a CLI resume with a reason.

**Interface surface**:

| Surface | Kind | Key inputs | Key outputs | Who | Key errors |
|---|---|---|---|---|---|
| `gate promote --manual --step paper-live --version <id>` | CLI, VPS terminal | version id; typed cap, coins, drill date | promotions row id | you, TTY only | 64: no gate pass, no canary pass, mismatch, no TTY |
| `gate check-version` | CLI, deploy | `trader.version` JSON, `TRADING_ENV` | exit 0 / 3 / 1 | `kfs-deploy` | 3 without a valid sign off for that hash |
| `python -m dashboard.control_cli resume --reason "<text>"` | CLI, VPS terminal | reason | control result | you | refused: empty reason |
| `trader_live` start | process | settings, key files, Kraken | `live_start_refused` or running | Compose | exit 1 on any AC-9 check |
| Kraken dead man's switch | signed REST POST | `timeout` s | current and trigger time | trader | not confirmed in 20 s → AC-20 |
| Kraken account log | signed REST GET | from last id | entries | trader | unreadable 15 min → AC-15 |
| Kraken open orders, open positions | signed REST GET | none | orders with trigger signal and size; positions | canary | step fails |
| Kraken fee schedules and volumes | REST GET (volumes signed) | none | tiers, 30 day volume | trader at start and daily | above setting → refuse or block |
| Kraken accounts | signed REST GET | none | collateral per currency | trader at start | non USD collateral → refuse |

Endpoint paths and the signing scheme come from Kraken's Futures API docs in task 1, and are pinned in the client's tests. The client is a thin module in `apps/trader` that uses `urllib.request`, like the seed fetch, and reads the key through `trading_core.secrets`. It never logs the request body or the signature.

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| Sizing | account equity | `balance_total` of the trader's Nautilus account state (Kraken in live, sandbox in demo), as the existing account recording writes it |
| Sizing | capped equity | min(account equity, `RISK_CAPITAL_CAP_USD`), computed on read |
| Daily stop | day start equity | capped value of the first `account_snapshots` row of the UTC day |
| Drawdown halt | high water mark | max capped value in `account_snapshots` since the start point |
| Drawdown and total loss | start point | live: the sign off row's `decided_at`; demo: the run's `started_at` |
| Ledger match | match key | `fills.trade_id` = `account_log_entries.execution_id` |
| Ledger match | fill fee | `fills.fee` (the trader's own figure) vs the log fee |
| Cost drift | live cost | fee bps + `entry_slippage_bps` per closed position (spec 0015 formula) |
| Cost drift | paper baseline | sign off `metrics.paper_slippage_bps` + paper mean fee bps |
| Fee check | Kraken's rate | fee schedule tier for the account's 30 day volume |
| Sign off | drill date | typed by you (spec 0023 records the pass only in its `verify.md`) |
| Canary | mid | the trader's latest quote for the coin |
| Canary | canary size | instrument `min_quantity` and `size_increment`, raised to Kraken's minimum notional |
| Lab latency | new default | median `insert_ms` of the latest passing canary run, written into `candle_parity.py` by a commit |
| Banner | env | the dashboard's own `TRADING_ENV` |

**Key invariants**:
- No order reaches Kraken from a strategy unless `LIVE_MODE=trade` and every AC-9 check passed at start.
- The version trading live has the same frozen hash as the version that earned the gate's paper pass. Demo runs that exact hashed config.
- A manual live row always confirms a gate pass for the same frozen hash.
- Sizing never uses more than `RISK_CAPITAL_CAP_USD` of equity.
- A stop rests on Kraken for every filled live size. A resize places the new stop before cancelling the old.
- The dead man's switch is armed before any order that could open or add to a position.
- New `risk_*` settings appear in the version config only when set.

**Security model**:
- Only `trader_live` mounts `kraken_api_key` and `kraken_api_secret`. The key can trade but cannot withdraw, and is IP restricted (spec 0003).
- The `trader_live` Redis user reaches only live keys, and the demo `trader` user only demo keys.
- The sign off and review halt resumes need a VPS terminal (Tailscale SSH), never the dashboard.
- `dashboard_live` sits behind the same TOTP login as the demo dashboard (spec 0004), on its own port, and writes only to `live:control`.
- Logs never print key values (`trading_core.secrets` redaction), signed bodies, or signatures.

**Configuration required** (live values in `/etc/kfs/kfs.env`, added by hand before the deploy):
- `LIVE_MODE`: `off` | `drill` | `canary` | `trade`.
- `LIVE_STRATEGY`, `LIVE_INSTRUMENTS`: the strategy and coins `trade` runs. They must give the signed off frozen hash.
- `LIVE_MAKER_FEE`, `LIVE_TAKER_FEE`: the fees live sizes with; the demo's `SANDBOX_MAKER_FEE` and `SANDBOX_TAKER_FEE` are set equal.
- New, in both envs: `RISK_CAPITAL_CAP_USD` = 100, `RISK_DAILY_LOSS_PCT` = 0.02, `RISK_DRAWDOWN_PCT` = 0.10.
- Existing, at live values in both envs (they are hashed, so demo and live must match):
  - `RISK_PER_TRADE` = 0.005
  - `RISK_MAX_ORDER_NOTIONAL_USD` = 25
  - `RISK_MAX_TOTAL_EXPOSURE_USD` = 50
  - `RISK_MAX_LEVERAGE` = 2
  - `RISK_TOTAL_LOSS_LIMIT_USD` = 50
  - `RISK_MAX_TRADES_PER_DAY` = 50
  - `RISK_LOSS_COOLDOWN_S` = 600
  - `RISK_TOTAL_LOSS_SINCE` (the same date in both)
  - `SANDBOX_STARTING_BALANCE_USD` = 100
- `LIVE_ACCOUNT_LOG_POLL_S` = 60, `LIVE_ACCOUNT_LOG_STALE_S` = 900, `LIVE_DMS_TIMEOUT_S` = 60, `LIVE_DMS_REFRESH_S` = 20.
- `DASHBOARD_LIVE_PORT`: the live dashboard's port on the Tailscale interface.
- Secrets: `kraken_api_key`, `kraken_api_secret` (spec 0003), plus the `trader_live` Redis password file.

**Critical test scenarios**:
- Happy path: on the VPS the BTC canary passes with every step and latency recorded, the sign off is written, and a `trade` start passes every check. Verifies **AC-5**, **AC-6**, **AC-8**, **AC-9**.
- Failure: a gate rerun records `fail` for the signed off hash, so the next `trade` start is refused with `live_start_refused`. Verifies **AC-9**.
- Failure: a fake account log returns a fee 5% off the fill, so the trader halts with `ledger_mismatch`. Verifies **AC-13**.
- Failure: the switch refresh times out with an entry resting, so the entry is cancelled and entries are blocked, then unblocked on the next success. Verifies **AC-20**.
- Failure: a 40% partial fill gets a stop for 40%; the next fill places a 100% stop before the 40% one is cancelled. Verifies **AC-19**.
- Database: inserting a manual `paper → live` row without `confirms_promotion_id` fails the check. Verifies **AC-8**.
- Permission: a dashboard `resume` for a drawdown halt is refused with `needs_cli_review`. Verifies **AC-17**.
- Separation: the demo trader has no Kraken key mounted, its allowlist holds only the sandbox, and its Redis user cannot read `live:*`. Verifies **AC-1**, **AC-2**.

## Build plan

Tracer Bullet: task 1 is a thin real thread from settings to Kraken to Postgres. A BTC canary on the VPS proves the live service, the key, the env guard, the signed client and the records together. Each later task thickens one segment. Build after the demo week review (2026-11-12 08:08 UTC), because tasks 3 to 5 move the demo strategy's hash.

1. **Thin thread: BTC canary on live.**
   - Migration: the new table, the promotions column and checks, kind `canary`, and the widened checks. List each check that names the new values first.
   - `LiveMode` values and the env guard.
   - `trader_live` in `compose.live.yaml`, with the `trader_live` Redis user and password secret. List the demo trader's key patterns, then narrow its user.
   - `kfs-compose` and `deploy.sh` handling of the second trader (flat only drain, 30 minute limit).
   - The signed REST client (open orders, open positions, account log, accounts) with signing tests.
   - `live_canary` with steps 1 to 4, its caps and time limits, and its `run_measures`.
   - Then, once you fund the wallet: the spec 0023 live drill in `trader_live`, and the BTC canary on the VPS.

   Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-5**, **AC-6**, **AC-10**.
2. **Ledger and fees.** Confirm what the adapter puts in `fills.trade_id`. Then build the account log polling and storage, fill and funding matching, the `ledger_mismatch` halt, the `account_log_stale` block, the fee schedule check at start and daily, `LIVE_*_FEE` sizing, and the USD only collateral check. Satisfies **AC-13**, **AC-14**, **AC-15**, **AC-7**.
3. **Mark price stops.** Start with the sandbox spike; if mark triggers do not fire, stop and return to `/architect`. Then switch the node bases to `TriggerType.MARK_PRICE`, and record the hashes. Satisfies **AC-18**, **AC-22**.
4. **Partial entries.** Size the stop to the filled part, placing the new stop before cancelling the old. Cancel the rest at the time limit, and resize again on a late fill. Record the hashes. Satisfies **AC-19**, **AC-22**.
5. **Live risk.** Build the following, then record the hashes. Set the demo to the live values, and promote the new demo version by hand.
   - `RISK_CAPITAL_CAP_USD`, `RISK_DAILY_LOSS_PCT`, `RISK_DRAWDOWN_PCT` (left out of the version config when unset).
   - The new `HaltReason` values and the drawdown halt.
   - The live total loss start point.
   - The CLI only resume for review halts, with `needs_cli_review`.

   Satisfies **AC-11**, **AC-12**, **AC-17**, **AC-22**.
6. **Dead man's switch.** Arm before an entry, refresh, disarm, and the unconfirmed block. Satisfies **AC-20**.
7. **Sign off and live start.** `gate promote --manual --step paper-live` with the typed confirm, `LIVE_MODE=trade` with `LIVE_STRATEGY` and `LIVE_INSTRUMENTS`, the AC-9 start checks with revocation, `check-version` for live, and the cost drift halt. Satisfies **AC-8**, **AC-9**, **AC-16**, **AC-3**.
8. **Live dashboard and alerts.** `dashboard_live`, the LIVE banner and DEMO tag, the alert rules, and the live fill pushes. Measure the VPS memory with both stacks up for 24 h. Satisfies **AC-4**, **AC-21**, **AC-2**.
9. **Close out.** Run the canary on every coin of the demo strategy's set, the live journal export, and the lab latency default. Update the restart runbook for `trader_live`, and write `verify.md`. Set `LIVE_MODE=off` until a version earns its pass. Satisfies **AC-5**, **AC-7**, **AC-22**, **AC-23**.

## Consequences

**Positive**:
- No strategy can trade live by configuration alone. It needs a gate pass at the exact live config plus your terminal sign off, and a later failing gate run revokes it.
- The first real orders are canary orders that cost cents, and they measure latency and fees the lab has only guessed so far.
- Demo keeps running beside live, so paper and live can be compared every day.
- Kraken's account log becomes the record for fees and funding, which the journal export and tax work can trust.

**Negative / tradeoffs**:
- Nothing trades live after this ships until a version passes paper, and none does today. This is the machinery, not a strategy.
- Demo must paper trade at 100 USD with 25/50 USD caps. Small sizes make paper stats noisier, and the paper record restarts for every version at these settings (30 days and 100 trades for a scalper).
- Tasks 3 to 5 change the shared node bases and the demo settings, so every strategy's hash moves and the running demo version needs a manual promote.
- Accepted gap: a trader death while an entry rests lets Kraken's switch cancel other coins' stops. Usually it's back within seconds, but a long VPS or network outage leaves those positions without stops.
- Mark price stops differ from the lab's trade price stops, a small lab to live drift the cost drift check only partly watches.
- Two traders and two dashboards on a 4 GB VPS. Memory is measured in task 8; if it is short, the recorder's extras go first.
- A 25 USD order cap with a tight stop often clips the 0.5% risk, so live trades usually risk less than the setting says.
- An own signed REST client is new code touching the live key. Its signing must match Kraken's docs exactly, and Kraken's API changes are ours to track.
- A USD only wallet: deposits in other currencies must be converted before live starts.

**Neutral**:
- `LIVE_MODE=drill` no longer pauses the demo loop, because the drill now runs in `trader_live`.
- Spec 0015's "no manual live" rule is narrowed, not dropped: a manual live row must confirm a gate pass.
- The live journal export (spec 0024) starts returning rows.

## Follow-up

- [ ] Spec 0015: note the narrowed `no_manual_live` check (a manual live row must confirm a gate pass).
- [ ] Spec 0023: mark its follow-up "extend `LIVE_MODE` with `trade`" as handled here, and note that the drill now runs in `trader_live`.
- [ ] Spec 0013: the live fill verify (AC-8) runs at the first live strategy fill, not at the canary.
- [ ] Scaling capital or leverage above these values needs a new risk decision and a new sign off (scope Deferred: Scale up).
- [ ] Spec 0025's follow-up "two coins on live is a decision for feature 19" is decided here: live trades the strategy's coin set, with each coin canaried first.
- [ ] A running result drift halt (live net per trade vs the lab band) was considered and left out. Revisit after the first 30 live trades.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
