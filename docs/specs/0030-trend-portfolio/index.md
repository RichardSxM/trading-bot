# 0030. trend_portfolio: a weekly 30 day trend portfolio on a second demo trader, beside six_vote_trend

**Date**: 2026-11-07 (revised the same day after a cross check by a second model: per trader pause key and result readers, rebalance on resume, the portfolio's own risk caps, dashboard reads per trader, the frozen file list; amended 2026-11-09: AC-3 lets the demo run at about 10x by your choice, with AC-1's tested values kept as the rule's defaults, and AC-23 allows a one step order on resume)
**Status**: In Progress

## Summary

`trend_portfolio` holds all 12 demo coins at once, long a coin whose price is above where it was 30 days ago and short one below it. Each position is sized so a calmer coin gets a bigger share (volatility scaling), and the whole book never uses more than a set multiple of the account's equity: 2 times in the tested method, and 10 times on the demo since 2026-11-09, by your choice. It trades once a week, Monday 00:05 UTC (01:05 Amsterdam), with market orders. Unlike every earlier idea, it held up on data it never saw: Binance 2018 to 2022 gave a Sharpe of 1.36 on these 12 coins, every year positive, including the 2018 crash (see `rationale.md`). That evidence is for the tested settings only. A run of the same backtest at 10 times (pre check of 2026-11-07) was wiped out on 2018-04-12, so the book on the demo today says nothing about it, in either direction.

It runs **beside** `six_vote_trend`, not instead of it, as a second trader service with its own sandbox account (a simulated account, since Kraken has no futures demo; spec 0001). Six vote keeps its keys, version, and behavior. A new trader "slot" setting gives the second trader its own keys for pause, halt, snapshots, and commands, and the dashboard, alerter, deploy script, and loss limits learn to tell the two apart. One stop button still stops both. A new Portfolio page shows the book. It goes straight to the demo as a forward test (a test on live prices with simulated money); a lab study and gate verdict are owed before any live money. Most of the work is this plumbing, not the strategy, and the build does the strategy first.

## Requirements

**User stories**:
- As the trader, I want a slow trend portfolio across the 12 coins, rebalanced weekly, so that I trade the one idea that survived out of sample tests, with low fees.
- As the trader, I want it to run beside six vote with its own money, so that I can compare the two and neither disturbs the other.
- As the operator, I want one stop button to stop both, and alerts when either trader goes silent or a rebalance fails, so that safety does not get weaker with two traders.
- As the trader, I want a Portfolio page with each coin's signal, target, position and P&L, plus the account summary, so that I can follow it at a glance.

**Acceptance criteria**:

*Part A: the strategy*

- **AC-1** (the rule): a new pure module `trading_core/strategy/trend_portfolio.py` with `portfolio_plan(closes, equity, params) -> PortfolioPlan`. `closes` maps each coin to its daily closes (oldest first, one per UTC day, each with its date, `Decimal`); `equity` is a `Decimal` in USD; `params` holds `lookback_days` (30), `vol_days` (30), `vol_target` (0.02), `gross_cap` (2), `min_history_days` (120). These are the rule's defaults and the tested values; the demo trader overrides `vol_target` and `gross_cap` (AC-3, amended 2026-11-09). Definitions pinned to the backtest (`slow_ideas.portfolio()` and `old_data.portfolio()`, see `rationale.md`):
  - A coin is **eligible** when it has at least `min_history_days` closes (which also covers the 31 closes the signal and the volatility need).
  - **signal** = sign(close[t] / close[t − 30] − 1): +1, −1, or 0 when exactly equal.
  - **vol** = the sample standard deviation (divisor n − 1) of the last 30 simple daily returns (close[i] / close[i − 1] − 1).
  - **raw weight** = signal × `vol_target` / vol. A coin with vol 0 gets weight 0 and still counts as eligible.
  - **N** = the number of eligible coins. **weight** = raw weight / N.
  - **gross** = the sum of |weight|. When gross > `gross_cap`, every weight is multiplied by `gross_cap` / gross (`gross_scale`, else 1).
  - **target notional** = weight × equity (signed USD). A coin that is not eligible gets target 0.
  - `PortfolioPlan` holds per coin: eligible, signal, vol, weight, target notional, and the close date used; plus N, gross before scaling, `gross_scale`, equity, and the plan's close date. All arithmetic is `Decimal` (`Decimal.sqrt` at 28 digit precision). Equity ≤ 0 raises a typed error. No I/O, no Nautilus import.
- **AC-2** (quantity and delta): a pure helper in the same module, `order_delta(target_notional, mid, current_qty, size_increment, min_quantity)`. It computes target quantity = target notional / mid, rounded toward zero to `size_increment`, then delta = target quantity − current signed quantity. A delta whose size is below max(`size_increment`, `min_quantity` when set) returns `None` (skip), and the node logs INFO `rebalance_skip_small`.
- **AC-3** (the node): a new node `trading_core/node/trend_portfolio.py` with `TrendPortfolio(KfsStrategy)` and `TrendPortfolioConfig(KfsStrategyConfig)`.
  - `STRATEGY_NAME = "trend_portfolio"`, `uses_stop = False` (no protective stop; spec 0023 AC-9). **One instance trades every coin**, with one `order_id_tag` (`"TP"`).
  - Config: `instruments` (the sorted coin list), `instrument_id` = BTC (used only as the base's context instrument), AC-1's params with `vol_target` 0.31 and `gross_cap` 10 (amended 2026-11-09: your choice of about 10x for the demo, PR #135, instead of the tested 0.02 and 2), `rebalance_weekday` 0 (Monday), `run_at_utc` "00:05", `retry_max` 3, `retry_wait_s` 60, `quote_wait_s` 60, `start_cooldown_s` 3600, `seed_days` 150, `seed_min_days` 121.
  - For every coin it subscribes to trade ticks, quote ticks, and `BarType.from_str(f"{coin}-1-DAY-LAST-INTERNAL")` (daily bars built from trades). It keeps each coin's daily closes keyed by the bar's UTC date (`ts_event` minus one day, since a daily bar closes at 00:00 of the next day), seed plus live, at most `seed_days`.
  - It meets the `KfsStrategy` contract over all coins:
    - `flatten` and `flatten_for` cancel every retry timer and every pending flip open, then close each open position with a reduce only market order.
    - `has_open_positions` is true when any coin is held or any order is open.
    - `on_stop` cancels all timers and calls `super()`.
    - `resume_position` is called once per position.
    - It checks `fresh_signal` before acting.
  - A bad value refuses at config build time: `lookback_days` < 2, `vol_days` < 2, `vol_target` ≤ 0, `gross_cap` ≤ 0, `min_history_days` < max(`lookback_days`, `vol_days`) + 1, `rebalance_weekday` outside 0 to 6, `run_at_utc` not HH:MM, `retry_max` < 0, `retry_wait_s` < 1, `start_cooldown_s` < 0, or an empty coin list or one without BTC.
- **AC-4** (schedule): a clock timer fires every day at `run_at_utc` (00:05 UTC). The first fire after a start is the next 00:05.
  - Each run plans from closes up to the day that just ended, logs INFO `portfolio_targets` once per coin (coin, close date, eligible, signal, vol, weight, target notional, current quantity), and stores the plan for the `portfolio_plan` snapshot.
  - On `rebalance_weekday` it also rebalances (reason `weekly`), at most once per date: the node keeps the date of its last weekly rebalance, and a second fire on the same date does nothing.
  - **Start**: once the seed has landed and every eligible coin has a quote (waiting at most `quote_wait_s`, then going ahead without the coins that have none, WARNING `no_quote` each), it plans from closes up to yesterday and rebalances (reason `start`). It skips with INFO `start_rebalance_cooldown` when a `start` decision row of this trader exists less than `start_cooldown_s` ago in Postgres (a crash loop guard). The book then waits for the next run.
  - **Resume**: when the pause gate goes from paused or halted to clear (an `unpause` or `resume` command), it rebalances (reason `resume`) with the latest stored plan, so a deploy pause or a stop never leaves the book empty until Monday.
  - A coin whose last close is not from the plan's day (the bar was late or missing) keeps its current position that run, with WARNING `close_missing`. If no coin can be planned, or equity is missing or ≤ 0, the run logs WARNING `plan_empty` or `equity_unknown` and sends nothing.
- **AC-5** (orders): per rebalance, all reducing orders go first, then all orders that open or grow. Each coin gets one market order, reduce only whenever it only reduces (a smaller size on the same side, or target 0).
  - **Flip** (long to short or back): a reduce only market close of the whole position. When the close fills, the open leg is sized again from the same plan's target notional at the current mid and goes through the check below.
  - **Check before every open or grow**: a new pure check `trading_core/risk/rebalance.py` `check_rebalance` applies the trader's `RiskLimits`:
    - refuse when the quote is older than `RISK_STALE_QUOTE_S`;
    - clip the order down to `RISK_MAX_ORDER_NOTIONAL_USD`;
    - clip it down so the book's total exposure (every open position at its mid, plus this order) stays within `RISK_MAX_TOTAL_EXPOSURE_USD`, and its leverage within `RISK_MAX_LEVERAGE`;
    - refuse when the position's liquidation distance would be under `RISK_MIN_LIQ_DISTANCE_PCT` (the existing `liquidation_price` and `distance_pct` helpers).

    A clip logs INFO `rebalance_clipped` with both sizes. Reduce only orders skip the check: reducing is always allowed.
  - **Held**: while the pause gate is paused or halted, or an entry block is set (for example the trades per day cap), orders that open or grow are not sent (INFO `rebalance_held` with the reason), and reductions still go. A resume rebalances again (AC-4).
  - **Denied**: a denial by the trader's order guard (`node/guard.py`) is handled like a rejection (AC-6).
- **AC-6** (failures): an order that is rejected, denied, or refused for a stale quote is retried for that coin up to `retry_max` (3) times, `retry_wait_s` (60 s) apart. Each retry recomputes the delta from the same plan, the current position, and the current mid. After the last try the coin goes into the plan's `failed` list with WARNING `rebalance_coin_failed`, and it stays there until the next rebalance. The other coins trade as normal. A liquidation distance refusal is not retried. A new rebalance cancels pending retries. A quote that is still missing after `quote_wait_s` at start counts as `no_quote` (AC-4), not as a failure.
- **AC-7** (decisions): each rebalance writes one decision row per coin with a delta, through the same record path six vote uses: strategy `trend_portfolio`, instrument the coin, outcome approved or refused with the reason (`held:<reason>`, `stale_quote`, `liq_distance`, or the guard's reason). The order carries the decision's tag, as six vote does, so fills link back. The inputs hold:
  - reason (`weekly`, `start`, or `resume`) and the close date;
  - signal, vol, weight, N, gross, `gross_scale`;
  - equity, target notional, current quantity, target quantity, delta, the mid;
  - any clip.

  Small skips are logged only and get no row.
- **AC-8** (warmup seed): `trader/warmup.py`'s interval parameter (spec 0029 AC-8) gains `1d`. For `trend_portfolio` the trader fetches the last `seed_days` (150) closed daily candles per coin from `/api/charts/v1/trade/<symbol>/1d`.
  - A candle counts as closed when its open time plus one day is at or before now. The same retry and failure rules apply (spec 0025 AC-7 to AC-10).
  - A coin with fewer than `seed_min_days` (121) closes logs `warmup_seed_failed` and stays not eligible until live bars fill it up.
  - A seed whose last close is not from yesterday (UTC) is dropped with `seed_stale`.
  - The first live daily bar after a start is partial in high, low, and volume, but its close is the true close of that day, and only closes are used, so it is kept.
  - The seed never trades or records a decision.
  - `trend_portfolio` joins `SEEDED_STRATEGIES` and the multi coin list, and its seeds go to its single instance as a map per coin.
- **AC-9** (restart and resume): on the demo, a start is always flat (the demo cache purge, spec 0006 AC-15), so AC-4's start rebalance rebuilds the book. `resume_position` adopts any position found at start as that coin's current quantity, with no stop to rest.
- **AC-10** (paper wiring and version): `PAPER_STRATEGY` accepts `trend_portfolio` with more than one coin.
  - `trader/main.py` builds **one** instance for it instead of one per coin.
  - Its snapshot view is a new `TrendPortfolioView` that gives per coin rows in the existing `positions` snapshot shape: quantity and side, average price, unrealized P&L, and `exiting`, `position_opened_ns`, `exit_due_ns`, `stop_order` (the last three null). It also gives the plan for `portfolio_plan`.
  - `paper_version` records name `trend_portfolio`, kind `trend` (`StrategyKind.TREND`, spec 0027), and a config holding every AC-3 setting plus `instruments`.
  - Outside `TRADING_ENV=demo` the config builder refuses.
  - Task 2 confirms that `gate promote --manual --step candidate-paper` accepts a `trend` version, as it did for six vote's `hour_level`. If it does not, stop and come back to `/architect`.
- **AC-11** (frozen modules): no file in the version closure of any registered strategy is edited. The closure is what `trading_core.versioning.code_closure` lists. For six vote today it includes:
  - `control/halt.py`, `control/pause.py`, `control/messages.py`;
  - `recording/enums.py`, `recording/rows.py`, `recording/mappers.py`, `recording/spool.py`;
  - `settings.py`, `secrets.py`;
  - `node/strategy.py`, `node/guard.py`, `node/stale.py`, `node/bar_rule.py`, `node/bar_rule_maker.py`;
  - every `risk/` and `strategy/` module it imports.

  So the slot work goes into **new** modules and into files outside every closure (`node/control.py`, `control/consumer.py`, `recording/queries.py`, `recording/repository.py`, the app packages). A hash baseline (every registered version's code hash plus the closure file list) is taken first. At close out every existing version's hash is unchanged, and the new lines are only for new modules. If a change can only go into a closure file, stop and come back to `/architect`.

*Part B: a second trader*

- **AC-12** (trader slot): a new pure module `trading_core/control/slots.py` (new, so it is in no existing closure). It defines `TraderSlot` (`main`, `portfolio`) and the names per slot:
  - `pause_key`;
  - `halt_key`;
  - `snapshot_name(base)`;
  - `control_group`;
  - `consumer_name`;
  - `result_slot_field`.

  For `main` each one returns exactly today's value, by calling the existing functions in `halt.py` and `pause.py` (a test pins every `main` name). For `portfolio` the values are:
  - `{env}:trader:portfolio:pause`;
  - `{env}:trader:portfolio:halt`;
  - `portfolio_<base>`;
  - `trader-portfolio`;
  - the `slot=portfolio` field.

  The setting `TRADER_SLOT` (default `main`) lives in the trader app's `TraderSettings` (not in core `settings.py`). `TRADER_SLOT=portfolio` refuses to start outside `TRADING_ENV=demo`, and refuses unless `PAPER_STRATEGY=trend_portfolio`. Every place that builds one of these names in files outside the closures (`trader/main.py`, `trader/cache.py`, `node/control.py`, `control/consumer.py`, the snapshot writer) takes it from the slot.
- **AC-13** (snapshots and reconcile): the `portfolio` slot writes `portfolio_heartbeat`, `portfolio_trader_state`, `portfolio_positions`, and `portfolio_account`, plus the new `portfolio_plan`. The snapshot writer refreshes `portfolio_plan` on the same timer and TTL as the others, from the node's last stored plan, so it never expires between weekly runs. The portfolio trader runs its own reconcile against its own sandbox, but it never publishes the shared `exchange` snapshot (its `ReconcileActor` gets no `publish`). `trader.cache reset` and the demo cache purge check the slot's own heartbeat (`heartbeat_alive` gets the slot's name) and delete only their own `TRADER_ID`'s keys.
- **AC-14** (commands reach both, results kept apart):
  - **The portfolio's group**: the `portfolio` slot reads `{env}:control` in its own consumer group `trader-portfolio`, so every command reaches both traders. On first creation the group starts at `$` (new entries only), so old commands are never replayed. `main`'s group is unchanged.
  - **Results**: the consumer adds `slot=portfolio` to the fields it gets from `result_to_fields` before appending, so `messages.py` is not edited.
  - **Commands the portfolio refuses**: it executes `stop`, `resume`, `pause`, and `unpause`. Any other command (the reconcile `flatten` and `drill-step`, say) gets the existing refusal outcome with reason `not_for_slot`.
  - **Every result reader takes a slot.** Readers ignore results whose slot is not theirs, and an entry with no `slot` field is `main`'s. The readers are:
    - the control CLI's `_final_outcome` and `send_and_wait` (with a new `--slot main|portfolio|all` flag, default `main`);
    - the dashboard poller's command state;
    - `web/app.py`'s results reads.

    With `--slot all` the CLI waits for a final result from every slot whose heartbeat is fresh.
  - **Migration**: `control_commands.slot` (text, not null, server default `main`, check `main` or `portfolio`); the primary key `control_commands_pkey` becomes (`id`, `slot`). The downgrade deletes the `portfolio` rows, drops the column, and restores the old key. `CommandRecorder.outcome` and the recorder writes take the slot, so `duplicate` reads only its own slot's row.
- **AC-15** (pause and halt per trader, one button): each trader has its own pause key and halt key (AC-12).
  - A `stop` halts and flattens both, each through its own keys.
  - `resume`, `pause`, and `unpause` reach both.
  - A loss limit or reconcile mismatch halt affects only the trader that tripped it.
  - A restart of one trader never inherits the other's pause.
- **AC-16** (reads per trader):
  - `loss_seed` gains a `trader_id` parameter and counts only that trader's runs (`strategy_runs.trader_id`); its `daily_loss_limit` halt lookup is scoped through the run too. The caller in `node/loss_limits.py` passes its own `TRADER_ID`.
  - The dashboard's P&L (`dashboard/data/pnl.py`), Overview, Trades page, and round trips read only `main`'s `TRADER_ID` (a new dashboard setting `DASHBOARD_MAIN_TRADER_ID`, which must match `trader`'s), so six vote's figures stay clean.
  - The trade journal export (feature 18) keeps every paper trade and gains a `trader` column.
  - The gate's run reads are by version, so they are already per strategy and are unchanged.
  - The `RISK_*` limit values are applied to each trader on its own.
- **AC-17** (alerts): a new alerter setting `ALERTER_TRADER_SLOTS` (default `main`; the VPS sets `main,portfolio`). For each listed slot the alerter runs its heartbeat and entry block checks on that slot's keys, and its alert text names the slot. For `portfolio` it also sends one alert per rebalance when `portfolio_plan.failed` is not empty, naming the coins, deduped by the plan's `last_rebalance_at`.
- **AC-18** (compose and deploy):
  - **The service**: a new compose service `trader-portfolio`, using the trader image, under the compose profile `portfolio`, with:
    - `TRADER_SLOT=portfolio`, its own `TRADER_ID`, and `PAPER_STRATEGY=trend_portfolio` fixed in the service;
    - `PAPER_INSTRUMENTS` from the same variable as `trader`;
    - its own spool volume, the same secrets and database and Redis users, and `compose.vps.yaml`'s catalog bind.
  - **Its own risk values**, read from `PORTFOLIO_*` variables with defaults sized for a 2× book on a 1000 USD sandbox (the VPS sets its own values for the about 10x demo, see the `PORTFOLIO_RISK_*` line under configuration): `RISK_MAX_TOTAL_EXPOSURE_USD` `${PORTFOLIO_RISK_MAX_TOTAL_EXPOSURE_USD:-5000}`, `RISK_MAX_ORDER_NOTIONAL_USD` `${PORTFOLIO_RISK_MAX_ORDER_NOTIONAL_USD:-1000}`, `RISK_MAX_LEVERAGE` `${PORTFOLIO_RISK_MAX_LEVERAGE:-2.5}`, `RISK_MAX_TRADES_PER_DAY` `${PORTFOLIO_RISK_MAX_TRADES_PER_DAY:-100}`. Its loss limit and cooldown lines read the same variables as `trader` (off on demo today). Task 3 checks that the Redis ACL lets the trader user write the new key names, and adds the narrowest pattern if it does not.
  - **`kfs-deploy`**, when the `portfolio` profile is on (`COMPOSE_PROFILES` contains it):
    - it runs the promotion check for `trader-portfolio`'s version too, with `compose_new run --rm --no-deps -T trader-portfolio python -m trader.version`;
    - the deploy pause and unpause use `--slot all` and wait for both results;
    - `kfs_unpause_if_ours` lifts the pause only for slots whose `trader_state` shows the `deploy` source;
    - it waits for flat only on `main` (the portfolio is never flat by design: it is paused, then rebuilt at its restart, AC-4 and AC-9);
    - its health gate checks `portfolio_heartbeat`'s `git_commit` too, and a portfolio failure rolls back the whole release, like any other failed service (one image, one release).

    With the profile off, nothing about deploy changes. The profile is turned on only after a release that has the service is current (AC-22), so a rollback to an older release never meets the profile. If one ever does, `--remove-orphans` stops `trader-portfolio`, which is safe on demo.

*Part C: the Portfolio page*

- **AC-19** (page): a new page `/portfolio` in the dashboard shell, linked as "Portfolio" in the nav, with the usual top bar and alert bar. When `portfolio_heartbeat` is missing or stale, the page shows "Portfolio trader not running" and the time it was last seen, if ever. The page refreshes over SSE whenever its snapshots change, like the Overview. Existing pages are not changed except for the nav link.
- **AC-20** (per coin rows): one row per coin in `PAPER_INSTRUMENTS` order:
  - the 30 day signal as text (Long, Short, Flat, or Not eligible), never colour alone;
  - the target weight in %;
  - the target size in USD and in coins;
  - the current position (side and quantity);
  - the unrealized P&L in USD;
  - a "failed" marker when the coin is in `failed`.

  Plan values come from `portfolio_plan`. Positions and unrealized P&L come from `portfolio_positions`.
- **AC-21** (account summary): above the rows:
  - equity (USD);
  - gross leverage (the sum of |position notional| / equity);
  - realized P&L since the run started;
  - the last and the next rebalance time, in UTC and Amsterdam;
  - the failed coins;
  - the portfolio trader's state (running, paused, halted, with the reason);
  - the last command result with `slot=portfolio`.

  Equity comes from `portfolio_account`. Realized P&L is a dashboard query of the portfolio's current run (closed positions' realized P&L plus funding, by `run_id` from `portfolio_trader_state`). The next rebalance is computed from `run_at_utc` and `rebalance_weekday`, which `portfolio_plan` carries. State comes from `portfolio_trader_state`, and the result from the newest `slot=portfolio` entry within the existing search window of `{env}:control:results`.

*Part D: the switch*

- **AC-22** (VPS start): after merge, the deploy runs with the `portfolio` profile still off. It recreates `trader` as every deploy does; six vote's version hash is unchanged, so its promotion holds.
  1. You get the version with `sudo kfs-compose run --rm --no-deps trader-portfolio python -m trader.version` and promote it by hand (`candidate → paper`).
  2. `/etc/kfs/kfs.env` is backed up, then it gains `COMPOSE_PROFILES=portfolio` and `ALERTER_TRADER_SLOTS=main,portfolio`, and the dashboard gets `DASHBOARD_MAIN_TRADER_ID`.
  3. `sudo kfs-compose up -d --no-deps trader-portfolio alerter dashboard` starts the new trader and reloads the readers. `trader` is not recreated by this step.

  The sandbox balance is the same `SANDBOX_STARTING_BALANCE_USD` the six vote trader uses (the default 1000), so both halves are equal. `verify.md` names the backup.
- **AC-23** (start checks): for all 12 coins there is no `warmup_seed_failed` or `seed_stale`, and there is one `portfolio_targets` line each. A `start` rebalance writes a decision row and fills an order for every coin whose delta is above the minimum. The Portfolio page shows 12 rows, with positions matching the targets within one size step. A `pause` then `unpause` from the dashboard is answered by both traders (the CLI with `--slot all` shows both), and the unpause triggers a `resume` rebalance. Each coin whose `order_delta` (AC-2) is not `None` sends one order, and every other coin logs `rebalance_skip_small`. With the book already at its targets, any such order is small and comes from drift since the last rebalance (amended 2026-11-09: a 0.1 LINK order, about 1 USD, went out this way on the VPS). Six vote's `six_vote_values` lines carry on through steps 2 and 3 of the switch, and its Overview figures do not include any portfolio trade.

## Decision

**Chosen option**: Option 1: a second trader service with its own sandbox account, made possible by a `TRADER_SLOT` that gives it its own pause, halt, snapshot, and command names through a new slots module, plus a new pure portfolio rule and a single multi coin node.

Build `trend_portfolio` (rule, risk check, node, daily seed), run it as `trader-portfolio` beside six vote's trader, add the Portfolio page, and start it on the VPS demo as a forward test, with no lab screen and no gate.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `htmx` (`.claude/skills/htmx/`)

## Rationale

Reasoning, options, and the backtest evidence: see [rationale.md](rationale.md).

## Feature design

### Rule (`trading_core/strategy/trend_portfolio.py`)

`PortfolioParams` (frozen dataclass, AC-1 defaults), `CoinPlan` (eligible, signal, vol, weight, target notional, close date), `PortfolioPlan` (coins, N, gross, `gross_scale`, equity, close date), `portfolio_plan(...)`, and `order_delta(...) -> Decimal | None`.

### Risk check (`trading_core/risk/rebalance.py`)

`check_rebalance(request, limits) -> RebalanceVerdict`. The request holds side, wanted quantity, mid, quote age, equity, the book's current gross exposure, this coin's current quantity, and the maintenance margin rate. The verdict is approved with a quantity (possibly clipped, with the clip reasons) or refused with a reason. It reuses `RiskLimits`, `liquidation_price`, `distance_pct`, and `floor_to_step` unchanged.

### Node (`trading_core/node/trend_portfolio.py`)

- `on_start`: `super()` first, then trade, quote, and daily bar subscriptions for every coin, the seeds (AC-8), the daily timer (AC-4), and the start rebalance once the seed has landed and quotes are in (with the cooldown check).
- `on_bar`: store the close under the bar's day, and keep at most `seed_days`.
- Timer: plan, log, store the plan, and on Monday rebalance once.
- Gate listener: on clear after paused or halted, a `resume` rebalance.
- Rebalance: reductions first, then opens and grows through `check_rebalance`, and flips as close then open.
- Retries: a per coin timer, cancelled by the next rebalance, `flatten`, or `on_stop`.
- `flatten` and `flatten_for`: cancel timers and pending opens, then reduce only market closes for every coin.
- `resume_position`: adopt.

### Trader slot (Part B)

```
trading_core/control/slots.py (new, pure)
  TraderSlot.MAIN      -> halt.halt_key(env), pause.pause_key(env), name, "trader", today's consumer name, no slot field
  TraderSlot.PORTFOLIO -> {env}:trader:portfolio:halt, {env}:trader:portfolio:pause, "portfolio_"+name,
                          "trader-portfolio", "trader-portfolio-1", slot=portfolio
```

Users of the slot (all outside every closure): `trader/main.py` (snapshots, reconcile publish, view), `trader/cache.py`, `node/control.py` (the halt, pause, and kill switch keys), `node/loss_limits.py` (passes `TRADER_ID`), `control/consumer.py` (group, consumer, result field, refusals), `recording/queries.py` and `recording/repository.py` (`loss_seed`, the command recorder), `dashboard` (CLI, poller, app, pnl, the new page), `alerter`, and `deploy/host/kfs-deploy` plus `lib.sh`.

### State transitions (per coin, within one rebalance)

```
PLANNED ──delta below minimum──▶ DONE (skip, log only)
PLANNED ──reduce only order──▶ SENT ──filled──▶ DONE
PLANNED ──flip──▶ CLOSING ──filled──▶ PLANNED (open leg, sized again)
PLANNED ──opens or grows, check approves──▶ SENT ──filled──▶ DONE
PLANNED ──stale quote / rejected / denied──▶ RETRY_WAIT ──60 s──▶ PLANNED (up to 3 times) ──▶ FAILED
PLANNED ──liquidation distance refused──▶ FAILED
PLANNED ──paused, halted, or blocked, opens or grows──▶ HELD ──gate clear──▶ (resume rebalance)
any ──new rebalance / flatten / stop──▶ timers and pending opens cancelled
```

### Data model sketch

One migration: `control_commands.slot` (text, not null, server default `main`, check `main` or `portfolio`), and the primary key `control_commands_pkey` becomes (`id`, `slot`). The downgrade deletes the `portfolio` rows and restores the key on `id`. Nothing else changes. Plan values live in `decisions.inputs` (JSON). Runs are told apart by the existing `strategy_runs.trader_id`. The journal export's `trader` column is read from `strategy_runs.trader_id` (no schema change).

### API surface

| Surface | Kind | Inputs | Outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/portfolio` | GET page | none | the page (AC-19 to AC-21) | dashboard login | snapshots missing: the empty state |
| `/events` | SSE (existing) | none | adds the portfolio fragments | dashboard login | Redis down: the existing "No data" alert |
| control CLI | command (existing) | new `--slot main\|portfolio\|all` | results for those slots | VPS shell | a slot with a stale heartbeat is not waited for |
| `{env}:control` | Redis stream (existing) | unchanged | read by groups `trader` and `trader-portfolio` | Redis ACL users | none new |
| `{env}:control:results` | Redis stream (existing) | none | plus `slot=portfolio` on the portfolio's results | Redis ACL users | none new |
| `{env}:snapshot:portfolio_*`, `{env}:trader:portfolio:*` | Redis keys (new) | none | AC-12, AC-13 | Redis ACL users | none |

### Value sourcing

| Action | Value | Source |
|---|---|---|
| Plan (AC-1) | daily closes and their dates | the seed (Kraken charts `1d`) plus `1-DAY-LAST-INTERNAL` bars built from trade ticks; date = bar `ts_event` minus one day (UTC) |
| Plan (AC-1) | equity | the node's cache: the sandbox account's USD `balance_total` plus the open positions' unrealized P&L at the mid (Nautilus `portfolio.unrealized_pnls` for the venue) |
| Plan (AC-1) | lookback, vol days, vol target, gross cap, min history | config constants (AC-3): lookback, vol days, and min history from the backtest; vol target 0.31, chosen to reach about 10x gross so the cap binds (10.02× on the VPS), and gross cap 10, both by your choice for the demo |
| Quantity (AC-2) | mid, size step, minimum | the cached quote; `instrument.size_increment`, `instrument.min_quantity` |
| Quantity (AC-2) | current position | the node's cache, the net position per coin |
| Flip open (AC-5) | open size | the same plan's target notional at the mid when the close fills |
| Risk (AC-5) | caps, stale quote, margin rate, liquidation distance | `RiskLimits` from the portfolio service's `RISK_*` values (AC-18) |
| Risk (AC-5) | book gross exposure | the sum of absolute (quantity × mid) over the node's open positions |
| Schedule (AC-4) | run time, weekday, last weekly date | config `run_at_utc` 00:05 and `rebalance_weekday` 0 on the node clock (UTC); the last weekly date in node memory |
| Start guard (AC-4) | last start rebalance | the newest `decisions` row of this trader's runs with reason `start` |
| Resume (AC-4) | gate cleared | the pause gate's state change |
| Retries (AC-6) | count, wait | config `retry_max` 3, `retry_wait_s` 60 |
| Slot names (AC-12) | keys, group, consumer, result field | `trading_core/control/slots.py` from `TRADER_SLOT` |
| Page rows (AC-20) | signal, weight, target | the `portfolio_plan` snapshot |
| Page rows (AC-20) | position, unrealized P&L | the `portfolio_positions` snapshot |
| Summary (AC-21) | equity | the `portfolio_account` snapshot |
| Summary (AC-21) | realized P&L since start | Postgres: realized P&L of closed positions plus funding for the run in `portfolio_trader_state` |
| Summary (AC-21) | gross leverage | the sum of absolute position notional from `portfolio_positions` / equity |
| Summary (AC-21) | last and next rebalance | `portfolio_plan.last_rebalance_at`; next from `run_at_utc` and `rebalance_weekday` in the plan |
| Summary (AC-21) | Amsterdam time | the dashboard's existing time filter |
| Summary (AC-21) | trader state, last command | `portfolio_trader_state`; the newest `slot=portfolio` result in the existing search window |
| Dashboard scope (AC-16) | main trader | `DASHBOARD_MAIN_TRADER_ID` |
| Version (AC-10) | config JSON | AC-3's settings plus `instruments` |
| Loss seed (AC-16) | own runs | `strategy_runs.trader_id` = the trader's `TRADER_ID` |
| Alerts (AC-17) | slots, dedupe | `ALERTER_TRADER_SLOTS`; `portfolio_plan.last_rebalance_at` |
| Switch (AC-22) | balance | `SANDBOX_STARTING_BALANCE_USD`, unchanged (1000) |

### Key invariants

- The `main` slot behaves byte for byte as before: same keys, group, consumer, halt, pause, results, and snapshot names (a test pins them).
- No file in any registered strategy's closure is edited; every existing version hash is unchanged.
- Every command on the control stream reaches every running trader exactly once (one consumer group each), and no trader reads another's results.
- A stop halts and flattens both traders. A loss limit halt never crosses to the other trader. One trader's pause never carries into the other.
- Gross exposure after a rebalance is at most `gross_cap` × equity, and within every `RISK_*` cap of the portfolio service.
- Reductions are never blocked by a pause, a halt, or the risk check. Opening or growing orders are.
- At most one weekly rebalance per Monday date, and at most one start rebalance per `start_cooldown_s`.
- The rule is pure and deterministic: the same closes and equity always give the same plan. No money value uses a float.

### Security model

No new secret. The new trader uses the same Postgres and Redis users as `trader` (task 3 checks the Redis ACL covers `{env}:trader:portfolio:*` and `{env}:snapshot:portfolio_*`, adding the narrowest pattern if not). The only new outbound call is the daily candle fetch to the public charts API the other seeds use. Demo only: the slot and the strategy both refuse outside `TRADING_ENV=demo`. The page sits behind the existing dashboard login.

### Configuration required

- `TRADER_SLOT`: `main` (default) or `portfolio`; set only in the `trader-portfolio` service.
- `TRADER_ID` for `trader-portfolio`, in its compose service (distinct from `trader`'s).
- `PORTFOLIO_RISK_MAX_TOTAL_EXPOSURE_USD`, `PORTFOLIO_RISK_MAX_ORDER_NOTIONAL_USD`, `PORTFOLIO_RISK_MAX_LEVERAGE`, `PORTFOLIO_RISK_MAX_TRADES_PER_DAY`: the portfolio service's caps (defaults 5000, 1000, 2.5, 100). On the VPS, `/etc/kfs/kfs.env` sets exposure 12000, order 2500, and leverage 12 for the about 10x demo, and trades stay at 100. A leverage cap of 12 sits only 2x above the book, so after losses a grow is clipped to it (AC-5's order checks).
- `ALERTER_TRADER_SLOTS`: comma list of slots the alerter watches; default `main`; VPS `main,portfolio`.
- `DASHBOARD_MAIN_TRADER_ID`: the `TRADER_ID` of `trader`, so the dashboard's P&L and Overview read only six vote.
- `COMPOSE_PROFILES=portfolio` in `/etc/kfs/kfs.env` on the VPS turns the second trader on.

### Critical test scenarios

- Rule: three coins with known closes give the hand computed signal, vol, weights, and N; with the default params, a gross of 3 scales by 2/3; a coin with 119 closes is not eligible and gets 0; equal closes give signal 0; vol 0 gives weight 0 but counts in N; equity 0 raises. Verifies **AC-1**.
- Demo settings: the trader builds the node config with `vol_target` 0.31 and `gross_cap` 10. Verifies **AC-3**.
- Delta: target 0.0123 BTC with step 0.0001 gives 0.0123; a delta under the step is skipped. Verifies **AC-2**.
- Schedule: a Sunday run logs targets and sends nothing; a Monday run rebalances once, and a second Monday fire does nothing; a coin with no close for yesterday keeps its position with `close_missing`; a start within an hour of the last start rebalance skips; an unpause after a held start rebalance sends the held opens. Verifies **AC-4**.
- Orders: reductions are sent before grows; long 0.02 to long 0.01 is one reduce only sell; long to short is a reduce only close, then an open sized at the new mid; a grow over the order cap is clipped and logged; a liquidation distance refusal fails at once; paused: reductions go, grows are held; a guard denial retries. Verifies **AC-5**.
- Failures: a rejected order retries 3 times 60 s apart, then lands in `failed`, while the others fill; a new rebalance cancels pending retries. Verifies **AC-6**.
- Decisions: each coin with a delta gets one row with the plan values and the coin, and its fill links to it. Verifies **AC-7**.
- Seed: 150 closed daily candles make every coin eligible; the in progress candle is dropped; a seed ending two days ago is `seed_stale`; the first live bar's close is used under the right date. Verifies **AC-8**.
- Flatten: a stop during a flip cancels the pending open and closes everything reduce only; `has_open_positions` is false after. Verifies **AC-3**, **AC-15**.
- Slot names: `main` returns today's exact halt, pause, snapshot, group, and consumer names; `portfolio` returns the new ones; `portfolio` in live refuses. Verifies **AC-12**.
- Snapshots: the portfolio slot writes the prefixed names and never `exchange`; `portfolio_plan` stays present between runs; `cache reset` checks `portfolio_heartbeat`. Verifies **AC-13**.
- Commands: one `pause` is read by both groups and gives two results, one with `slot=portfolio`; a new group starts at `$` and does not replay an old `stop`; the CLI with default `--slot` ignores the portfolio's result; `--slot all` waits for both; the same command id is stored once per slot; `flatten` gets `not_for_slot` from the portfolio. Verifies **AC-14**.
- Pause and halt: a `stop` writes both halt keys and flattens both; a daily loss trip on `portfolio` writes only its key; a restarted portfolio does not pick up `main`'s pause. Verifies **AC-15**.
- Reads: the loss seed ignores the other trader's positions; the dashboard P&L ignores portfolio fills; the journal shows both with a `trader` column. Verifies **AC-16**.
- Alerts: a stale `portfolio_heartbeat` alerts naming the slot only when the slot is listed; a non empty `failed` alerts once per `last_rebalance_at`. Verifies **AC-17**.
- Deploy: with the profile on, the promotion check refuses an unpromoted portfolio version; the pause waits for both results; a portfolio health failure rolls back; with the profile off the script runs as today (dry run). Verifies **AC-18**.
- Page: snapshots for 12 coins render 12 rows with text signals; no heartbeat shows the empty state; times show in UTC and Amsterdam. Verifies **AC-19** to **AC-21**.
- Hashes: every registered version hash matches the baseline. Verifies **AC-11**.

## Build plan

Tracer Bullet: the thinnest real thread first (rule to orders on a laptop demo trader), then the second trader around it, then safety, alerts, deploy, and the page, then the VPS start.

1. Hash baseline: record every registered strategy version's code hash and `code_closure` file list to a scratch file. Satisfies **AC-11**.
2. Thin thread, one trader: the rule and `order_delta` with unit tests; `check_rebalance` with unit tests; the `TrendPortfolio` node with trade, quote, and daily bar subscriptions, the start rebalance, and the daily timer; the `1d` seed; the single instance build in `trader/main.py`, `TrendPortfolioView`, and the version; confirm the manual `candidate-paper` promote accepts kind `trend`. Laptop smoke as the **main** slot (`PAPER_STRATEGY=trend_portfolio`, 3 coins, the portfolio caps set in `.env`): seeds land, `portfolio_targets` lines appear, the start rebalance fills market orders in the sandbox, and decision rows are written. Take the stack down. Satisfies **AC-1** to **AC-4**, **AC-7**, **AC-8**, **AC-10**.
3. Second trader: `control/slots.py` with the `main` pin test; `TRADER_SLOT`; slot names through `main.py`, `cache.py`, `node/control.py`, the snapshot writer, and the reconcile publish switch; the consumer group at `$`, the result field, and the refusals; the result readers with `--slot`; the migration and the recorder; `loss_seed` by trader id; the dashboard reads scoped by `DASHBOARD_MAIN_TRADER_ID` and the journal `trader` column; the `trader-portfolio` service with its caps; the Redis ACL check. Laptop smoke with both traders (six vote as `main` on 2 coins, the portfolio on 3): a pause reaches both with two results, unpause lifts both and triggers the `resume` rebalance, a stop flattens both, and the Overview P&L shows only six vote. Satisfies **AC-12** to **AC-16**, **AC-18** (compose part).
4. Order paths: reductions first, flips, clipping, held orders and the resume rebalance, retries and `failed`, the start cooldown, `flatten` across coins, and `resume_position`, each with the test scenarios above. Satisfies **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-9**.
5. Alerts and deploy: `ALERTER_TRADER_SLOTS` and the failed coins alert; the `kfs-deploy` promotion check, `--slot all` pause and unpause, `kfs_unpause_if_ours` per slot, the health gate, and the rollback rule; a dry run of the deploy script with the profile on and off. Satisfies **AC-17**, **AC-18**.
6. Portfolio page: the route, the nav link, the template, the SSE fragments, the realized P&L query, and the empty state. Laptop check with both traders running. Satisfies **AC-19** to **AC-21**.
7. Close out and VPS start: compare hashes with task 1, merge, deploy with the profile off, you promote the version by hand, back up `kfs.env`, set the profile, alerter slots, and dashboard trader id, run the `up -d --no-deps` command, run the start checks, and write `verify.md`. Satisfies **AC-11**, **AC-22**, **AC-23**.

## Consequences

**Positive**:
- The first strategy here with real out of sample evidence (Binance 2018 to 2022, every year positive on these 12 coins) gets a live forward test.
- About 12 orders a week, so fees are tiny (about 3% over 4 years in the backtest).
- Six vote's version is unchanged and its dashboard figures stay its own; the two run on separate money and can be compared.
- The slot work makes "more than one trader" a real, tested capability, which the go live spec (0028) will need anyway if both ever go live.

**Negative / tradeoffs**:
- Most of the work is plumbing, not strategy: a new slots module threaded through the trader, control consumer, CLI, dashboard, alerter, loss limits, and deploy script, plus a migration and a new page. Parts B and C are about 2 to 3 times Part A.
- A weekly strategy is slow to judge: a few months of demo give only about 12 to 20 rebalances, so the result stays noisy for a long time. The backtest had losing years too (2022: −20% on the Kraken data for these coins).
- No stop on positions, as tested: a coin can gap hard against a position during the week. The book's size, the leverage cap, and the account kill switch are the only guards, and the loss limits are off on demo.
- At about 10x (amended 2026-11-09, your choice), the demo is not the method the backtest tested, and at 10x gross a one sided move of under 10% against the whole book can empty the account, since liquidation comes before the full 10%. A book of longs and shorts usually loses less than that when the whole market moves. On the VPS, `kfs.env` raises the portfolio's caps to match: exposure 12000, order 2500, leverage 12. To forward test the tested method, you would go back to `vol_target` 0.02 and `gross_cap` 2 and the default caps, which needs a new version and promotion.
- Every deploy or restart on demo empties the sandbox, and the start rebalance rebuys the book at market midweek, with closes up to yesterday rather than Sunday's plan: a few extra fees and an entry the backtest did not have. The cooldown bounds a crash loop to one rebuild an hour.
- Live would need two Kraken accounts (or subaccounts) for two traders. Until that exists, `TRADER_SLOT=portfolio` refuses outside demo.
- The dashboard's top bar command state still follows `main` only. A portfolio trader that misses a command shows on its own page and through the alerter.
- A portfolio health failure during a deploy rolls back six vote's trader too (one release).

**Neutral**:
- Three strategy side modules and one slots module are new. Every existing strategy version stays as it is.
- `control_commands` gains a column and a wider primary key, and its old rows become `main`.
- The portfolio service has its own `RISK_*` caps, separate from six vote's.

## Follow-up

- [ ] Before any live money: a lab study and gate verdict for `trend_portfolio` (kind `trend`), and a decision in spec 0028 on two Kraken accounts for two live traders.
- [ ] Decide when to judge the forward test. A reasonable minimum is 12 weekly rebalances (about 3 months) before reading anything into the result.
- [ ] Before judging either forward test, restore the loss limit lines from `/etc/kfs/kfs.env.bak-20261106-sixvote` (spec 0029 AC-12) for both traders.
- [ ] Optional later: an equity chart and a rebalance history table on the Portfolio page (not chosen now), and a top bar command state that covers both slots.
- [x] Add a scope row (feature 31) for this spec.
