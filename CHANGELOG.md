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

### Security
- Each Compose service gets only the variables it reads, instead of the whole `.env`. Kraken keys put in `.env` reach no service, so the dashboard and recorder can never receive them. Docker builds also leave out key and certificate files (`*.key`, `*.pem`, `secrets`).
