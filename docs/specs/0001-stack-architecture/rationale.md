# 0001. Rationale: stack and architecture

Decision record for [index.md](index.md). `/develop` does not read this file.

## Context

> ⚠️ Premise note: Scalping BTC and ETH perps on a seconds timescale from a retail fee tier is hard to make profitable: exchange fees and the spread take a large bite out of each small move, and firms with colocated servers sit ahead of you in the queue. That is not a reason to change the stack, but it means the promotion gate (feature 15) and the hour level fallback (feature 17) are what protect your money, and the stack must make honest cost modeling cheap. The largest technical risk in this decision is the maturity of the NautilusTrader Kraken adapter; the Tracer Bullet slice (feature 6) should prove it in demo (live data, sandbox execution) before anything else is built on top, and the live canary step proves the real order path before go live.

The project is a greenfield automated trading bot for Kraken Futures BTC and ETH perpetuals, run by one trader in the Netherlands. It needs a live trading loop with an exchange side stop on every entry, a 24/7 recorder of order book updates and trades (weeks of data are needed before scalping can be tested), a backtest lab for hour level candles, a replay simulator that models fees, queue position, latency, and partial fills, and a private dashboard with an always available stop button on laptop and phone.

Forces: latency matters for scalping, and Kraken's matching engine is in Equinix London. The budget is under €20/month for hosting. The scope requires that the same strategy code run unchanged in backtest and paper trading. Restart safety, duplicate order prevention, and reconciliation against real exchange state are hard requirements before live money. The builder can read Python, TypeScript, Rust, and Go, so language familiarity does not decide it. There is no official Kraken SDK; community clients and one framework adapter exist.

Not deciding blocks every later feature: the data model, secrets, dashboard, deployment, and simulator all depend on the language, the process layout, and where data lives.

## Options considered

### Option 1: NautilusTrader on Python, three processes, Docker Compose on a London VPS

Python strategies on NautilusTrader's Rust core, using its Kraken Futures adapter; `trader`, `recorder`, `dashboard` processes; Postgres, Redis, Parquet catalog (basis: NautilusTrader architecture, one engine for backtest and live).

**Pros**:
- Backtest, L2 replay, paper, and live share one engine and one strategy API.
- Ships the hard parts: order state machine, state reload from a cache database, reconciliation hooks, fee, latency, and queue models.

**Cons**:
- Young Kraken adapter; a large framework with a fast release pace to learn and pin.

### Option 2: Custom Python asyncio engine on python-kraken-sdk

A hand built event loop around the community Kraken SDK or raw WebSockets, with Postgres, and a separate hand built replay simulator.

**Pros**:
- Small surface you fully understand; no framework concepts to learn.
- Easy to adapt to any Kraken API quirk directly.

**Cons**:
- Weeks of work on the order state machine, reconnects, reconciliation, and a replay simulator, each easy to get subtly wrong with real money.
- Backtest and live parity must be engineered and kept true by hand.

### Option 3: Custom Rust engine

A Rust engine for trading and recording, with Python beside it for research.

**Pros**:
- Lowest and most predictable latency; strong types for order state.

**Cons**:
- Two languages for strategy work (research in Python, live in Rust) breaks the parity rule or doubles the code.
- Slowest iteration speed for a solo builder testing many strategy ideas.

### Option 4: TypeScript on Node or Bun

One language for bot and dashboard, with a community exchange client.

**Pros**:
- Single toolchain end to end; strong web dashboard ecosystem.

**Cons**:
- Weak numeric, backtest, and research ecosystem; a replay simulator would be built from scratch.
- Garbage collection pauses under heavy order book load.

## Rationale

The parity rule (same strategy code in backtest and paper) and the need for an honest scalping simulator are the forces that decide this. Option 1 is the only one where both come from the engine instead of being built and maintained by one person; Options 2 to 4 each spend weeks recreating what NautilusTrader already ships, and every hand built order state machine is a new way to lose money. The adapter risk is real but bounded: it surfaces in the first slice, in demo (live data, sandbox execution), where a gap costs time rather than capital, and the live canary step proves the real order path at minimum size (basis: Tracer Bullet, prove the riskiest path first).

Three processes instead of one follow from the safety requirements: the recorder and dashboard change and fail far more often than the trader, and neither should ever restart the process holding positions (basis: failure isolation). Redis earns its place twice, as the Nautilus cache database for restart recovery and as a durable command channel, so a stop sent while the trader restarts is not lost; an HTTP control endpoint would fail exactly when it is needed. Postgres covers the relational records (fills, fees, funding, tax export) where transactions matter (basis: a relational database as the default primary store), while market data goes to Parquet because that is what the replay simulator reads natively.

London hosting follows from Kraken's matching engine location; the €20 budget rules out the managed platforms and points to one OVHcloud VPS, with the ping measured before committing. Tailscale only access removes the internet facing login page from a system that can move real money (basis: minimize attack surface). Docker Compose keeps laptop and server identical without an orchestration platform one person would have to operate.

## Amendment 2026-10-01

Checking the runtime contracts against the real NautilusTrader 1.231.0 wheel, and against the development laptop, before scaffolding showed four places where the first version could not be built as written.

- **Redis key prefix.** `CacheConfig` only offers `use_trader_prefix` and `use_instance_id`; Nautilus keys are always `trader-<TRADER_ID>:…`. Chosen: put the environment in the trader id (`TRADER-DEMO`, `TRADER-LIVE`), with a startup guard that the id matches `TRADING_ENV`, and keep the `{env}:{process}:` prefix for our own keys. Runner up: a separate Redis instance per environment, rejected as one more service and more memory on a 4 GB box for the same isolation. The goal (demo and live can never collide) is unchanged.
- **Log format.** `LoggingConfig` writes plain text to stdout and JSON only to a file (`log_file_format="JSON"`). Chosen: our own code logs JSON to stdout, Nautilus engine lines stay text. Runner up: a Nautilus JSON file sink on a volume from day one, deferred until feature 12 needs to parse engine lines (basis: add infrastructure when a consumer exists).
- **Clock check source.** chrony exists only on the VPS host, not on the laptop or inside containers. Chosen: an SNTP query from each process, one code path everywhere; containers share the host clock, so it measures the same thing. If no server answers, live refuses to start and demo warns, so a flaky network on the laptop does not block development while live never trades on an unchecked clock. Runner up: read chrony's socket on the VPS and fall back to SNTP locally, rejected as two code paths to test. The dashboard only warns, never refuses: it signs no exchange requests, and the stop button must stay reachable during a time server outage (basis: the safety control must not depend on a noncritical check).
- **Cross check fill ins.** An independent read of the amended spec listed values the scaffold would otherwise invent (node ids, cache settings, SNTP rules, log field sources, health checks, memory limits, the baseline migration, instrument pinning, where tests run). Each was settled in the runtime contracts with the recommended default.
- **Intel Mac development.** NautilusTrader 1.231.0 ships wheels for CPython 3.12 to 3.14 on manylinux_2_35 (x86_64, aarch64), Apple Silicon macOS, and Windows, but not Intel macOS. Current Docker Desktop does not install on macOS 12. Chosen: Colima for the Docker engine (later replaced by Docker Desktop 4.41.2, see amendment (b)), Nautilus only inside Linux containers, and a uv lock restricted to Linux environments. Runner up: develop on a Linux VPS, rejected because the spec keeps one Compose setup on laptop and VPS.

## Amendment 2026-10-01 (b)

The first scaffold boot showed that Kraken decommissioned its futures demo environment (`demo-futures.kraken.com`) on 14 July 2026. The host now redirects to a Kraken marketing page, Kraken names no replacement, and NautilusTrader 1.231.0 still points its demo REST client at the old host, so the nodes could not load instruments.

**What `demo` connects to.** Options weighed:

- **Live public market data plus NautilusTrader's sandbox execution client (chosen).** Real prices and depth, no API keys, no money at risk; the sandbox simulates fills, fees, and positions with the same matching engine the backtests use. It ships in the pinned 1.231.0 release.
- **A tiny live account as demo.** Real orders and exchange side stops, but real money in every test, and it blurs the demo and live line the spec guards.
- **An exchange with a futures testnet (for example Bybit or OKX).** Free real order testing, but it abandons Kraken, the London latency reasoning, and the eligibility work for a trader in the Netherlands, and rewrites most of this spec.
- **Wait for a Kraken replacement.** Blocks the scaffold and feature 6 with no sign one is coming.

The chosen option keeps every safety property that does not depend on Kraken's demo: demo still cannot lose money, and it now cannot place a real order at all, which the startup guard enforces (runner up: config discipline only, rejected because demo and live now read the same market data host, so a mistake would be harder to notice). The cost is that the real Kraken order path is no longer exercised before live money, so a **live canary step** closes that gap: minimum size, place and cancel a far off order, then open and close the smallest position with an exchange side stop (runner up: let the first go live trade be the proof, rejected because the first real order would also be a strategy trade).

**The name stays `demo`.** Renaming to `paper` would churn node ids, key prefixes, settings, tests, and scope text for a clearer word; the meaning is redefined in the runtime contracts instead.

**Docker Desktop instead of Colima on the laptop.** On macOS 12, Homebrew has no prebuilt packages, so Colima's QEMU needed about 24 libraries built from source (an estimated 1 to 3 hours). Docker Desktop 4.41.2 is the last release that installs on macOS 12 (4.42.0 requires macOS 13.3), notarized and signed by Docker Inc, and ships the engine and Compose prebuilt. Tradeoffs: it is frozen at that version with no further security updates, and its license is free only for personal use and small businesses.

## References

**Project sources**:
- `docs/scope/scope.md`: feature 1 intent, the parity rule in feature 13, the simulator in feature 14, the safety slice (features 9 to 11), budget and region constraints
- Installed community skills listed in [index.md](index.md), `## Decision`

**Practices & standards**:
- Tracer Bullet delivery: prove the riskiest end to end path first
- Failure isolation between processes with different change rates
- A relational database as the default primary store; ORM for CRUD, SQL for aggregates
- Minimize attack surface for systems that can move money
- Pin fast moving dependencies to exact versions

**Links** (web verified during the design conversation):
- Kraken colocation guide: https://docs.kraken.com/exchange/guides/general/colocation
- Kraken Futures rate limits: https://docs.kraken.com/exchange/guides/futures/ratelimits
- python-kraken-sdk on PyPI: https://pypi.org/project/python-kraken-sdk/
- NautilusTrader: https://nautilustrader.io/
- Kraken derivatives demo decommission notice: https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives
- NautilusTrader sandbox execution client: https://nautilustrader.io/docs/core-nightly/nautilus_sandbox/execution/struct.SandboxExecutionClient.html
- Docker Desktop release notes (macOS minimums per release): https://docs.docker.com/desktop/release-notes/

## Evidence: landscape check (2026-10-01)

- Kraken's matching engine is in Equinix London; Hetzner has no London location, so cheap London options are OVHcloud, Vultr, DigitalOcean, and Lightsail.
- No official Kraken SDK; python-kraken-sdk (community, maintained) and ccxt cover Kraken Futures.
- NautilusTrader has a Kraken adapter covering Spot and Futures.
- freqtrade does not support Kraken futures, so it was excluded.
- For L2 storage on one box: Parquet read with DuckDB or QuestDB are practical; ClickHouse is heavy to operate at this size; TimescaleDB is row based and slower for book data.
- The check did not confirm a Kraken Futures dead man's switch endpoint; tracked as a Follow-up in [index.md](index.md).
