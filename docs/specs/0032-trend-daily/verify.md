# Verify: trend_daily · spec 0032 · updated 2026-11-11
_Steps derived from spec 0032 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## Build checks (laptop, done during /develop 2026-11-11)

- [x] Hash baseline before and after: every strategy module's code hash unchanged; only `node/control.py` (and the non strategy modules that import it: `control`, `loss_limits`, `paper_snapshots`, `reconcile`) moved, and `trend_daily` is new → AC-1
- [x] `pytest packages/core/tests/test_node_trend_daily.py`: three days at 00:05 give three `daily` rebalances, each trading the day's change; a second fire on the same date sends nothing; a start at 00:03 is followed by the 00:05 daily run; the plan snapshot has `schedule: daily`, no `rebalance_weekday`, and `close_missing` → AC-1, AC-2, AC-2a, AC-2b, AC-3
- [x] `pytest apps/trader/tests/test_paper_trend_daily.py`: the version records every AC-4 value and the `RISK_*` caps, differs from `trend_portfolio`'s, a cap change is a new version; refused on live and without BTC; one instance with a view per coin; daily seed 150/121; `book` true only for a built demo book → AC-4, AC-5, AC-6
- [x] `scripts/tests/host-scripts-test.sh`: a demo book pauses and does not wait for flat; book false, missing, or on live still waits; an unconfirmed pause still aborts → AC-7
- [x] `pytest apps/dashboard/tests/test_portfolio_main.py`: main mode reads main's keys, run, and results, shows the daily schedule and next run, and the churn table; default mode is unchanged; a churn read failure says so → AC-2a, AC-8
- [x] `pytest apps/alerter/tests/test_book_on_main.py`: a main book's failed coins alert once per rebalance as service `trader`, slot `main`; a missing close alerts once per plan date; no book and no portfolio slot reads nothing → AC-2b, AC-9
- [x] Laptop run with `PAPER_STRATEGY=trend_daily` on 12 coins: 12 `start` decisions approved, 12 fills (all short), about 4982 USD notional at 1000 USD equity, `trader_state.book` true, `portfolio_plan.schedule` daily → AC-3, AC-5, AC-6
- [x] `daily_churn` against laptop Postgres matches a direct sum over the fills (4982.0557 USD, fees 2.48) and is 0 for another trader id → AC-8
- [x] Order cap from the last 150 Kraken daily closes at 1000 USD: largest target BTC −667.67 USD, 1.5 times is 1001.51, cap 2500 (the floor) → AC-10

## VPS (task 7 and 8)

- [ ] `/etc/kfs/kfs.env.bak-2026-11-11-trenddaily` exists; `kfs.env` has `PAPER_STRATEGY=trend_daily`, `RISK_MAX_ORDER_NOTIONAL_USD=2500`, `DASHBOARD_PORTFOLIO_SLOT=main`, `RISK_MAX_TOTAL_EXPOSURE_USD=9000`, `RISK_MAX_LEVERAGE=9`, `COMPOSE_PROFILES` empty → AC-10
- [ ] `control_cli pause --slot main --source cli`, `flatten --slot main`, then `scripts/deploy.sh` exits 4 and prints the promote line; the owner runs it with note `trend_daily` → AC-11
- [ ] After the deploy: `Cached 0 positions`, 12 `warmup_seed_fetched` with 150 daily closes each → AC-11 step 1
- [ ] The paused start writes 12 decisions with reason `start`, refused `held:paused` → AC-11 step 2
- [ ] `unpause --slot main --source cli` gives 12 `resume` decisions approved and 12 fills; gross about 5x equity → AC-11 step 3
- [ ] `control_cli state` shows `"book": true` → AC-6
- [ ] Portfolio page: main's book, "rebalanced every day", next rebalance the coming 00:05 UTC, "Traded per day" table with today's fees → AC-2a, AC-8
- [ ] The next 00:05 UTC run logs `portfolio_targets` for 12 coins and a `daily` rebalance (decisions with reason `daily`) → AC-2, AC-11 step 5
- [ ] A later deploy logs `trader holds a book, not waiting for flat` and goes on → AC-7

## Value sourcing checks

- [ ] Signal, vol, weight, target: the 00:05 plan's targets match a rerun of `portfolio_plan` on the same closes (the order cap script in the session scratchpad) → value row 1
- [ ] Equity: the plan's `equity` equals the sandbox account's equity on the Portfolio page → value row 2
- [ ] Plan date: the 00:05 run on day D plans `plan_day` D−1 → value row 3
- [ ] Order clip: no `rebalance_clipped` at 1000 USD equity with the 2500 cap; clips appear only once equity passes about 1800 USD (the 9000 exposure ceiling) → value row 5
- [ ] Fees per day: the page's figure for a day equals the sum of `fills.fee` over main's runs that UTC day → value row 7

## Acceptance-criteria coverage

- AC-1 hash and module · AC-2 daily once per date · AC-2a snapshot schedule and page · AC-2b close_missing alert · AC-3 every change trades · AC-4 settings in the version · AC-5 wiring · AC-6 book flag · AC-7 deploy skip · AC-8 page slot and churn · AC-9 main slot alerts · AC-10 kfs.env and cap · AC-11 the switch on the VPS

## Note for /architect

AC-7 reads `.data.env` from `trader_state`, which has no env field. The owner chose (2026-11-11, during /develop) to use the deploy's own `kfs_trading_env` (TRADING_ENV in `/etc/kfs/kfs.env`); AC-7's wording is owed an amendment.
