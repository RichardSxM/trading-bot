# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project scaffold: a uv workspace on Python 3.14 with a shared `packages/core` and three apps (`trader`, `recorder`, `dashboard`), built on NautilusTrader 1.231 with the Kraken Futures adapter (see spec 0001).
- One Docker Compose stack for the laptop and the VPS: Postgres, Redis, a `migrate` job (Alembic baseline), the three apps, and a `backup` placeholder until feature 4. Every service has a health check and a memory limit. Only the dashboard publishes a port, on `127.0.0.1`.
- `TRADING_ENV` (`demo` or `live`) as the single environment switch. Every process refuses to start when it is unset or unknown. Both environments read Kraken's live public market data, with no execution client yet.
- An env guard that runs before every node build. It refuses any execution client not on the allowlist (empty for now). It also refuses Kraken API keys in demo, and for the recorder in any environment.
- A startup clock check over SNTP (limit 250 ms by default). A node refuses to start when its clock is too far off; the dashboard only warns.
- Demo and live kept apart in Redis through the node ids (`TRADER-DEMO`, `TRADER-LIVE`, and the same for the recorder). A node refuses to start when an explicit id does not match `TRADING_ENV`.
- The trader runs a Nautilus node with a Redis backed cache and refuses to start when Redis is unreachable. The recorder streams BTC and ETH perp market data into a feather catalog. The dashboard serves `/healthz`.
- One JSON log line per event across all processes, with time, level, process, and environment.
- A pytest suite of 127 tests run inside the containers, plus a runtime verify checklist in `docs/specs/0001-stack-architecture/verify.md`.
- A `compactor` service that turns the recorder's raw feather files into an hourly Parquet catalog (zstd). Every `order_book_deltas` hour starts with a full book snapshot, so a replay can begin at any hour. Reruns after a crash give the same files, byte for byte, and a book state is never applied twice. It runs at the lowest CPU priority on the box (see spec 0007).
- Gap detection: reconnects, recorder downtime, silence, and lost messages are each written to `gaps/<day>.parquet` and logged as `book_gap`. A daily manifest in `manifests/<day>.json` gives each closed day's size and lists its incomplete hours.
- Out of order rows are put back in order within a 600 s window. Anything later goes to a backed up `quarantine/`, never silently dropped. A `QueueDepthActor` warns when the recorder's data queue runs high or full.
- The recorder watches Kraken's message counter and records every hole in `live/<run>/sequence_holes.jsonl`. The compactor turns each hole into a `sequence` gap and stops trusting the book until the next feed `CLEAR`. On the VPS, a replay of the 24 hours after the deploy showed no crossed or one sided book (1,440 samples).
- Disk safety: local hours are deleted only after they are in a B2 `catalog` snapshot, oldest first, once the catalog is over 30 GB or free space is under 8 GB. The recorder turns unhealthy and stops before free space drops under 2 GB.
- `scripts/fetch-catalog.sh <from> <to>` restores catalog days from B2 to the laptop for research.
- A test that fails when a pure core folder imports NautilusTrader, `pyarrow`, or `numpy` (only `node/` and `catalog/` may; see spec 0008).
- Risk sizing for every entry: a stopped out trade loses at most `RISK_PER_TRADE` of equity (0.5 % by default). The loss per contract counts the stop distance, slippage past the stop, the taker fee on both legs, and the funding the trade would pay if it is held across an hourly funding time. A funding rate the trade would earn counts as 0. Each decision records the figures the guard used (see spec 0011).
- Hard caps on top of the risk size: order notional, a new total exposure cap across every instrument (`RISK_MAX_TOTAL_EXPOSURE_USD`, 500 USD), and leverage. The smallest size wins, and the decision says which one bound. A caller with a fixed size, such as the lab's random taker, is refused when it breaks a cap.
- A liquidation guard: before an entry, the trader estimates where the exchange would liquidate the account (cross margin, maintenance rate the larger of the instrument's and `RISK_MAINTENANCE_MARGIN_RATE`). It refuses the entry when that price is closer than 10 % to the entry (`liq_too_close`) or under 3 stop distances away (`liq_inside_stop`). Every position snapshot stores the estimate, and a mark closer than 10 % raises one `critical` alarm per position.
- An entry is refused with `funding_stale` when a funding time falls inside the planned hold and the funding rate is missing or stale. An open position, or equity, that the guard cannot price skips the entry instead of guessing.
- Demo funding: at every top of the UTC hour, the trader books a funding payment for each open position into `funding_payments` and the position's `funding_total`. Booking twice keeps one row. When no usable rate or quote exists, nothing is booked and a `funding_unbooked` warning is written. Live funding from Kraken's account log is still to come (feature 19).
- The loss limits count booked funding by the day it was paid, so a payment can trip the daily limit while the position is flat. The demo equity the guard sizes from includes the run's booked funding.
- Six new settings, each checked at start: `RISK_PER_TRADE`, `RISK_MAX_TOTAL_EXPOSURE_USD`, `RISK_MAINTENANCE_MARGIN_RATE`, `RISK_MIN_LIQ_DISTANCE_PCT`, `RISK_LIQ_STOP_MULTIPLE`, and `RISK_STOP_SLIPPAGE_BPS`. The trader refuses to start when one is out of range.

### Changed
- The recorder records the BTC perp (`PF_XBTUSD`) only. With BTC and ETH at 3.2 GB/h, the 2 CPU VPS could not drain its data queue. Backtests use free Tardis days instead of our own long history. The old raw backlog was dropped, not converted, after a check that B2 holds it.
- The nightly backup takes the catalog as its own snapshot, tagged `catalog`, separate from the Postgres dump. The restore drill keeps both kinds and restores only the main snapshots.
- The placeholder strategy is sized by the guard from its stop price, instead of a fixed notional. With the default settings the 250 USD order cap still binds, so its trades stay about the same size.
- P&L includes funding everywhere it is shown or counted: net P&L is `realized_pnl + funding_total`. The dashboard drops "before funding", shows a funding column on Trades, and shows the funding paid so far on the open position card. `realized_pnl` itself stays NautilusTrader's figure, before funding, the same in paper, live, and replay.
- The guard sizes with the taker fee from the strategy config (`SANDBOX_TAKER_FEE` in demo, the run's fee in the lab), never the instrument's fee, which Kraken publishes as 0. The trader refuses to build its strategy config outside demo until feature 19 decides the live fee source.

### Removed
- `PLACEHOLDER_NOTIONAL_USD`. The order notional cap covers it.

### Fixed
- The restore drill's retention could forget Postgres dump snapshots, because same day catalog snapshots took their daily slots. It now groups snapshots by host and paths.
- A catalog day never closed after ETH stopped being recorded. A day now needs only the instruments currently recorded.
- A damaged feather file in the middle of a run stops the compactor with an error, instead of being read as a cut off tail with the rest of the file dropped.
- Catalog writes are flushed to disk before and after each rename, so a power loss cannot leave a committed hour empty.
- A garbled line in a holes file now becomes a `sequence` gap and logs an error, instead of being skipped. Short writes to that file are retried until the whole line is written.
- Entries on the VPS were sized with a taker fee of 0, because the guard read the fee from Kraken's instrument while the sandbox charged 0.0005 on every fill. The risk size came out about 28 % too large.
- The trader logged `recording_failed` at every top of the hour and blocked entries until a restart, because the funding alert was set again from inside its own callback. It is now cancelled first, so the next hour's alert is always armed.

### Security
- Each Compose service gets only the variables it reads, instead of the whole `.env`. Kraken keys put in `.env` reach no service, so the dashboard and recorder can never receive them. Docker builds also leave out key and certificate files (`*.key`, `*.pem`, `secrets`).
