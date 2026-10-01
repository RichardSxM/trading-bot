# Verify: Stack & architecture · spec 0001 · updated 2026-10-01
_Steps derived from spec 0001's runtime contracts and its "What boots means for the scaffold" definition (the spec has no numbered acceptance criteria, so each step names the contract it checks). `/check verify` runs these; `/test` locks the durable ones._

All commands run from the repo root on the laptop, with `.env` copied from `.env.example` (`IMAGE_TARGET=dev`, `TRADING_ENV=demo`).

## Commands
- [x] `docker info --format '{{.MemTotal}}'` → about 4 GB (shows as roughly 3.8 GiB) → Follow-up: laptop Docker setup check
- [x] `docker compose build` → trader, recorder, dashboard, and migrate images all build → Packaging
- [x] `docker compose up -d`, then `docker compose ps -a` → postgres, redis, trader, recorder, dashboard, and backup `healthy`, migrate `Exited (0)` → What boots means; Compose services
- [x] `curl -s 127.0.0.1:8000/healthz` → `{"status":"ok"}`; `docker compose port dashboard 8000` → `127.0.0.1:8000` → What boots means; Network exposure
- [x] `docker compose logs trader recorder` → JSON lines for `starting` and `clock check passed`; `DataClient-KRAKEN` on `wss://futures.kraken.com/ws/v1` (LIVE, not demo); `No exec_clients configuration found` → Environment switch
- [x] `docker compose exec recorder sh -c 'ls -l /data/catalog/live/*/order_book_deltas/* /data/catalog/live/*/trade_tick/*'` → feather files for `PF_XBTUSD.KRAKEN` and `PF_ETHUSD.KRAKEN`, growing between two runs a few seconds apart → What boots means (recorder)
- [x] `docker compose exec redis redis-cli --scan --pattern 'trader-*' | head` → keys start with `trader-TRADER-DEMO:` → Redis
- [x] `docker compose run --rm --no-deps -e KRAKEN_FUTURES_API_KEY=x trader python -m trader.main` → one JSON line with `code: env_guard_refused`, no clock check lines, exit 1. Repeat for `recorder`, and for `recorder` with `-e TRADING_ENV=live` → Demo can never trade for real
- [x] `for s in trader recorder dashboard; do docker compose exec $s env | grep -c '^KRAKEN'; done` → `0` for each → Keys in the environment
- [x] `KRAKEN_FUTURES_API_KEY=x docker compose config | grep -c 'KRAKEN_'` → `0` (a key in `.env` or the shell reaches no service; only the trader in live will list keys, feature 4) → Keys in the environment
- [x] `docker compose run --rm --no-deps <app> pytest -q` for trader, recorder, and dashboard, plus `docker compose run --rm --no-deps -w /app/packages/core trader pytest -q` for core → all pass → What boots means (tests in containers)
- [x] `docker compose down` (keeps the volumes)

## Contract coverage
- Environment switch (data always LIVE, no exec client in the scaffold): logs step, trader and recorder smoke tests
- Env guard (allowlist, key variables, runs before the clock check, JSON `env_guard_refused`, exit 1): guard refusal step, `packages/core/tests/test_env_guard.py`, one guard test per node
- Redis key format: Redis step, trader smoke test
- Clock check and JSON logs: logs step
- Compose health, memory, and network exposure: ps and healthz steps
