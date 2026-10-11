# Scope: Kraken Futures Scalper

An automated trading bot for Kraken futures (BTC and ETH perpetuals) that scalps on a seconds timescale, keeps slower hour level strategies as a fallback, and only trades real money after strategies pass backtests and paper trading. Built for one trader in the Netherlands, judged on profit per unit of risk, not win rate.

**Build approach:** Tracer Bullet (one thin real thread from market data to order to dashboard first, then thicken each segment end to end).
**Workflow:** GA (after develop: check verify, test, a fresh model check review, then document). The project default level of rigor. `/architect` is the recommended first stop for a feature with a real decision, but skippable when you already know the build. Any feature can carry its own tag (e.g. `· Beta`) to do more or less.

_These are recommendations to keep your build orderly, not requirements. Skip anything that does not fit: if you already know how to build a feature, use `/develop` and skip `/architect`. You decide when a feature is `done`._

## At a glance

| # | Feature | Phase | Status |
|---|---------|-------|--------|
| 1 | Stack & architecture | Foundation | done |
| 2 | Coding standards & tooling | Foundation | done |
| 3 | Data model | Foundation | done |
| 4 | API keys & secrets | Foundation | in-progress |
| 5 | Design system & dashboard shell | Foundation | done |
| 6 | Core paper trading loop | Slice 1 | done |
| 7 | Always on cloud deployment | Slice 2 | in-progress |
| 8 | Order book recorder | Slice 3 | done |
| 20 | Market context feed | Slice 3 | planned |
| 9 | Kill switch & loss limits | Slice 4 | planned |
| 10 | Leverage & liquidation guard | Slice 4 | planned |
| 11 | Restart & outage safety | Slice 4 | planned |
| 12 | Phone alerts & health checks | Slice 5 | planned |
| 13 | Candle backtest lab | Slice 6 | planned |
| 14 | Scalping simulator | Slice 6 | done |
| 15 | Strategy scoring & promotion gate | Slice 6 | planned |
| 16 | Scalping strategy candidates | Slice 7 | planned |
| 17 | Hour level strategy candidates | Slice 7 | planned |
| 18 | Trade journal export | Slice 8 | planned |
| 19 | Go live with small capital | Slice 9 | planned |

## Foundations

### 1. Stack & architecture · done
Decide language, runtime, process layout, and a host region close to Kraken's servers (latency matters for scalping), then scaffold a runnable project.
**Done when:** the stack is recorded in a spec and the empty scaffold boots locally and passes build.
spec [0001](../specs/0001-stack-architecture/index.md) · code in [packages/core/](../../packages/core/), [apps/](../../apps/), [compose.yaml](../../compose.yaml), [docker/](../../docker/)
- [x] Decide the stack (spec): `/architect stack & architecture`
- [x] Scaffold from the decision: `/develop stack & architecture`
- [x] Verify it: `/check verify stack & architecture`
- [x] Test it: `/test stack & architecture`
- [x] Review it (fresh model): `/check review stack & architecture`
- [x] Document it: `/document stack & architecture`

### 2. Coding standards & tooling · done
Capture conventions and tooling from the real scaffolded project, then install lint, format, type checks, pre commit, and CI.
**Done when:** root `AGENTS.md` reflects the real stack, and lint, format, and pre commit run clean.
code in [AGENTS.md](../../AGENTS.md), [pyproject.toml](../../pyproject.toml), [.pre-commit-config.yaml](../../.pre-commit-config.yaml), [scripts/typecheck.sh](../../scripts/typecheck.sh), [.github/workflows/ci.yml](../../.github/workflows/ci.yml)
- [x] Capture conventions + tooling choices: `/audit`

### 3. Data model · done
Core records every feature builds on: market data (trades, book snapshots, candles), orders, fills, positions, funding payments, strategy runs, backtest results, risk events.
**Done when:** the model supports paper and live trading, backtests, and the journal export without a breaking migration, and every order traces to the strategy decision that caused it.
spec [0002](../specs/0002-data-model/index.md) · code in [packages/core/src/trading_core/recording/](../../packages/core/src/trading_core/recording/)
- [x] Design it (spec): `/architect data model`
- [x] Build it: `/develop data model`
  - [x] Schema and roles: recording package, settings, role setup, models, migration `0002` (AC-1, AC-2, AC-4, AC-9, AC-11, AC-13)
  - [x] Traceable write thread: core repository from instrument to fill, stub orders, conflicts, integration test setup (AC-3 to AC-8)
  - [x] Full write API: events, snapshots, funding, risk, control, promotions and live gate, run bundles, journal query (AC-5, AC-10, AC-14, AC-15)
  - [x] Guards and retention: role permission, no float, downsample, and migration round trip tests (AC-1, AC-9, AC-11, AC-12)
- [x] Verify it: `/check verify data model`
- [x] Test it: `/test data model`
- [x] Review it (fresh model): `/check review data model`
- [x] Document it: `/document data model`

### 4. API keys & secrets · in-progress
Keep Kraken API keys safe: least privilege keys (trade only, never withdraw), live keys only on the VPS (demo needs no keys since Kraken closed its futures demo), never in the repo or logs.
**Done when:** the bot loads keys from a secret store, live keys cannot withdraw funds, and a leaked log or repo contains no secret.
spec [0003](../specs/0003-api-keys-secrets/index.md) · code in [packages/core/src/trading_core/secrets.py](../../packages/core/src/trading_core/secrets.py), [compose.yaml](../../compose.yaml), [compose.live.yaml](../../compose.live.yaml), [scripts/](../../scripts/), [docker/](../../docker/)
- [x] Design it (spec): `/architect API keys & secrets`
- [x] Build it: `/develop API keys & secrets`
  - [x] Secret file thread: loader, redaction, boot order, split env guard, dev script, one secret end to end (AC-1 to AC-5, AC-14)
  - [x] All Postgres secrets from files, owner password cutover (AC-1, AC-11)
  - [x] Redis ACL users for trader and dashboard (AC-10)
  - [x] Kraken live path: exec client factory, live override, key age, key check, canary leak test (AC-4, AC-6 to AC-9)
  - [x] Backup secrets, gitleaks, runbook (AC-12, AC-13, AC-15)
  - [x] Key check module: move the watcher to `trading_core/node/key_check.py` (AC-8, review follow up)
- [ ] Verify it: `/check verify API keys & secrets`
- [x] Test it: `/test API keys & secrets`
- [x] Review it (fresh model): `/check review API keys & secrets`
- [x] Document it: `/document API keys & secrets`

### 5. Design system & dashboard shell · done · Beta
A small, clear visual language for a private dashboard: numbers, tables, charts, status colors, and an always visible stop button.
**Done when:** `design.md` covers type, color, spacing, and base components, and the shell renders behind a login only you can pass.
spec [0004](../specs/0004-design-system-dashboard-shell/index.md)
code in [apps/dashboard/src/dashboard/](../../apps/dashboard/src/dashboard/), [apps/trader/src/trader/snapshot_writer.py](../../apps/trader/src/trader/snapshot_writer.py), [packages/core/src/trading_core/snapshot/](../../packages/core/src/trading_core/snapshot/), [packages/core/src/trading_core/control/](../../packages/core/src/trading_core/control/), [docker/redis/start.sh](../../docker/redis/start.sh)
- [x] Design it (spec): `/architect design system & dashboard shell`
- [x] Build it: `/develop design system & dashboard shell`
  - [x] Thin thread: credentials and login, bare shell, trader heartbeat to live data age over SSE (AC-1 to AC-5, AC-7 to AC-9, AC-18, AC-19)
  - [x] Stop path: control command, ACL change, command state from results, Redis down handling (AC-11 to AC-14)
  - [x] Design system: `PRODUCT.md`, root `DESIGN.md`, tokens, fonts, components, filters, `/styleguide`, Impeccable critique and polish (AC-15 to AC-17)
  - [x] Complete shell: nav and empty states, responsive layout, LIVE treatment, link status, vendored assets (AC-6, AC-7, AC-10, AC-18)
  - [x] Activity feed: newest 20 stop commands with their outcomes on Overview, live over SSE (AC-20)
- [x] Verify it: `/check verify design system & dashboard shell`
- [x] Test it: `/test design system & dashboard shell`

## Slice 1: Core paper trading loop

### 6. Core paper trading loop · done
The thinnest real thread (this is the walking skeleton): live BTC perp prices from Kraken's public feed → one deliberately simple placeholder strategy → a risk check with fixed size and leverage caps → an entry with its stop placed as soon as it fills → fills recorded → position and P&L shown on the dashboard. Kraken closed its futures demo on 14 July 2026, so demo orders and stops rest in NautilusTrader's sandbox (a simulated exchange inside the trader), not on Kraken.
**Done when:** in demo (real prices, sandbox fills), left alone for 2 hours, the bot opens and closes at least 6 positions by itself, every entry has its stop accepted within 5 seconds, fills and fees land in the database, and the dashboard shows the live position and P&L matching it.
spec [0006](../specs/0006-core-paper-trading-loop/index.md)
- [x] Design it (spec): `/architect core paper trading loop`
- [x] Build it: `/develop core paper trading loop`
  - [x] Gate: sandbox on `PF_XBTUSD` fills an entry and a reduce only stop; key purge allowed (AC-1)
  - [x] Thin thread: settings, sandbox client, pure risk check, `placeholder_timed`, recording actor and writer, live position card (AC-2 to AC-6, AC-8 to AC-11, AC-16, AC-17, AC-20)
  - [x] Failure paths: stop acceptance and exit loop, stale feed, writer errors, spill and replay, abandoned close, blocked badge (AC-7, AC-8a, AC-12 to AC-15, AC-18)
  - [x] Trades page and the 2 hour run (AC-19, AC-21)
- [x] Verify it: `/check verify core paper trading loop`
- [x] Test it: `/test core paper trading loop`
- [x] Review it (fresh model): `/check review core paper trading loop`
- [x] Document it: `/document core paper trading loop`

## Slice 2: Always on

### 7. Always on cloud deployment · in-progress
Run the bot 24/7 on a hosted server near Kraken, with automatic restart, logs, and a safe way to deploy updates.
**Done when:** the paper loop runs unattended for 72 hours, survives a forced restart, and a deploy never leaves an order unmanaged.
**Carried over from feature 5:** once the VPS and Tailscale are up, run spec 0004 verify steps [1, 6, and 12](../specs/0004-design-system-dashboard-shell/verify.md) on the real hardware: add the QR to your authenticator app, write the dashboard files with `sudo scripts/dashboard-credentials.sh --write` (then `chown 10001`, `chmod 0400`), and on a real phone over Tailscale check the stop button and airplane mode. The laptop run on 2026-10-03 already proved the behavior behind each one. (Now spec 0005 AC-23; `place-secrets.sh` writes the files.)
spec [0005](../specs/0005-always-on-cloud-deployment/index.md) · code in [deploy/](../../deploy/), [scripts/deploy.sh](../../scripts/deploy.sh), [compose.vps.yaml](../../compose.vps.yaml), [docker/backup/](../../docker/backup/), [scripts/restore-drill.sh](../../scripts/restore-drill.sh)
- [x] Design it (spec): `/architect always on cloud deployment`
- [ ] Build it: `/develop always on cloud deployment`
  - [x] Thin thread to the VPS: compose image vars and `kfs-compose`, CI images to GHCR, bootstrap, secret placement, deploy v1 with health gate, tailnet policy, 0004 carryover on the box (AC-1 to AC-5, AC-22, AC-23)
  - [x] Safe deploy: pause and unpause commands, `KfsStrategy` gate, trader control consumer and `trader_state`, graceful shutdown, wait for flat, rollback, dashboard Pause and Resume (AC-6 to AC-13)
  - [x] Self healing: autoheal timer, safe reboot, missing disk guard, the four restart drills (AC-14 to AC-18)
  - [x] Backups: append only key gate, backup image and nightly loop, restore drill with prune (AC-19 to AC-21)
  - [ ] Soak, after feature 6: `kfs-soak`, a deploy during an open demo position, the 72 h run (AC-8, AC-9, AC-24)
- [ ] Verify it: `/check verify always on cloud deployment`
- [ ] Test it: `/test always on cloud deployment`
- [ ] Review it (fresh model): `/check review always on cloud deployment`
- [ ] Document it: `/document always on cloud deployment`

## Slice 3: Start the data clock

### 8. Order book recorder · done
Record BTC and ETH perp order book updates and trades continuously, because scalping can only be tested on this data and it takes weeks to collect.
**Done when:** the recorder runs 24/7, detects and logs gaps, and stored data can be replayed in order at a known size cost per day.
**Carried over from spec 0005:** local catalog retention on the 50 GB disk (B2 holds the history), and which catalog paths the nightly backup should skip while an hour is still open.
spec [0007](../specs/0007-order-book-recorder/index.md)
- [x] Design it (spec): `/architect order book recorder`
- [x] Build it: `/develop order book recorder`
  - [x] Gate: rotation and types, pyarrow Parquet read by the catalog, feed `CLEAR`, carry speed, 512 MB memory (M0)
  - [x] Thin thread: hourly rotation, BTC delta conversion with the hour start snapshot, replay check, Parquet ratio measured (AC-1, AC-2, AC-3)
  - [x] Conversion and backlog: all types, closed file rule, `done.json`, space checked backlog, idempotent reruns, overlap (AC-2, AC-9, AC-11, AC-12)
  - [x] Gaps and manifest: reconnect, recorder down, silence, daily manifest (AC-4, AC-5)
  - [x] Out of order rows: 600 s reorder window, late rows to a backed up quarantine, recorder queue depth warning, compactor CPU weight (AC-12, AC-13, AC-14). The laptop stress check (task 12) was skipped on 2026-10-08; the VPS checks in the last milestone cover AC-13 and AC-14 live.
  - [x] Disk safety and backup: retention gated on the backup, recorder health and hard floor, `catalog` snapshots kept by the drill (AC-6, AC-7, AC-8). The drill also restores only main snapshots now, since a catalog snapshot holds no dump.
  - [x] Laptop fetch and VPS deploy: `fetch-catalog.sh`, compactor `--offline` and bridge gap, deployed 2026-10-08 (AC-5, AC-9, AC-10). The backlog conversion was dropped the same day, and the recorder now records BTC only (spec 0007 Summary, AC-9, AC-14).
  - [x] Lost messages (from `/check verify`, 2026-10-08): gate (resubscribe brings a `CLEAR`, run folder name, CPU cost), the recorder's sequence watch with the holes file and resubscribe, the compactor's `sequence` gaps and incomplete hours, the AC-3 sample point, then deploy and the 24 h replay check (AC-3, AC-4, AC-15, AC-16, AC-17). Built 2026-10-08 on `feat/recorder-lost-messages` (tasks 18 to 22). Gate: the run folder is the instance id and the watch costs 0.13% of a core, but a resubscribe brought no `CLEAR`, so it is dropped as AC-15 allows. The real 15:00 hour showed 137 bad samples with the old rules and none with `sequence` gaps. Deployed 2026-10-09 (#26); the 24 h check passed 2026-10-11 on 2026-10-10 (1,440 samples, 0 bad, 0 holes).
- [x] Verify it: `/check verify order book recorder`
- [x] Test it: `/test order book recorder`
- [x] Review it (fresh model): `/check review order book recorder`
- [x] Document it: `/document order book recorder`

### 20. Market context feed · needs a decision
Record the perp market's own context next to prices: funding rate, open interest, mark versus index basis, and liquidations. Strategies can then use them as inputs and regime filters, in backtests and live alike. Much of this history cannot be downloaded later, so recording starts early.
**Done when:** the context values are recorded continuously with gaps logged, a backtest can read them at the time each one was known (no look ahead), and a strategy can read the same values live.
**Carried over from research (2026-10-04):** the idea came from the `tracking-crypto-derivatives` skill (ideas only, nothing installed). Optional extras for the design to weigh: a news or event blackout filter, the Fear & Greed index as a regime tag, and later Deribit options implied volatility and put/call ratio.
**Carried over from an article (2026-10-04, a OneBullEx post, promotional, rules of thumb only):** a funding rate above +0.05 % per period reads as crowded longs and reversal risk, below −0.02 % as crowded shorts. Rising open interest while price sits in a tight range reads as pressure building before a breakout. Treat both as hypotheses for the backtest to test on our own data, not as thresholds to hard code.
**Carried over from spec 0008:** own recorder days carry no funding, so the scalping simulator labels own runs "before funding". Once this feed records funding, the simulator can apply it to own days and drop that label.
- [ ] Design it (spec): `/architect market context feed`

## Slice 4: Safety guards (all required before live)

### 9. Kill switch & loss limits · needs a decision
One action closes all positions, cancels all orders, and stops trading; automatic halt after a daily or total loss threshold.
**Done when:** the dashboard stop button and an automatic loss limit each flatten everything in demo within seconds, and trading stays halted until you resume it.
**Carried over from spec 0005:** build `stop`, `resume`, and `flatten` on the existing ControlActor, decide how stop and pause combine, and reword spec 0004 verify step 21 (stop answers `not_implemented` until then).
**Carried over from an article (2026-10-04, monkeytrade scalping guide, forex focused), for the design to weigh:** besides the daily loss limit (the article suggests 2 to 3 % of the account), a losing streak stop: halt new entries for the day after three losses in a row, shown as one more `entries_blocked` reason (spec 0006).
**Also from an article (2026-10-04, NinjaTrader 1 minute scalping), for the design to weigh:** two guards against overtrading, each one more `entries_blocked` reason. A cap on trades per day, since a fast strategy always finds a signal and every trade pays fees. And a short cooldown after any loss (somewhere around 5 to 15 minutes, tuned in testing), a softer cousin of the losing streak stop.
- [ ] Design it (spec): `/architect kill switch & loss limits`

### 10. Leverage & liquidation guard · needs a decision
Hard caps on position size, total exposure, and leverage (2x at first), plus a minimum distance between entry and liquidation price; funding costs counted in every decision.
**Done when:** any order that breaks a cap or sits too close to liquidation is rejected and logged, and funding payments show in P&L.
**Carried over from spec 0006:** feature 6 shipped fixed caps in a pure `check_entry` (extend it, don't fork it) and left funding out of P&L, labeled "before funding". This feature decides how funding reaches `funding_payments` and `positions.realized_pnl` in demo (spec 0002's open Follow-up).
**Carried over from an article (2026-10-04, changelly strategies overview), for the design to weigh:** size each trade from its risk, not a fixed notional. Size = (account equity × risk per trade) ÷ (distance from entry to stop), with the article's 1 to 2 % as the usual ceiling for risk per trade. Every stopped out trade then costs about the same, whatever the stop distance. The hard caps above still apply on top, and the smaller size wins. Fees count toward the risk, since a tight stop on a small move can lose more to fees than to price.
- [ ] Design it (spec): `/architect leverage & liquidation guard`

### 11. Restart & outage safety · needs a decision
After a crash, restart, or dropped connection, the bot rebuilds its view from the exchange's real positions and orders before doing anything else, and never sends a duplicate order.
**Done when:** killing the bot or the network mid trade, then restarting, leaves positions and stops correct with no duplicates, and a stale data feed pauses new entries.
**Carried over from specs 0005 and 0006:**
- **Demo restart:** spec 0006 settled it. The sandbox cannot restore a position, so a demo restart closes it as abandoned with a critical event.
- **Stale feed:** a stale feed already blocks entries.
- **What this feature still owes:** reconciliation with Kraken's real positions and orders for live, and how to test it before go live.
- [ ] Design it (spec): `/architect restart & outage safety`

## Slice 5: Stay informed

### 12. Phone alerts & health checks · needs a decision
Push alerts to your phone for fills, limit hits, errors, and a missing heartbeat, so you know within a minute when something is wrong.
**Done when:** each alert type reaches your phone within a minute, and a stopped bot triggers an alert on its own.
**Carried over from specs 0005 and 0006:** alert on:
- the deploy and host events: `autoheal_gave_up`, `backup_failed`, an unhealthy backup, `rollback_failed`, disk under 5 GB
- a restore drill older than 35 days
- a newest `kfs-vps` B2 snapshot older than 26 h, checked from outside the VPS
- the trader's critical risk events: `stop_not_accepted`, `exit_failed`, an abandoned position, `record_conflict`
- an entry block that lasts longer than an hour
- [ ] Design it (spec): `/architect phone alerts & health checks`

## Slice 6: The strategy lab

### 13. Candle backtest lab · needs a decision
Test hour level strategies on Kraken candle history with realistic fees, funding, and slippage.
**Done when:** a strategy can be backtested over any date range, results include fees and funding, and the same strategy code runs unchanged in the backtest and the paper loop.
**Carried over from research (2026-10-04):** guard against look ahead. A swing high or low is only known some bars after it happens (it needs the bars that follow it), so a signal must fire on the bar where the swing is confirmed, never on the swing bar itself. Public price action scripts often get this wrong, and it makes a backtest look far better than live.
- [ ] Design it (spec): `/architect candle backtest lab`

### 14. Scalping simulator · done
Replay recorded order book data to test scalping honestly: maker and taker fees, queue position, latency, and partial fills.
**Done when:** a scalping strategy replays over recorded days, results account for fees, queue position, and latency, and a known losing strategy shows a loss.
**Carried over from an article (2026-10-08, `strategy.md`), for the design to weigh:** post only maker entries pay the lower maker fee, but a resting limit order often never fills, and when it does it is often because price is running through it (adverse selection). The simulator should model both: the fills a limit order misses, and the worse average outcome of the fills it gets.
spec [0008](../specs/0008-scalping-simulator/index.md)
code in `apps/lab`, `packages/core/src/trading_core/lab`
- [x] Design it (spec): `/architect scalping simulator`
- [x] Build it: `/develop scalping simulator`
  - [x] Gate: one real hour through the engine with queue, latency, fees; speed and memory; commission rounding; quotes into bars; one Tardis hour and its funding fields (AC-1, AC-5, AC-9, AC-14, AC-16)
  - [x] Thin thread: `apps/lab` and its Compose service, migration (`run_fill_stats`, `lab_app`), run file, segment planner, hour stream, `random_taker`, bundle into the laptop Postgres, report (AC-3, AC-4, AC-5, AC-7, AC-11, AC-14, AC-16, AC-17)
  - [x] Tardis data and funding: shared hour writer moved to core, `lab import-tardis`, cap and prune, funding payments, the full loser check on a Tardis day (AC-1, AC-2, AC-9, AC-14)
  - [x] Fill realism and variants: `maker_probe`, queue bound, latency sweep, pair entry, segment end close, fill rate and markouts (AC-6, AC-8, AC-10, AC-18)
  - [x] Hardening and parity: determinism, failure isolation, the worker pool and the day timings, `fetch-paper-decisions.sh` and the parity check, permission tests (AC-12, AC-13, AC-15, AC-16, AC-17). Built 2026-10-09: tasks 17, 18, 20, and parity (task 19, ratio 1.0 on the 2026-10-08 paper hours). The own day timing stays a verify.md step
  - [x] Core boundary guard (amended 2026-10-11 from the order book recorder review, finding 5): `test_core_boundary.py` keeps Nautilus in `node/` and `catalog/`, and `pyarrow` and `numpy` in `catalog/` (spec 0008 task 21). A small follow-up; it does not reopen the feature. Built 2026-10-11
- [x] Verify it: `/check verify scalping simulator`
- [x] Test it: `/test scalping simulator`
- [x] Review it (fresh model): `/check review scalping simulator`
- [x] Document it: `/document scalping simulator`

### 15. Strategy scoring & promotion gate · needs a decision
Score every strategy on return after costs, max drawdown, and a risk adjusted ratio (win rate shown, never ranked on), with out of sample testing to catch overfitting. Only strategies that pass move to paper, and only paper winners can go live.
**Done when:** the dashboard ranks strategies on those metrics, the gate blocks any strategy that fails out of sample or paper thresholds, and promotions are logged.
**Carried over from research (2026-10-04), for the design to weigh:**
- **Fee hurdle:** short horizon Bitcoin strategies that beat buy and hold on paper were not reliably profitable once fees were counted (Ahmad et al. 2021, PeerJ Computer Science 7:e337). The gate should require the average trade to clear the round trip cost (taker fee twice, about 0.1 %, plus funding) by a clear margin.
- **Buy and hold benchmark:** most standard indicator rules did not beat simply holding the coin (Ahmed et al. 2020, Finance Research Letters 35:101495). Score every candidate next to holding BTC over the same period, and fail it if it does not beat that.
- **Overfitting test:** a backtest can look good only because it fits noise. Consider the probability of backtest overfitting test (Gort et al. 2022, arXiv 2209.05559) alongside the out of sample windows.
- **Maker or taker entries** (from an article, 2026-10-08, `strategy.md`): score each scalping candidate twice, once entering with taker orders and once with post only maker orders. The maker fee lowers the fee hurdle, but only the simulator's fill model (feature 14) says whether the missed fills and adverse selection eat that saving.
- **Carried over from spec 0008:** decide how the laptop's replay results reach the VPS dashboard (load the `.bundle.json` files, or another path). About 55 free Tardis days exist, enough to reject strategies but thin for tuning, so guard against overfitting. A five variant day takes about 2 h 20 min on the laptop. If the day counts need more speed, a depth band filter is an option, but it needs its own fidelity proof.
- [ ] Design it (spec): `/architect strategy scoring & promotion gate`

## Slice 7: First strategies

### 16. Scalping strategy candidates · needs a decision
A small set of well known scalping ideas (for example order book imbalance, short term mean reversion) run through the simulator and the gate.
**Done when:** each candidate has a scored result on recorded data, and the gate's verdict is recorded, pass or fail.
**Carried over from an article (2026-10-04), for the design to weigh:** a regime gate. Scalping and mean reversion tend to work in calm, rangebound markets and get run over in strong trends. Consider pausing new entries when volatility or trend strength (from feature 20's context or the candles) crosses a level, added as one more reason in `entries_blocked` (spec 0006) so the dashboard shows it. Score each candidate with and without the gate. A standard measure of trend strength is ADX(14) (the average directional index): above about 25 is the common reading for a trending market, the case the gate would block. Treat 25 as a starting point to tune on recorded data, not a fixed rule.
**Also from an article (2026-10-04, quantum-algo scalping guide, promotional, its 75 % win rate at 2.3:1 claim is unverified and ignored):**
- **Session filter:** scalp only in the busy hours (for BTC perps roughly the Europe and US overlap), when spreads are tightest. Check which hours actually have the tightest spread and most volume on our own recorded data before fixing a window.
- **VWAP bounce:** with the trend set by the 9 and 21 EMA on 1 minute candles, enter when price pulls back to the day's VWAP and holds. Simple, few settings, a good early candidate against the fee hurdle.
**Also from an article (2026-10-04, monkeytrade scalping guide, forex focused, its pip sizes do not carry over and its 55 to 65 % win rate claim is unverified):**
- **Spread cap:** skip an entry when the spread is wider than a limit. Set the limit from our recorded order book data, not the article's forex number; with targets this small a wide spread alone erases the edge.
- **Time stop:** exit when price has not moved within 3 to 5 candles. It is the same kind of exit as the placeholder's timed exit (spec 0006).
- **EMA pullback:** on 1 minute candles, 8 EMA above 21 EMA, price pulls back to the 8 EMA with RSI(7) between 30 and 50, enter on a close back above the 8 EMA; stop past the 21 EMA, target 1.5 to 2 times the risk. A natural pair to compare with the VWAP bounce.
**Also from an article (2026-10-04, NinjaTrader 1 minute scalping, CME focused; its 1 to 5 tick targets cannot clear Kraken's fees, see feature 15's fee hurdle):**
- **Cumulative delta:** a running total of aggressive buying minus aggressive selling (a trade at the ask adds, one at the bid subtracts). When price keeps rising but delta flattens or falls, the move is losing its buyers. Try it two ways: as a filter that stops other candidates from chasing a fading move, and as a reversal candidate of its own. The recorder already stores trade ticks with their aggressor side, so delta comes from recorded data. It measures actual trades, unlike order book imbalance, which measures resting orders.
**Also from an article (2026-10-08, `strategy.md`, a general scalping compendium with no backtests or fees; its account sizes, prop firm rules, Nasdaq hours, and pip stops do not carry over):**
- **Liquidity sweep and structure shift:** price runs past a recent 5 minute high or low and is rejected, then a 1 minute close breaks the nearest micro swing the other way. Enter with a limit order back at the broken level, stop just past the sweep's wick. The recorder sees a sweep directly in the trades and the book, not only in candles. Swings need confirming bars, so the look ahead warning in feature 13 applies.
- **Bollinger and RSI mean reversion:** Bollinger Bands (20, 2) and RSI(14). Buy when price closes back inside the lower band with RSI under 30 (short in the mirror case), take profit at the middle band, and trade only while the bands are flat. Flat bands are a second range test to compare with the ADX regime gate above. Also worth a try on 15 minute candles in feature 17.
- **Scaled exit:** close half at 1R, move the stop to breakeven, let the rest run to the opposite 5 minute high or low. Score it against a single exit; breakeven stops often lower the average trade once fees count.
- **VWAP break and retest:** the short side mirror of the VWAP bounce: price breaks below VWAP, bounces weakly back to it, and is rejected. MACD histogram fading is offered as a confirmation; it adds free parameters, so test it for overfitting.
- [ ] Design it (spec): `/architect scalping strategy candidates`

### 17. Hour level strategy candidates · needs a decision
The fallback set: trend following and mean reversion on 1h to 4h candles, tested in the candle lab.
**Done when:** each candidate has a scored result over several years of candles, and the gate's verdict is recorded.
**Carried over from research (2026-10-04), a candidate to weigh:** Al Brooks style price action on 1h candles.
- **Context:** the 20 period EMA, with a trend or range tag from how many of the last 15 closes sit above it.
- **Entry:** the second pullback entry in a trend, called H2 when buying and L2 when selling (the second bar that breaks the prior bar's high or low during a pullback).
- **Reversal:** a three push wedge.
The trend or range tag could also serve as a regime filter for other candidates.
**Also from research (2026-10-04, altFINS chart patterns):**
- **Candidates:** breakouts from chart patterns that can be detected by rule: double tops and bottoms, channels, triangles, flags, and horizontal support and resistance. The rule is to trade only in the trend's direction and to require above average volume on the breakout bar.
- **Exit template:** close at the pattern's target, after N candles, or when price moves one standard deviation against the trade, whichever comes first.
- **Skip:** harmonic and Fibonacci patterns (Gartley, butterfly, ABCD). They have many free ratios to tune, which invites overfitting.
- altFINS's published success rates (for example 82 % for double bottoms) do not define "success", ignore fees, and say nothing about the size of losses. Our own gate measures these instead.
**Also from research (2026-10-04, RSI strategies):**
- **RSI pullback in a trend:** buy when price is above its 50 period average and RSI(14) dips into 30 to 40, then crosses back up. Sell short in the mirror case (RSI 60 to 70 in a downtrend).
- **Short period RSI mean reversion:** RSI with a period of 2 to 6 (the Connors style) is a well known test. Try it on 1h, and on 15 minute candles for feature 16 only if it clears the fee hurdle.
- **Layered RSI:** use RSI(21) above or below 50 as the bias, RSI(14) for the signal and RSI(7) for timing. Several periods mean several free parameters, so test it for overfitting.
- **Divergence** (price makes a new low while RSI does not) needs confirmed swings. The look ahead warning in feature 13 applies.
- **Ignore** the article's accuracy claims (62 %, 78 %, 91 %). They have no method and no fees. The article itself ranks crypto as the weakest market for RSI.
**The engineer's plan (2026-10-04): combine price action, RSI, and MACD into one strategy.** Build it in layers, each with one job:
- **Context:** price action (the 20 EMA trend or range tag, swings) decides whether to trade and in which direction.
- **Setup:** an RSI pullback (RSI(14) dipping into 30 to 40 in an uptrend) says the price is cheap within the trend.
- **Trigger:** a MACD (12, 26, 9) signal line cross, or the histogram turning, in the trend's direction times the entry.

A simple MACD only baseline to beat first (from the TradingView script "MACD Aggressive Scalp Simple", which has no stops and no published results): enter when the MACD (12, 26, 9) histogram crosses zero in the direction of the 50 period EMA, and exit when the histogram weakens. The layered strategy has to beat this baseline after fees to earn its extra settings. On short candles the histogram flips often, so check its trade count against the fee hurdle.

Two cautions. RSI and MACD are both built from recent price momentum, so they often agree simply because they measure the same thing; one should not be counted as confirming the other. Each layer also adds settings to tune, so the backtest should add one layer at a time and keep it only if it improves the out of sample result after fees, with the overfitting test from feature 15 applied to the final combination.

**Also from an article (2026-10-04, cryptoanalysislab market structure guide, course marketing, no rules or results):** a liquidity sweep candidate. Price runs past the prior day's high or low, closes back inside, then breaks the last swing in the other direction; the stop goes beyond the swept level. The article gives no swing definition, so we pick a fixed bar count and the look ahead warning in feature 13 applies.

**Also from Fidelity's chart pattern webinar (2026-10-04, by Charles Kirkpatrick, CMT, drawing on his textbook *Technical Analysis: The Complete Resource*):**
- **Breakout confirmation:** a pattern counts only once price breaks out, and many breakouts are false. Every breakout candidate should use one confirmation filter (two closes beyond the level, a minimum percent, or a wait of N bars) and the backtest should compare that filter with none.
- **Failed breakout reversal:** when a breakout fails and price comes back through the level, reverse (a stop and reverse at the far side of the breakout bar). This is the same idea as the liquidity sweep candidate above, so test them as one candidate.
- **Volatility contraction:** an NR4 bar (narrower range than the three before it) or an inside bar, then enter on a break of its high or low. Few settings and fully rule based, so a good early candidate, and a possible 15 minute variant for feature 16.
- **Skip single candle patterns:** the source rates the hammer, hanging man, harami and doji at average or below, often breaking either way at random.
- [ ] Design it (spec): `/architect hour level strategy candidates`

## Slice 8: Records

### 18. Trade journal export · Beta
Export every fill, fee, and funding payment as CSV for your own analysis and your Dutch tax records.
**Done when:** you can export any date range to CSV, and its totals match the P&L on the dashboard.
- [ ] Build it: `/develop trade journal export`

## Slice 9: Live

### 19. Go live with small capital · needs a decision
Switch one promoted strategy from paper trading in demo to your live Kraken futures account with under €1k and max 2x leverage.
**Done when:** live trading only starts if the strategy passed the gate and features 9 to 12 are done, capital and leverage caps are enforced, and demo and live can never be confused.
**Carried over from specs 0001 and 0006:**
- **Dead man's switch:** confirm Kraken's switch (cancel all orders after a timeout) and use it. It moved here because demo sends nothing to Kraken.
- **The live canary step:** with the smallest size, place and cancel a far off order, then open and close a position with a stop. Every state must match Kraken's records.
- **Stops:** choose the live stop trigger type (mark or last price), and decide whether a partial entry needs a resized stop.
- **Fees and funding:** check the current fee tier, and record funding from Kraken's account log.
- **Latency (from spec 0008):** the canary should measure real insert and cancel latency and replace the 25 ms default in the lab's run files.
- [ ] Design it (spec): `/architect go live with small capital`

## Deferred
Out of scope for the current build pass, kept so the plan stays honest.
- **More instruments**: perps beyond BTC and ETH, with a liquidity filter · needs a decision
- **AI or ML strategies**: model based predictions, only after the lab can catch overfitting · needs a decision
- **Scale up**: larger capital or leverage after a long profitable live record · needs a decision
- **Spot or other exchanges**: diversify venues · needs a decision

## Legend

**The decision box.** Every feature carries exactly one, the sub-task whose label ends with `(spec)`. Its wording varies (`Design it (spec)` normally, `Decide the stack (spec)` on Stack & architecture), so skills locate it by that `(spec)` suffix, never by an exact label. Every other box is an execution box and `/architect` never ticks one.

**Feature lifecycle**: the scope updates as a feature moves; each row is what it shows and who sets it:

| State | Set by | The feature shows |
|---|---|---|
| `planned` · needs a decision | `/scope` | one box: `Design it (spec): /architect <feature>` |
| `in-progress` (designed) | **`/architect` at spec capture** | `Design it` ticked; spec linked; `Build it: /develop <feature>` + **2 to 5 milestones**; the tier's closing boxes (`Verify it` Alpha+, `Test it` Beta+, `Review it` + `Document it` GA); any surfaced follow-up enrolled |
| `in-progress` (building) | `/develop` | milestone sub-boxes tick one by one; code pointer filled |
| `in-progress` (verified) | `/check verify` | `Build it` + milestones ticked; `Verify it` ticked |
| `done` | **you, when you decide it is** (any skill sets it when you say so); `/sync` reconciles | boxes you ran ticked, skipped ones marked skipped; the tier's last stage (`Prototype` → after `/develop`; `Alpha` → after `/check verify`; `Beta`/`GA` → after `/test`) is the suggested point to call it done; `/sync` captures conventions |

- **Next step** = the first unticked box (always a command or a tracked milestone).
- **needs a decision** = run `/architect` first; otherwise straight to `/develop` (or `/audit` for standards & tooling). The tag drops once the spec is captured.
- **Atomic build tasks live in the spec's `## Build plan`, not here**: the scope carries only the milestone rollup.
- **Status** `planned` → `in-progress` → `done`, plus `existing` (pre-workflow) and `dropped` (de-scoped, kept for history).
- **Approach tag** beside a heading (e.g. `· Facade`) overrides the project default for that feature; no tag = inherits it.
- **Workflow tier tag** beside a heading (e.g. `· GA`, `· Prototype`) sets that one feature's rigor above or below the project default; no tag inherits the default. It decides the feature's check boxes and each skill's next suggestion.
- **Workflow** (header line) is the project default, what runs after `/develop`: **Prototype** = nothing (trust develop's own build time self check); **Alpha** = `/check verify`; **Beta** = `/check verify` then `/test`; **GA** = adds a fresh model `/check review` then `/document`. A feature built on an unratified decision (an `Assumed` spec) stays flagged, but that never blocks `done`.
- **Pointer line** (`spec <n> · code in <path>`): the spec link added by `/architect`, the code path by `/develop`.
