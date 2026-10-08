# 0001. Build the bot on NautilusTrader in Python, as three processes on one London VPS

**Date**: 2026-10-01
**Amended**: 2026-10-01 (runtime contracts matched to NautilusTrader 1.231: Redis key format, log format, clock check source, Intel Mac development; see [rationale.md](rationale.md#amendment-2026-10-01)). Amended again 2026-10-01: demo redefined after Kraken shut down its futures demo, and Docker Desktop on the laptop; see [rationale.md](rationale.md#amendment-2026-10-01-b)
**Status**: Accepted

## Summary

The bot is written in Python on NautilusTrader (a trading framework with a fast Rust core), so the same strategy code runs in backtests, in replays of recorded order books, and live on Kraken. It runs as three separate programs (trader, recorder, dashboard) in Docker on one small VPS in London, next to Kraken's servers. Postgres keeps the business records, Redis keeps live state and carries dashboard commands, and recorded market data is stored as compressed Parquet files (a column based file format). The dashboard is private: only reachable over your Tailscale network (a private network between your own devices).

## Decision

**Chosen option**: Option 1: NautilusTrader on Python, three processes, Docker Compose on a London VPS.

Build every part of the bot in Python on an exactly pinned NautilusTrader release, split into `trader`, `recorder`, and `dashboard` processes that share Postgres, Redis, and a Parquet catalog, packaged with Docker Compose and hosted on an OVHcloud London VPS reached only through Tailscale.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `htmx` (`damusix/skills`, `.claude/skills/htmx/`) · `fastapi` (`terminalskills/skills`, `.claude/skills/fastapi/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `tailscale` (`terminalskills/skills`, `.claude/skills/tailscale/`)

## Proposed stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python: CPython 3.14 (the newest the pinned NautilusTrader release ships wheels for), locked with uv | Strategy research and live code share one language; the Rust core covers the hot path. |
| Trading engine | NautilusTrader 1.231.0, pinned exactly, using its Kraken Futures adapter (and its sandbox execution client in demo) | One event driven engine for backtest, L2 replay (order book depth replay) with fee, latency, and queue models, paper, and live. |
| Process layout | Three processes: `trader`, `recorder`, `dashboard` | A recorder crash or dashboard deploy never touches the process holding positions. |
| Repo layout | One uv workspace: `packages/core` (shared domain, DB models, config), `apps/trader`, `apps/recorder`, `apps/dashboard` | Shared types, one lockfile, one small image per app. |
| Operational database | PostgreSQL in a container on the VPS | Orders, fills, positions, funding, strategy runs, risk events need transactions and safe access from three processes. |
| Database access | SQLAlchemy 2 (async for the dashboard) + Alembic migrations | Typed models plus versioned migrations; hand written SQL for reports and P&L aggregates. |
| Market data store | Nautilus Parquet data catalog on local disk; DuckDB for ad hoc research queries | The replay simulator reads it directly with no conversion; compressed columns keep daily cost low. |
| Live runtime state | Redis as the Nautilus cache database | Nautilus reloads orders and positions on restart, then reconciles with Kraken (feature 11). |
| Dashboard to trader commands | A Redis stream (`control` stream, one consumer group for the trader) | Durable, ordered, acknowledged: a stop sent during a trader restart is still read when it comes back. |
| Dashboard | FastAPI + HTMX + Server Sent Events, TradingView Lightweight Charts, responsive layout | One language, no JS build step, live P&L pushed to laptop and phone. |
| Access | Tailscale only (host install, dashboard published with `tailscale serve`, no public ports), plus an app login inside it | The kill switch page never faces the internet. Login design belongs to feature 5. |
| Packaging | Docker Compose, the same file for laptop and VPS, restart policy `unless-stopped`; on the Intel Mac laptop the Docker engine is Docker Desktop 4.41.2 | One way to run everything; the laptop runs in demo mode. NautilusTrader has no Intel Mac wheel, so it only ever runs in Linux containers. |
| Hosting | OVHcloud VPS in London, about 2 vCPU, 4 GB RAM, 80 GB+ disk, under €20/month | Same metro as Kraken's matching engine in Equinix London. Measure the round trip before committing (Follow-up). |
| Time sync | chrony on the VPS host keeps the clock right; each process checks its offset at startup with an SNTP query | Order timestamps, latency numbers, and signed API requests depend on an accurate clock. The SNTP check works the same on laptop and VPS. |
| Observability | Logs to stdout from every process: our own code writes JSON, NautilusTrader's engine lines stay plain text; Docker `json-file` log rotation | No extra services on a small box; Nautilus only writes JSON to a file. Alerts and heartbeats belong to feature 12. |
| Backups | restic (encrypted) to Backblaze B2 (EU region): nightly `pg_dump` plus closed Parquet hours | Fills and tax records survive a lost VPS, stored with a different company. |

### How the pieces talk

```
Kraken Futures (WS + REST)
   │  market data            │  market data + private orders/fills
   │                         │  (demo: orders/fills come from the sandbox, never Kraken)
   ▼                         ▼
recorder (Nautilus node,  trader (Nautilus node: strategies, risk,
 data clients only)        execution) ──┬── Redis: Nautilus cache (live state)
   │                                    │   Redis: `snapshot` keys (position, P&L)
   ▼                                    │   Redis: `control` stream ◄── dashboard
Parquet catalog (disk) ──► backtests    └── recording actor ──► Postgres
                                                                   ▲
dashboard (FastAPI + HTMX) ── reads Postgres + Redis snapshot, SSE ┘
```

- **Trader records through an actor.** A Nautilus Actor inside `trader` subscribes to order, fill, position, and account events and writes them to Postgres off the event loop's hot path (a background writer with a bounded queue). If Postgres is unreachable or the queue is full, records spill to a local append only file on a volume (replayed into Postgres on recovery) and the trader blocks new entries until the spill drains; a fill is never dropped. Runner up: exporting from the Nautilus cache on a schedule, rejected because the dashboard would lag and a crash could lose the tail.
- **Recorder is its own Nautilus node** with data clients only, recording L2 book deltas and trades for BTC and ETH perps through Nautilus's streaming (feather) writer, converted into the Parquet catalog once per closed hour. Readers never touch the open hour, so a crash loses at most that hour. Runner up: record inside `trader`, rejected because it couples a disk problem to trading.
- **Dashboard reads, never trades.** History comes from Postgres, live position and P&L from plain JSON snapshot keys the trader writes to Redis, pushed to the browser over SSE. The dashboard does not import NautilusTrader. Its only write path to the trader is the `control` stream.
- **Local development** runs the same Compose stack on your laptop with `TRADING_ENV=demo`. The laptop is an Intel Mac on macOS 12: NautilusTrader ships wheels only for Linux, Windows, and Apple Silicon Macs, so anything that imports NautilusTrader, tests included, runs inside containers. The Docker engine is Docker Desktop 4.41.2, the last release that installs on macOS 12 (4.42.0 requires macOS 13.3); keep its auto update off, since newer releases will not run there. uv runs on the host for locking only.

### Runtime contracts (settled here so the scaffold does not invent them)

- **Versions.** The scaffold resolves the newest stable NautilusTrader release and the newest CPython it ships wheels for, pins both exactly, and writes the resolved numbers into the Language and Trading engine rows above. One uv lock for the whole workspace; `uv lock` is the scaffold's first step (Nautilus pins numpy, pyarrow, and msgspec, and everything else resolves against them). Research tools (DuckDB, notebooks) live in an optional `research` dependency group. The workspace sets `[tool.uv] environments = ["sys_platform == 'linux'"]` so the lock resolves against the Linux wheels the containers use, even when `uv lock` runs on the Intel Mac.
- **Environment switch.** One variable, `TRADING_ENV` (`demo` or `live`). Every process refuses to start if it is unset or unknown. Non secret config loads through pydantic-settings in `packages/core`; secrets are feature 4. Kraken shut down its futures demo environment on 14 July 2026, so both environments read the same market data and differ in where orders go:
  - **Market data, both environments:** the Kraken Futures data client always uses `KrakenEnvironment.LIVE` (Kraken's real public feeds, no API keys needed).
  - **`demo`:** orders go to NautilusTrader's sandbox execution client, which simulates fills, fees, and positions locally against that live data. No money is ever at risk. Its settings (starting balance, leverage, fee model) belong to feature 6.
  - **`live`:** orders go to the Kraken Futures execution client with real keys (feature 4).
  - **Demo can never trade for real.** With `TRADING_ENV=demo`, no node builds a Kraken execution client or reads a Kraken API key. The sandbox is the only execution client allowed in demo. One guard function in `packages/core`, called by every node builder, enforces it:
    - **What it checks.** The final `TradingNodeConfig`, before `build()`, against an allowlist of execution clients. Trader in demo: none (scaffold) or only the sandbox (feature 6 on). Trader in live: none until feature 4 and the live canary, then only Kraken. Recorder, in every environment: none, ever.
    - **Keys in the environment.** The Kraken adapter reads `KRAKEN_FUTURES_API_KEY`/`_SECRET` and `KRAKEN_FUTURES_DEMO_API_KEY`/`_SECRET` from the environment on its own when its key fields are empty (checked in 1.231.0). So in demo, and for the recorder in any environment, a node refuses to start if any of those variables is set and not empty. `.env.example` never lists Kraken keys; live keys exist only on the VPS, never on the laptop. The dashboard and backup never receive Kraken keys; only `trader` in live does (feature 4).
    - **When and how it fails.** It runs in `boot` right after settings validate, before the clock check. On refusal it logs one JSON error with `code` `env_guard_refused` and the reason, then exits with status 1. Each node has a unit test for it.
- **Redis.** AOF persistence (`appendonly yes`, `appendfsync everysec`) on a named volume, `maxmemory-policy noeviction`. Demo and live state can never collide, in two ways:
  - **NautilusTrader cache keys** have a fixed format, `trader-<TRADER_ID>:…`, which cannot take a custom prefix. So the environment lives in the node id: `TRADER-DEMO` / `TRADER-LIVE` for the trader, `RECORDER-DEMO` / `RECORDER-LIVE` for the recorder (Nautilus ids are `NAME-TAG`; two nodes never share one). Trader keys look like `trader-TRADER-DEMO:…`. Each node derives its id from `TRADING_ENV`; if an id is configured explicitly, the node refuses to start unless its tag (the part after the first `-`, compared case insensitively) equals `TRADING_ENV`.
  - **Our own keys** (snapshots, control stream, anything else we write) are prefixed with environment and process: `demo:trader:`, `demo:recorder:`, `demo:snapshot:`. One helper in `packages/core` builds both the uppercase id tag and the lowercase prefix from `TRADING_ENV`.
  - **Trader cache settings:** `CacheConfig(database=DatabaseConfig(type="redis"), use_trader_prefix=True, use_instance_id=False, flush_on_start=False)`, so keys stay stable across restarts and the trader reloads its state. Redis host and port come from `REDIS_HOST` (default `redis`) and `REDIS_PORT` (default `6379`). The Nautilus message bus is not backed by Redis.
  - The recorder node keeps its Nautilus cache in memory only (no Redis database), since it holds no orders or positions.
  - **Redis unavailable at boot:** the trader refuses to start and Docker's restart backoff retries it (Compose already waits for Redis to be healthy).
  - **Flushing the trader's cache** (the Nautilus upgrade procedure) deletes only `trader-TRADER-<ENV>:*` keys for that environment, never `FLUSHDB`/`FLUSHALL`, which would wipe the other environment and the control stream.
- **Control stream.** Stream `{env}:control`, consumer group `trader`. Message fields: `id` (UUID), `cmd` (`stop`, `resume`, `flatten`), `issued_at`, `expires_at`, `schema_version`. Delivery is at least once, so every handler is idempotent on `id`; an expired command is acknowledged and logged, not executed. The trader writes each outcome to `{env}:control:results`, and the dashboard shows a command as confirmed only when its result arrives. Trimming (`MAXLEN ~`) never removes unacknowledged entries.
- **Compose services.** `postgres`, `redis`, `migrate` (one shot `alembic upgrade head`; models live in `packages/core`; the scaffold ships one empty baseline revision, tables are feature 3), `trader`, `recorder`, `dashboard`, `backup`. `trader` and `dashboard` wait on `depends_on: condition: service_healthy` plus `migrate` completing. Catalog volume: read and write for `recorder`, read only for `trader` and `dashboard`. `trader` gets the lowest OOM score so the kernel never picks it first, and a 30 s stop grace period to finish in flight writes. Each service sets `PROCESS_NAME` (its service name).
  - **Health checks:** `postgres` via `pg_isready`, `redis` via `redis-cli ping`. `trader` and `recorder` each run a small heartbeat actor that touches a file every 10 s; healthy means the file is under 30 s old. `dashboard` is healthy when `/healthz` answers 200. `backup` is a stub until feature 4 supplies its keys: it idles, logs "backup not configured", and counts as healthy.
  - **Memory limits:** trader 1 GB, recorder 768 MB, postgres 512 MB, redis 256 MB, dashboard 256 MB, migrate 256 MB, backup 128 MB.
  - **Postgres credentials:** the scaffold uses a development only password from a gitignored `.env` (with `.env.example` committed). Real secret handling is feature 4.
- **Network exposure.** Tailscale runs on the host. The dashboard binds to `127.0.0.1` only and is exposed to the tailnet with `tailscale serve`; no service ever binds `0.0.0.0`.
- **Time.** UTC everywhere: containers, logs, Postgres `timestamptz`. Nautilus nanosecond timestamps are stored as `bigint` beside any `timestamptz`. chrony on the VPS host keeps the clock right; the startup check does not read chrony (it is absent on the laptop and inside containers).
- **Clock check.** At startup each process measures its clock offset with an SNTP query (a small helper in `packages/core`, no extra dependency or service). It queries every server in `CLOCK_SERVERS` (default `0.pool.ntp.org,1.pool.ntp.org,2.pool.ntp.org`) with a 2 s timeout each, and takes the median offset of the servers that answer; one answer is enough. Containers share the host kernel clock, so this measures the host. The limit is `MAX_CLOCK_OFFSET_MS` (default 250). The check runs at startup only; watching drift while running belongs to feature 12. The VPS firewall allows outbound UDP 123.
  - `trader` and `recorder`: offset above the limit → refuse to start, in both environments. No server answers → with `TRADING_ENV=live`, refuse to start; with `demo`, log a warning and start.
  - `dashboard`: logs the result as a warning and always starts, because the page with the stop button must come up even when time servers do not answer.
- **Logs.** Everything goes to stdout, rotated by Docker. Our own code (config, clock check, actors, dashboard, and uvicorn's loggers) writes one JSON object per line through a stdlib logging formatter in `packages/core`, with fields at least `ts` (UTC ISO 8601), `level`, `process` (from `PROCESS_NAME`), `env` (from `TRADING_ENV`), `logger`, `msg`. `LOG_LEVEL` sets the level for our code and Nautilus alike (default `INFO`). NautilusTrader's engine lines stay in its plain text format, because it only writes JSON to a file. A Nautilus JSON file sink can be added later if feature 12 needs to parse engine lines.
- **Backups.** The `backup` container runs restic to an encrypted repository on Backblaze B2: nightly `pg_dump` and the closed Parquet hours. A restore drill runs monthly. Redis is not backed up; the exchange is the source of truth for live state.
- **What "boots" means for the scaffold.** `docker compose up` brings every service to healthy; `trader` starts a Nautilus node with no strategy and connects to Kraken's live public market data with a data client only, in either environment (no execution client yet: the sandbox arrives with feature 6, the Kraken execution client after feature 4 settles secrets and the live canary passes); `recorder` connects and writes its first feather stream file into the catalog (the scaffold looks up the BTC and ETH perp instrument ids from the adapter's instrument provider and pins them in config as `RECORDER_INSTRUMENTS`; conversion to hourly Parquet is feature 8); `dashboard` answers `/healthz`; `docker compose run --rm <app> pytest -q` on a dev image target passes one smoke test per app (tests run in containers, since NautilusTrader does not install on the Intel Mac). Linting and CI belong to feature 2.

### Owned by later specs (do not decide these while building this feature)

Secret storage and key handling (feature 4), table design (feature 3), dashboard login and visual design (feature 5), deploy and restart mechanics (feature 7), recorder gap detection and retention (feature 8), kill switch, loss limits, leverage caps, and reconciliation rules (features 9 to 11), alerts (feature 12).

## Consequences

**Positive**:
- Backtest, replay, paper, and live share one strategy codebase, which is the scope's parity rule (feature 13) for free.
- Much of features 13 and 14 (L2 replay, fee and latency models) and the groundwork for 11 (state reload, reconciliation hooks) comes from the framework; the adapter's reconciliation is young, so feature 11 still needs its own tests.
- Everything runs on one box for under €20/month plus a few euros of backups, with the same setup on your laptop.
- Demo runs on real prices and real order book depth, so paper results reflect the actual market rather than a thin demo book.

**Negative / tradeoffs**:
- The NautilusTrader Kraken adapter is young; expect to patch or work around gaps (order types, rate limit handling). The first Tracer Bullet slice (feature 6) exists partly to prove it.
- Demo fills are simulated, so real order behaviour (rejections, exchange side stop triggers, partial fills, reconciliation against Kraken) is first seen at the live canary step (Follow-up), not in demo. The sandbox's fill model ignores queue position and latency and may flatter a scalping strategy, so the promotion gate (feature 15) must not accept sandbox profit alone as evidence; it needs L2 replay with queue and latency models as well.
- In demo, trader and recorder both pull Kraken's live public feeds from your laptop's IP and share its rate limits.
- NautilusTrader is a large framework with its own concepts (nodes, actors, message bus, cache) and a fast release pace; upgrades need a deliberate test pass, which is why the version is pinned exactly.
- One VPS is a single point of failure: a host outage stops trading and recording together. Exchange side stops (feature 6) and alerts (feature 12) are the mitigation, not redundancy.
- Redis is one more always on service; while it is down the trader cannot persist live state or receive commands, so it keeps managing open positions and exchange stops but blocks new entries until Redis returns.
- `trader` and `recorder` both connect to Kraken from the same IP, so they share per IP rate limits; the recorder uses only public market data and must stay within its share.
- A Nautilus upgrade can change the cache format: upgrades pass a replay regression test first, then flush the trader's Redis keys and reconcile from the exchange on the next start.
- Two Nautilus nodes, Postgres, Redis, and the dashboard use roughly 2.5 to 3 GB of a 4 GB box; memory limits and swap are required, not optional.
- On the Intel Mac laptop, NautilusTrader only runs in Docker Desktop's Linux VM; the edit, run, test loop goes through containers. Docker Desktop is frozen at 4.41.2 there (no further security updates) and is free only for personal use and small businesses. An Apple Silicon or Linux machine would remove both limits.
- The log stream mixes JSON (our code) and plain text (Nautilus engine), so anything parsing logs must skip lines that are not JSON.
- Demo and live share one Redis database when they run on the same box, so isolation rests on key names; a careless `FLUSHDB` would wipe both. In practice the laptop runs demo and the VPS runs live.
- In live, if no time server answers, trader and recorder keep failing to start and Docker keeps retrying with backoff; feature 12's alerts must catch that loop.
- Python keeps the strategy layer slower than pure Rust; fine at a seconds timescale, a ceiling at sub millisecond timescales (out of scope).

**Neutral**:
- Parquet files grow every day; retention and size per day are set in feature 8.
- A 4 GB box is tight once backtests run next to live trading; heavy research runs on the laptop against a copy of the catalog.

## Follow-up

- [ ] **Gate before feature 6:** confirm Kraken Futures offers a dead man's switch (cancel all orders after a timeout) and how to use it with the Nautilus adapter. It is the real protection if the VPS dies with orders open; exchange side stops alone do not cover resting entry orders.
- [ ] Before paying for the VPS, measure the round trip to `futures.kraken.com` from a trial OVHcloud London box (and one alternative such as Vultr London); pick the lower median and jitter.
- [ ] Measure the recorder's compressed bytes per day on the laptop for a day before choosing the VPS disk size (80 GB may fill within weeks); feature 8 sets retention. Spec [0007](../0007-order-book-recorder/index.md) measured the raw rate (about 0.75 GB/h average, 2 GB/h peak) and sets retention; its milestone 1 measures the Parquet bytes per day.
- [ ] Check the pinned NautilusTrader Kraken adapter covers L2 book subscriptions and batch orders, and that the sandbox execution client works with Kraken `PF_` instruments; record gaps as issues before feature 6. **Gate:** if the sandbox does not work with `PF_` instruments, feature 6 goes back to `/architect` rather than working around it.
- [ ] **Live canary step before go live (feature 17):** with the smallest size on the live account, place and cancel a far off order, then open and close the smallest position with an exchange side stop. It proves the real Kraken order path that demo no longer exercises. It passes when every order state change matches Kraken's records and no order or position is left open. Run it again after every NautilusTrader upgrade. Size and price distance are set in feature 17's spec. `/scope` adds it as a sub task.
- [ ] Confirm Kraken's live public futures feeds (`futures.kraken.com` REST and WebSocket) answer from your laptop's connection in the Netherlands, and that your live account is eligible for Kraken futures, before feature 6.
- [ ] Laptop Docker setup check: `docker version` reports Docker Desktop 4.41.2, the VM has at least 4 GB of memory (Settings, Resources), and "Automatically check for updates" is off (Settings, Software updates).
- [ ] `/scope` follow ups from the demo shutdown: features 6, 9, and 17 say "demo environment" in their Done when lines (demo now means live data with sandbox execution), and feature 4's "separate demo and live keys" no longer applies, since demo needs no Kraken keys.
- [ ] Run `/audit` after the scaffold so root `AGENTS.md` records the real stack, and lists these installed skills under `## Agent skills`: nautilus-trader, htmx, fastapi, sqlalchemy, alembic, postgres-writing-guidelines, redis, uv, docker-compose-skill, tailscale. Record MCP servers as declined in favour of CLIs (psql, redis-cli, docker, tailscale, uv).
- [ ] Area scoped skills belong in nested files once those folders exist: `htmx` and `fastapi` in `apps/dashboard/AGENTS.md`; the rest are project wide (root).

## Rationale

Reasoning, options, and references: see [rationale.md](rationale.md).
