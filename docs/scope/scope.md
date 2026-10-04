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
| 6 | Core paper trading loop | Slice 1 | planned |
| 7 | Always on cloud deployment | Slice 2 | in-progress |
| 8 | Order book recorder | Slice 3 | planned |
| 9 | Kill switch & loss limits | Slice 4 | planned |
| 10 | Leverage & liquidation guard | Slice 4 | planned |
| 11 | Restart & outage safety | Slice 4 | planned |
| 12 | Phone alerts & health checks | Slice 5 | planned |
| 13 | Candle backtest lab | Slice 6 | planned |
| 14 | Scalping simulator | Slice 6 | planned |
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
Keep Kraken API keys safe: least privilege keys (trade only, never withdraw), separate demo and live keys, never in the repo or logs.
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

### 6. Core paper trading loop · needs a decision
The thinnest real thread (this is the walking skeleton): live BTC perp data from Kraken's futures demo environment → one deliberately simple placeholder strategy → a risk check with fixed size and leverage caps → an order placed together with an exchange side stop → fills recorded → position and P&L shown on the dashboard.
**Done when:** on the demo environment the bot opens and closes a real position by itself, every entry has an exchange side stop, fills and fees land in the database, and the dashboard shows the live position and P&L.
- [ ] Design it (spec): `/architect core paper trading loop`

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

### 8. Order book recorder · needs a decision
Record BTC and ETH perp order book updates and trades continuously, because scalping can only be tested on this data and it takes weeks to collect.
**Done when:** the recorder runs 24/7, detects and logs gaps, and stored data can be replayed in order at a known size cost per day.
- [ ] Design it (spec): `/architect order book recorder`

## Slice 4: Safety guards (all required before live)

### 9. Kill switch & loss limits · needs a decision
One action closes all positions, cancels all orders, and stops trading; automatic halt after a daily or total loss threshold.
**Done when:** the dashboard stop button and an automatic loss limit each flatten everything on the demo environment within seconds, and trading stays halted until you resume it.
- [ ] Design it (spec): `/architect kill switch & loss limits`

### 10. Leverage & liquidation guard · needs a decision
Hard caps on position size, total exposure, and leverage (2x at first), plus a minimum distance between entry and liquidation price; funding costs counted in every decision.
**Done when:** any order that breaks a cap or sits too close to liquidation is rejected and logged, and funding payments show in P&L.
- [ ] Design it (spec): `/architect leverage & liquidation guard`

### 11. Restart & outage safety · needs a decision
After a crash, restart, or dropped connection, the bot rebuilds its view from the exchange's real positions and orders before doing anything else, and never sends a duplicate order.
**Done when:** killing the bot or the network mid trade, then restarting, leaves positions and stops correct with no duplicates, and a stale data feed pauses new entries.
- [ ] Design it (spec): `/architect restart & outage safety`

## Slice 5: Stay informed

### 12. Phone alerts & health checks · needs a decision
Push alerts to your phone for fills, limit hits, errors, and a missing heartbeat, so you know within a minute when something is wrong.
**Done when:** each alert type reaches your phone within a minute, and a stopped bot triggers an alert on its own.
- [ ] Design it (spec): `/architect phone alerts & health checks`

## Slice 6: The strategy lab

### 13. Candle backtest lab · needs a decision
Test hour level strategies on Kraken candle history with realistic fees, funding, and slippage.
**Done when:** a strategy can be backtested over any date range, results include fees and funding, and the same strategy code runs unchanged in the backtest and the paper loop.
- [ ] Design it (spec): `/architect candle backtest lab`

### 14. Scalping simulator · needs a decision
Replay recorded order book data to test scalping honestly: maker and taker fees, queue position, latency, and partial fills.
**Done when:** a scalping strategy replays over recorded days, results account for fees, queue position, and latency, and a known losing strategy shows a loss.
- [ ] Design it (spec): `/architect scalping simulator`

### 15. Strategy scoring & promotion gate · needs a decision
Score every strategy on return after costs, max drawdown, and a risk adjusted ratio (win rate shown, never ranked on), with out of sample testing to catch overfitting. Only strategies that pass move to paper, and only paper winners can go live.
**Done when:** the dashboard ranks strategies on those metrics, the gate blocks any strategy that fails out of sample or paper thresholds, and promotions are logged.
- [ ] Design it (spec): `/architect strategy scoring & promotion gate`

## Slice 7: First strategies

### 16. Scalping strategy candidates · needs a decision
A small set of well known scalping ideas (for example order book imbalance, short term mean reversion) run through the simulator and the gate.
**Done when:** each candidate has a scored result on recorded data, and the gate's verdict is recorded, pass or fail.
- [ ] Design it (spec): `/architect scalping strategy candidates`

### 17. Hour level strategy candidates · needs a decision
The fallback set: trend following and mean reversion on 1h to 4h candles, tested in the candle lab.
**Done when:** each candidate has a scored result over several years of candles, and the gate's verdict is recorded.
- [ ] Design it (spec): `/architect hour level strategy candidates`

## Slice 8: Records

### 18. Trade journal export · Beta
Export every fill, fee, and funding payment as CSV for your own analysis and your Dutch tax records.
**Done when:** you can export any date range to CSV, and its totals match the P&L on the dashboard.
- [ ] Build it: `/develop trade journal export`

## Slice 9: Live

### 19. Go live with small capital · needs a decision
Switch one promoted strategy from the demo environment to your live Kraken futures account with under €1k and max 2x leverage.
**Done when:** live trading only starts if the strategy passed the gate and features 9 to 12 are done, capital and leverage caps are enforced, and demo and live can never be confused.
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
