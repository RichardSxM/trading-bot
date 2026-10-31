# dashboard (apps/dashboard)

## Overview

The private web dashboard, reachable only over Tailscale. History comes from Postgres, live position and P&L from Redis `snapshot` keys pushed to the browser over SSE. Pages: `/login`, Overview `/`, `/trades`, `/strategies`, `/system`, and `/styleguide` (spec 0004).

## Stack

- Python 3.14, FastAPI + uvicorn, HTMX + Server Sent Events, TradingView Lightweight Charts
- `trading-core` without the `node` extra
- Entry point: `dashboard.main:main`; dev tests use httpx

## Commands

```bash
docker compose build dashboard
docker compose run --rm --no-deps -T dashboard pytest -q
docker compose run --rm -T dashboard pytest -q -m redis   # Redis ACL tests against the Compose Redis
scripts/dashboard-credentials.sh --dev --write            # laptop: login files (password hash, authenticator seed, session key); never overwrites
```

## Conventions

- Never import NautilusTrader, directly or through `trading_core.node`.
- Read only, except the `control` Redis stream, its single write path to the trader. Redis enforces this: the dashboard logs in as the `dashboard` ACL user, which may write only to `*:control`.
- Never receives Kraken keys. Its port binds to `127.0.0.1` only.
- API docs routes stay off (`docs_url=None`, `openapi_url=None`).
- Design system: build all UI to root `DESIGN.md`; its frontmatter tokens are mirrored exactly in `static/css/tokens.css` (`tests/test_design_tokens.py` checks parity and contrast), and every component state it lists needs a `data-sample="macro:state"` on `/styleguide`.
- CSP is `'self'` only: no inline `<script>`, no `style=` attributes, no `hx-on`, nothing from another origin. Styles go in `static/css/`, behaviour in `static/js/`; JS may set styles through CSSOM or draw on a canvas.
- Live values: one SSE connection per page, opened by `.workspace` in `shell.html`; every `sse-swap` target must sit inside it, and each event is a partial the poller (`live/poller.py`) renders and publishes only on change.
- Third party browser assets are vendored under `static/vendor/` (or `static/fonts/`) with their licence and a `VERSIONS` line holding url and sha256.
- Journal export (spec 0024, `web/journal.py`, `data/journal.py`): the HTMX Check endpoint always answers 200 (htmx skips 4xx and 5xx), so a refusal or a Postgres failure is a panel state; each request reads in one `REPEATABLE READ` read only transaction, and the CSV streams open their own session from the factory, never a request dependency.
- Render money and times only through the `num`, `pnl`, and `ts` Jinja filters (`ui/format.py`): Decimals only, true minus sign, UTC with an Amsterdam tooltip.

## Agent skills

- [fastapi](../../.claude/skills/fastapi/): `terminalskills/skills`, routes, dependencies, SSE
- [htmx](../../.claude/skills/htmx/): `damusix/skills`, hx attributes, swaps, SSE extension
- [impeccable](../../.claude/skills/impeccable/): `pbakaus/impeccable`, design direction, critique, and polish for the UI
- [lightweight-charts](../../.claude/skills/lightweight-charts/): `tradingview/lightweight-charts`, the chart API

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
