# Kraken Futures Scalper

## Stack

- **Language / Runtime**: Python 3.14 (CPython), uv workspace: `packages/core` + `apps/trader`, `apps/recorder`, `apps/dashboard`
- **Framework**: NautilusTrader 1.231.0 (pinned exactly) with its Kraken Futures adapter; FastAPI + HTMX + SSE for the dashboard
- **Key dependencies**: PostgreSQL via SQLAlchemy 2 + Alembic; Redis (Nautilus cache, `snapshot` keys, `control` stream); Parquet catalog + DuckDB for research; pydantic-settings
- **Package manager**: uv (host only locks). Runs in Docker Compose, same file on laptop and the London VPS, reached only over Tailscale. NautilusTrader has no Intel Mac wheel, so anything that imports it, tests included, runs in containers.

## Build approach

**Tracer Bullet**: one thin real thread from market data to order to dashboard first, then thicken each segment end to end.

## Commands

```bash
uv lock                                   # host: lock deps (resolves Linux wheels)
scripts/dev-secrets.sh                    # laptop, first run: random dev secret files in ./secrets (never committed)
docker compose build                      # rebuild after any code change (tests are baked in)
docker compose up -d                      # run the stack (TRADING_ENV=demo on the laptop)
docker compose run --rm migrate           # alembic upgrade head
docker compose run --rm --no-deps -T <app> pytest -q                       # app tests
docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q  # core tests
pre-commit run --all-files                # gitleaks + ruff check + ruff format (the commit hooks)
pre-commit run --all-files --hook-stage pre-push   # mypy, the push hook (Docker must be running)
scripts/typecheck.sh                      # mypy strict; on the Mac it runs in the `tools` Compose service
```

## Specs

Stored in `docs/specs/`. Format: `docs/specs/NNNN-title/index.md` (plus `rationale.md`, `verify.md`).

## Rules

- Clean Architecture: pure domain and risk logic lives in `packages/core` with no I/O, framework, or ORM imports. NautilusTrader, Postgres, Redis, and FastAPI are adapters at the edges; Nautilus Strategy and Actor classes stay thin and call pure core logic. Dependencies point inward only. Inside each package, group code by feature (`risk/`, `execution/`, `recording/`). In core, `marketdata/`, `lab/`, `strategy/`, and `risk/` are the pure folders; `node/` and `catalog/` carry the Nautilus extra (see packages/core/AGENTS.md).
- Strict types: mypy strict on all our code, no untyped defs, no bare `Any`. Tests are type checked too but need no annotations. A Nautilus stub gap gets a narrow `# type: ignore[code]` with a reason.
- Errors: typed exceptions from core, never swallow one. Anything touching orders fails closed (refuse the action, log, alert).
- Every public function and class in `packages/core` has a docstring.
- Config only through `trading_core.settings`, validated at startup; a process refuses to start on bad config.
- Secrets only as files under `/run/secrets`, loaded through `trading_core.secrets` (fails closed, redacted from logs); never in env vars, logs, or the repo. Kraken keys exist only on the VPS (spec 0003).
- No floats for money: prices, sizes, fees, and P&L use Nautilus `Price`/`Quantity`/`Money` or `Decimal`.
- UTC everywhere: timezone aware UTC datetimes or Nautilus nanosecond ints, never naive datetimes.
- Demo is the default: nothing places a live order unless `TRADING_ENV=live` is set explicitly; tests never touch live.
- The dashboard never imports NautilusTrader; it reads Postgres and Redis and writes only to the `control` stream.
- Conventional commits: `feat(scope): ...`, `fix(scope): ...`, `docs(...)`.

## Tooling

Ruff (lint + format; its version is pinned in both `pyproject.toml` and `.pre-commit-config.yaml`, bump them together), mypy strict (needs NautilusTrader, so Linux only: the `tools` container locally, directly in CI), pre-commit running gitleaks, ruff check, and ruff format on every commit and mypy before every push (it is slow in the Mac's `tools` container), GitHub Actions on push and PR (gitleaks, lint, typecheck, pytest, and the trader's canary leak test on Linux). Testing gate: pytest unit tests for core logic (no infrastructure mocks) plus integration tests against real Postgres and Redis in Compose.

## Git

- integration: on
- branch prefix: feat/
- commit: per-milestone

## Agent skills

- [nautilus-trader](.claude/skills/nautilus-trader/): `algoderiv/agent-skills`, NautilusTrader nodes, strategies, actors, adapters
- [sqlalchemy](.claude/skills/sqlalchemy/): `terminalskills/skills`, ORM models and async queries
- [alembic](.claude/skills/alembic/): `terminalskills/skills`, schema migrations
- [postgres-writing-guidelines](.claude/skills/postgres-writing-guidelines/): `damusix/skills`, schema design and SQL
- [redis](.claude/skills/redis/): `terminalskills/skills`, cache, snapshot keys, streams
- [uv](.claude/skills/uv/): `terminalskills/skills`, workspace, lockfile, dependencies
- [docker-compose-skill](.claude/skills/docker-compose-skill/): `scientiacapital/skills`, the Compose stack
- [tailscale](.claude/skills/tailscale/): `terminalskills/skills`, private access to the VPS and dashboard

## Context files

- [packages/core/AGENTS.md](packages/core/AGENTS.md): shared settings, logging, clock check, naming, env guard, DB and migrations
- [apps/trader/AGENTS.md](apps/trader/AGENTS.md): the Nautilus node that holds positions
- [apps/recorder/AGENTS.md](apps/recorder/AGENTS.md): the Nautilus node that records market data
- [apps/dashboard/AGENTS.md](apps/dashboard/AGENTS.md): the private FastAPI + HTMX dashboard

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
