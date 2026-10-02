# 0004. Build the dashboard shell on a calm dark design system, behind a password and authenticator login

**Date**: 2026-10-02
**Status**: In Progress

## Summary

The dashboard gets one visual language, written down in a root `DESIGN.md`: dark, dense, monospace numbers, color used only to mean something (profit, loss, stale, live). Every page sits inside one shell. Its top bar always shows whether you are in DEMO or LIVE, whether the live link works, how old the trader's data is, the state of your last command, and a red stop button that really sends a stop to the trader with one tap. You get in with a password plus a 6 digit authenticator code, and the session lives in a signed cookie, because the dashboard is not allowed to write to Postgres or Redis. The Impeccable design skill guides how the pages are built and polished.

## Requirements

**User stories**:
- As the trader, I want to see at a glance whether I am in DEMO or LIVE, whether the data is fresh, and what happened to my last command, so I never act on a stale or wrong picture.
- As the trader, I want a stop button on every page that works with one tap on my phone, even if JavaScript fails, so stopping is never slower than panicking.
- As the trader, I want a login only I can pass, on top of Tailscale, so a lost unlocked device or a tailnet mistake cannot reach the stop button or my numbers.
- As the builder of later features, I want written tokens, components, and number rules, so every page looks and reads the same without new design decisions.

**Acceptance criteria**:
- **AC-1**: Any page except `/login`, `/healthz`, and `/static/*` without a valid session answers `303` to `/login?next=<path>`; an HTMX request answers `401` with `HX-Redirect: /login?next=<path>`; `/events` answers `204` (an EventSource stops retrying on 204). For a POST, `next` is the path of the `Referer` header, else `/`; after an unauthenticated stop tap the login page shows "Your stop was NOT sent. Log in and tap stop again." A correct password plus a current TOTP code sets the session cookie (`HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`) and redirects to `next` only when it matches `^/[A-Za-z0-9/_-]*$` and is not `/login`, else to `/`. A visit to `/login` with a valid session redirects to `/`. A failed login answers `200`, shows the form again, and keeps `next`.
- **AC-2**: A wrong password or code shows one generic message ("Password or code is wrong") and logs one WARNING `login_failed` with fields `reason` (`bad_password`, `bad_code`, `replayed_code`, `locked`) and `client` (the lockout key), never the submitted values. After 5 failures for one lockout key within a fixed 15 minute window (from that key's first failure), every attempt from that key answers `429` until the window ends, even with correct credentials; a `429` attempt does not extend it, and a success resets that key. Across all keys, 20 failures in 15 minutes lock every new login the same way. At most 1,000 keys are held (the oldest is evicted). Sessions that already exist keep working. A restart clears the counters.
- **AC-3**: The password and the code are both evaluated on every attempt (no early exit that reveals which one failed). A TOTP code is accepted for the current 30 s step and one step either side; a code whose matched step is equal to or older than the last accepted step is refused as `replayed_code`; a step becomes "last accepted" only when the password also matched.
- **AC-4**: A session expires 24 hours after its last request (sliding) and 7 days after login (hard cap), whichever comes first; an open SSE stream does not count as a request. When less than 1 hour remains, the top bar shows "session ends HH:MMZ". A tampered or expired cookie counts as no session. Replacing `dashboard_session_key` or `dashboard_password_hash` and restarting ends every session. `POST /logout` clears the cookie.
- **AC-5**: The dashboard refuses to start (exit 1, `secret_unavailable`, spec 0003 AC-2) when `dashboard_password_hash`, `dashboard_totp_seed`, or `dashboard_session_key` is missing or unusable. Its boot `guard` refuses (exit 1, code `dashboard_credentials_invalid`) when the hash is not an argon2id hash or the seed, after uppercasing and stripping `=`, is not base32 of at least 160 bits. Settings validation refuses (`invalid configuration, refusing to start`) `DASHBOARD_COOKIE_SECURE=false` while `TRADING_ENV=live`.
- **AC-6**: Every authenticated page renders the shell: a fixed top bar (env badge, link status, data age, last command state, stop button) and navigation to Overview `/`, Trades `/trades`, Strategies `/strategies`, System `/system`. At 900 px wide and above the nav is a side column; below, it is a bottom tab bar. At a 360 × 640 viewport the stop button is fully visible without scrolling, at least 44 × 44 px. Each nav page shows its empty state.
- **AC-7**: `TRADING_ENV=demo` shows an amber `DEMO` badge, `<title>` prefix `DEMO ·`, and the demo favicon; `TRADING_ENV=live` shows a solid red `LIVE` badge, a 2 px red top border on the bar, `<title>` prefix `LIVE ·`, and the live favicon. No page renders without one of the two.
- **AC-8**: In every environment the trader writes `{env}:snapshot:heartbeat` every 1 s as a snapshot envelope with a 30 s expiry, its `data` holding `process`, `git_commit`, `trading_state` (the Nautilus trader state name), and `data_connected` (whether the Kraken data client is connected). When Redis is unreachable the trader keeps running and logs at most one WARNING `snapshot_write_failed` per 60 s; if the writer task itself crashes, the trader logs one CRITICAL `snapshot_writer_crashed` and keeps trading.
- **AC-9**: The top bar shows the trader's data age, labelled `trader`, updated over SSE at least once per second. It is neutral when the heartbeat is 3 s old or less, amber above 3 s, red above 10 s, red `no heartbeat` when the key is missing, and red `bad snapshot` when the envelope does not decode; a negative age (clock skew) shows as `0s` and logs one WARNING `clock_skew` per 60 s. Both thresholds come from settings. Stopping the trader container turns it red within 11 s.
- **AC-10**: Link status shows `live`, `reconnecting`, or `lost`. The server sends an SSE `ping` event every 2 s; the browser shows `reconnecting` after 5 s without any event and `lost` after 15 s. While it is not `live`, the data age and command state are dimmed and labelled `link lost`, so a frozen value never looks fresh; the SSE connection reconnects by itself. A crashed poller task is restarted and logged at ERROR `poller_crashed`.
- **AC-11**: One tap on the stop button (`POST /control/stop` with a valid CSRF token) appends exactly one entry to `{env}:control` (`XADD` with `MAXLEN ~ 1000`; every tap appends, there is no debounce) with fields `id` (UUID4), `cmd` = `stop`, `issued_at` and `expires_at` (ISO 8601 UTC with `Z`, `expires_at` = `issued_at` + `CONTROL_COMMAND_TTL_S`, default 300), `schema_version` = `1`, and the command state shows `stop sent · awaiting trader` at once. With JavaScript disabled the same tap works as a plain form post and returns the full page. A missing or wrong CSRF token answers `403` and appends nothing, and the button area shows "Stop NOT sent: reload the page".
- **AC-12**: The command state shows the latest entry of `{env}:control` (so it survives a reload and shows on a second device) and its outcome from `{env}:control:results`: `received by trader` for `received`, `confirmed` for `executed`, `duplicate` for `duplicate`, `failed` for `failed`, `expired` for `expired`, `awaiting trader` with no result yet, `not handled, expired` when no result exists and `expires_at` has passed, and amber `unknown result` (logged WARNING `control_result_invalid`) for an outcome it does not know or an entry that does not parse. Only the newest 200 results are searched. A final state is hidden 15 minutes after its `outcome_at` (or `expires_at` for `not handled, expired`).
- **AC-13**: When Redis is unreachable or does not answer within 1 s on a stop tap, or the browser's request fails on the network, the stop area shows a red `Stop NOT sent` state with a retry button (the server answers `503`; HTMX is configured to swap `401`, `403`, and `503` responses on the stop target, and a network error renders the same state in the browser), the dashboard logs ERROR `control_send_failed` when it saw the request, and it never shows `sent`.
- **AC-14**: The `dashboard` Redis user can read `*:control` and `*:control:results` (XRANGE, XREVRANGE, XREAD) and still gets `NOPERM` for `SET`, `DEL`, `FLUSHDB`, and any write outside `*:control`.
- **AC-15**: Root `DESIGN.md` defines type, color, spacing, radius, elevation (rules, not shadows), motion, the base components with their states, and the number and time rules, with its tokens in YAML frontmatter and a `contrast_pairs` list; `apps/dashboard/src/dashboard/static/css/tokens.css` holds exactly those token values; the dashboard image copies `DESIGN.md` to `/app/DESIGN.md`, and a unit test parses both and fails on any mismatch, and fails when any listed pair is below WCAG AA (4.5:1 for text under 18.66 px bold or 24 px regular, 3:1 for larger text and UI marks). The list holds at least `text`, `muted`, `profit`, `loss`, `warn`, `info` on each of `bg`, `surface`, `raised`; `faint` on `bg` and `surface`; white on `stop` and `live`; `bg` on `demo`; and `rule` against `bg` at 3:1 only where a rule is the sole boundary of a control.
- **AC-16**: `/styleguide` (behind login, in every environment, sample data only) renders every token and every base component in every state listed in `DESIGN.md`, including profit, loss, zero, stale, error, empty, loading, both env badges, and a Lightweight Charts sample, with no CSP violation in the browser console.
- **AC-17**: The `num` filter renders a `Decimal` at its own precision with comma grouping and a dot decimal (`Decimal("1234.50")` → `1,234.50`, `Decimal("1E+3")` → `1,000`) and raises `TypeError` for anything that is not a `Decimal` (`float`, `int`, `bool`) and `ValueError` for NaN or infinity; `pnl` adds `+` or `−`, ▲ or ▼, and the profit or loss class, and renders zero (including `-0.00`, shown as `0.00`) as neutral with neither; `ts` takes a timezone aware datetime or nanosecond int, raises `ValueError` for a naive datetime, renders UTC truncated to seconds as `2026-10-02 14:03:07Z`, with `title="16:03:07 CEST, Amsterdam"`.
- **AC-18**: Every HTML response carries the security headers in *Security model*; no page has inline script, inline style, or `hx-on` attributes; no page loads anything from another origin.
- **AC-19**: `scripts/dashboard-credentials.sh` runs `docker compose run --rm -it dashboard-credentials`: a Compose service in the `tools` profile that uses the dashboard image, runs `python -m dashboard.credentials`, mounts no secrets (so it works before they exist), and binds the host folder `${SECRETS_DIR:-./secrets}` at `/out`. It asks for a password twice without echo (at least 12 characters), prints the argon2id hash, a new TOTP seed (uppercase base32, no padding), its `otpauth://` URI and a terminal QR code, and a new session key; with `--write` it also writes the three files into `/out`, mode 0444 and never overwriting (on the VPS you then set owner 10001 and mode 0400 as spec 0003 does). Nothing secret ever appears in process arguments. `--dev` uses a random password and prints it once. `scripts/dev-secrets.sh` creates `dashboard_session_key` itself and ends with the line "next: docker compose build dashboard && scripts/dashboard-credentials.sh --dev --write" while either other file is missing; Compose refuses to start any service until the files exist.

## Decision

**Chosen option**: Option 2: server rendered shell on a hand written token system, password plus TOTP login in a signed cookie, live values pushed by one Redis poller over SSE.

FastAPI renders Jinja2 pages and HTMX partials from a small macro component library styled only by `tokens.css` and `components.css`; a stateless signed cookie carries the session; one background task per dashboard process reads the snapshot and control streams every 500 ms and pushes changes over SSE; the stop button appends to the `control` stream through a plain form that HTMX enhances.

**Implementation skills**: `impeccable` (`pbakaus/impeccable`, `.agents/skills/impeccable/`) · `lightweight-charts` (`tradingview/lightweight-charts`, `.agents/skills/lightweight-charts/`) · `htmx` (`damusix/skills`, `.claude/skills/htmx/`) · `fastapi` (`terminalskills/skills`, `.claude/skills/fastapi/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no table or migration. Three shapes cross process boundaries, all defined as pure code in `packages/core` (no I/O, docstrings, typed errors):

| Shape | Where | Fields |
|---|---|---|
| Snapshot envelope | `trading_core/snapshot/envelope.py`: `snapshot_key(env, name)`, `encode(name, ts_ns, data)`, `decode(raw) -> Snapshot` | key `{env}:snapshot:<name>` via `naming.key_prefix(env, "snapshot")`; JSON `{"schema_version": 1, "ts_ns": int, "data": object}`; `decode` raises `SnapshotFormatError` on bad JSON, a missing field, or an unknown `schema_version` |
| Control command | `trading_core/control/messages.py`: `stop_command(now, ttl_s) -> ControlMessage`, `to_fields(msg)`, `from_fields(fields)` | stream `{env}:control` (spec 0001); fields `id`, `cmd` (`recording.enums` command values), `issued_at`, `expires_at`, `schema_version` = `"1"`, all strings |
| Control result | same module: `result_from_fields(fields) -> ControlResult` | stream `{env}:control:results`; fields `id`, `outcome` (`recording.enums` outcome values), `outcome_at` (ISO 8601 UTC), `schema_version` = `"1"`; written by the trader from feature 9, this spec fixes the format |

`{env}:snapshot:heartbeat` data: `{"process": "trader", "git_commit": <Settings.git_commit>, "trading_state": <node.trader state name>, "data_connected": <bool>}`. It proves the trader process and its data link are alive, not that a strategy trades. Feature 6 adds its position and P&L snapshots in the same envelope.

**State transitions** (command state shown in the top bar, derived on every poll, never stored):

`none` → `sending` (tap) → `awaiting trader` (XADD ok, set directly in the POST response and the poller cache) → `received by trader` → `confirmed` | `failed` | `duplicate` | `expired` (a result arrives) or `not handled, expired` (no result and now > `expires_at`) or `unknown result`. `sending` → `Stop NOT sent` (Redis error, timeout, CSRF refusal, or network failure); retry returns to `sending` with a new `id`. A final state is hidden 15 minutes after it was reached, and the bar shows `none`.

Session cookie `kfs_session` (itsdangerous `URLSafeSerializer` keyed by `dashboard_session_key`, salt `kfs-session-v1`): `{"iat": int, "seen": int, "csrf": str, "pw": str}`, where `pw` is the fingerprint of the password hash (so a new password ends old sessions) and `csrf` is 32 random bytes in URL safe base64. Every authenticated response except `/events` re-signs it with a new `seen`.

**App wiring**: `main()` runs `boot("dashboard", settings_cls=DashboardSettings, secrets=…, guard=credentials_guard)`, then `create_app(settings, secrets)` builds the FastAPI app (templates, static mount, auth dependency, Redis client, poller in the lifespan, all on `app.state`) and passes it to uvicorn. Tests call `create_app` with fake settings and secrets. The module level `app` goes away.

**Shell and design system**:
- Files: templates under `apps/dashboard/src/dashboard/templates/` (`base.html`, `shell.html`, `login.html`, `pages/*.html`, `partials/*.html`, `components/*.html` as Jinja macros); static under `static/` (`css/tokens.css`, `css/components.css`, `js/shell.js`, `fonts/` IBM Plex Sans 400/500/600 and Plex Mono 400/500 woff2 Latin subsets plus the OFL licence, `vendor/` htmx 2.x, the htmx SSE extension, Lightweight Charts, each pinned exactly in `vendor/VERSIONS`, `favicon-demo.svg`, `favicon-live.svg`).
- `shell.js` (no build, loaded with `defer`) only: sets `htmx.config` (`allowEval: false`, `includeIndicatorStyles: false`, and `responseHandling` so `401`, `403`, and `503` swap on the stop target); renders `Stop NOT sent` on a network error (`htmx:sendError`) for the stop target; maps SSE open, error, and close events plus a 5 s and 15 s silence watchdog to the link status; toggles the `link lost` dimming; and reloads the page when `/events` ends with `204` (session gone), which leads to the login.
- Root `PRODUCT.md` (written through `impeccable init`) and root `DESIGN.md` (Impeccable's DESIGN.md format) are the design source of truth; `DESIGN.md` stays within this brief (Impeccable's rule: the brief wins). Starting tokens, tuned in the design milestone but never dropped:

| Token group | Starting values |
|---|---|
| Surfaces | `bg` #0B0D10 · `surface` #12151A · `raised` #181C22 · `rule` #232831 |
| Text | `text` #E6E8EB · `muted` #9AA3AE · `faint` #838C98 (labels only) |
| Meaning | `profit` #2FBF71 · `loss` #F0524F · `warn` #E8A33D · `info` #6CA6FF · `live` #D62F2F (white text) · `demo` #E8A33D (dark text) · `stop` #C93338 (white text, 600 weight) |
| Type | Plex Sans for labels, Plex Mono with `font-variant-numeric: tabular-nums` for every number; sizes 12, 13, 14, 16, 20, 24, 32 px; body 14 |
| Space | 4 px base: 4, 8, 12, 16, 24, 32, 48 |
| Shape | radius 2 and 4 px; 1 px rules instead of cards and shadows |
| Motion | 120 ms ease out for state changes; none under `prefers-reduced-motion` |

- Base components (macros, each with the states `DESIGN.md` lists): `num`, `pnl`, `ts`, `badge` (env, status), `status_dot`, `stat` (label plus value), `table` (dense, sticky header, right aligned numbers), `button` (default, danger, stop, disabled, busy), `empty_state`, `loading`, `error_state`, `stale` wrapper, `chart` (Lightweight Charts container themed from tokens).
- Empty state copy: Overview "Live position and P&L appear here once the trader runs a strategy." · Trades "Fills and closed trades appear here." · Strategies "Backtests and strategy scores appear here." · System "Command history and service health appear here."

**API surface** (all HTML, cookie session; `csrf` = the `X-CSRF-Token` header from `hx-headers` or the `csrf` form field, compared in constant time with the cookie's token):

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/healthz` | GET | none | `{"status": "ok"}` | public | none |
| `/static/*` | GET | path | file, `Cache-Control: public, max-age=86400` | public | 404 |
| `/login` | GET | `next` (opt), `stop_lost` (opt flag) | login form; 303 `/` when a session exists | public | none |
| `/login` | POST | `password`, `code`, `next` (opt) form fields | 303 to `next`, cookie set | public | 200 with error and `next` kept, 429 locked |
| `/logout` | POST | csrf | 303 `/login`, cookie cleared | session | 403 csrf |
| `/`, `/trades`, `/strategies`, `/system` | GET | none | shell plus page | session | 303 / 401 (AC-1) |
| `/styleguide` | GET | none | every component in every state | session | 303 / 401 |
| `/events` | GET | `Accept: text/event-stream` | the current `topbar-age` and `topbar-command` partials at once, then each on change, plus `ping` every 2 s | session (checked at connect; the stream closes when the session would expire) | 204 without a session |
| `/control/stop` | POST | csrf | HTMX: command state partial; plain form: 303 back to the referring page | session | 401 (then 303 `/login?next=<Referer path>&stop_lost=1`), 403 csrf, 503 Redis down or timeout |

**Live poller** (`dashboard/live/poller.py`, one per process, supervised by the lifespan: a crash logs ERROR `poller_crashed` and restarts it after 1 s): while at least one SSE subscriber exists, every `DASHBOARD_POLL_MS` (500) it runs `GET {env}:snapshot:heartbeat`, `XREVRANGE {env}:control + - COUNT 1`, and, when that command has no final outcome yet, `XREVRANGE {env}:control:results + - COUNT 200` looking for its `id`; it computes the age and command state, keeps the two rendered partials as its current cache, and publishes each to every subscriber queue only when its HTML changed (the age text changes once per second). A new subscriber gets the cached partials at once. With no subscriber it idles, and a full page render reads Redis directly through the same functions. A Redis error marks both partials `link lost` and retries on the next tick. Subscriber queues are bounded (size 8, oldest dropped). The pure state logic (`age_state(now_ns, ts_ns, amber_s, red_s)`, `command_state(command, result, now, hide_after_s)`) lives in `dashboard/live/state.py`. The dashboard's redis-py asyncio client uses a 1 s connect timeout and a 1 s socket timeout.

**Trader heartbeat snapshot** (`apps/trader`): `run_node` starts the writer task with `loop.create_task(...)` in every environment (not inside the live only key check block), keeps it in a local for the life of the node, cancels it when the node stops, and adds a done callback that logs CRITICAL `snapshot_writer_crashed` if it raised. The task writes the heartbeat with `SET ... EX 30` through a redis-py asyncio client logged in as `trader`, every 1 s, with a 200 ms socket timeout, reading `trading_state` and `data_connected` from the node; it catches Redis errors and logs `snapshot_write_failed` at most once per 60 s.

**Redis ACL change** (extends spec 0003): the `dashboard` user line becomes `dashboard on #<hash> resetkeys %R~*:snapshot:* %RW~*:control %R~*:control:results -@all +@read +xadd +ping +hello +auth +client|setname +client|setinfo`.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| env badge, title, favicon | `demo` or `live` | `Settings.trading_env` of the dashboard process |
| data age | seconds since heartbeat | dashboard `time.time_ns()` minus envelope `ts_ns`; both containers read the same host clock (on the laptop, Docker Desktop's single Linux VM), and boot's clock check covers drift; a negative result shows `0s` |
| `bad snapshot` | decode failure | `SnapshotFormatError` from `envelope.decode` |
| session end cue | time left | cookie `seen` + idle limit and `iat` + cap, whichever is sooner |
| data age color | amber and red thresholds | `DASHBOARD_STALE_AMBER_S` (3), `DASHBOARD_STALE_RED_S` (10) in `DashboardSettings` |
| `no heartbeat` | key absence | `GET` returns nil (the 30 s expiry removes a dead trader's key) |
| link status | live, reconnecting, lost | browser SSE extension events in `shell.js` |
| stop command `id` | UUID4 | `uuid.uuid4()` at tap time (the stream message id, spec 0002 `control_commands.id`) |
| `issued_at`, `expires_at` | UTC timestamps | dashboard `datetime.now(UTC)`; plus `CONTROL_COMMAND_TTL_S` (300) |
| last command | latest command | `XREVRANGE {env}:control + - COUNT 1` |
| command outcome | `executed`, `failed`, `duplicate`, `expired` | `{env}:control:results` entry with the same `id` (trader, feature 9) |
| `not handled, expired` | derived | no result and dashboard now > `expires_at` |
| login check | password hash, TOTP seed | secret files `dashboard_password_hash`, `dashboard_totp_seed` |
| session validity | `iat`, `seen`, `pw` | cookie fields; limits `DASHBOARD_SESSION_IDLE_H` (24), `DASHBOARD_SESSION_MAX_D` (7) |
| lockout key | client identity | only when the socket peer is loopback (that is, `tailscale serve`): the `Tailscale-User-Login` header, else the first `X-Forwarded-For` address; otherwise the socket peer address |
| login `next` after a lost stop | page to return to | the `Referer` header's path, validated like any `next` |
| TOTP matched step | step number | computed from the server clock: `floor(now / 30) + offset` for the offset in −1, 0, +1 whose code matches (constant time compare), not pyotp's boolean `verify` |
| last accepted TOTP step | replay guard | process memory, starts empty (a restart allows one reuse of the current step, accepted) |
| number precision | digits shown | the `Decimal` value's own exponent (callers quantize to the instrument's precision; this spec never rounds) |
| Amsterdam time in `title` | local time | `zoneinfo.ZoneInfo("Europe/Amsterdam")`, with the `tzdata` package added to the dashboard so slim images have the zone data |

**Key invariants**:
- The dashboard writes nothing except `XADD {env}:control`; Redis enforces it (AC-14), Postgres enforces read only (spec 0002 AC-11).
- No page shows a value from Redis without its age or the `link lost` marker; a frozen value never looks live.
- The stop path never depends on JavaScript, SSE, Postgres, or a CDN; only on the session, CSRF, and Redis.
- Money and size values reach templates as `Decimal`; the filters refuse `float`.
- Every timestamp rendered is UTC; local time appears only in `title`.
- The lockout never affects a session that already exists.

**Security model**:
- One user. Factors: Tailscale membership, then argon2id password (argon2-cffi defaults: 64 MiB, t=3, p=4, run in a thread executor so it never blocks SSE or a stop), then TOTP (pyotp for code generation, SHA1, 6 digits, 30 s, the authenticator default; the seed is uppercased with `=` stripped before use). The replay guard's check and set run under one asyncio lock. No username field.
- Session: stateless signed cookie as above. Rotating `dashboard_session_key` is the "log out everywhere" switch. There is no server side revocation of a single session; that is accepted for one user behind Tailscale.
- CSRF: `SameSite=Strict` plus a per session token on every POST (synchronizer token in the cookie, sent back by header or form field).
- Headers on every HTML response: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. No HSTS header: `tailscale serve` owns TLS for the `.ts.net` name, and the laptop serves plain HTTP on loopback.
- Trusting the `Tailscale-User-Login` and `X-Forwarded-For` headers is safe only because the port binds to `127.0.0.1` and only `tailscale serve` reaches it (spec 0001); they key the lockout, never grant access.
- Logs: `login_ok`, `login_failed`, `logout`, `control_sent` (fields `id`, `cmd`), `control_send_failed`. Never a password, code, cookie, or token.

**Configuration required** (`DashboardSettings(Settings)` in `apps/dashboard`, validated at boot):
- Secret files (dashboard only, extend the spec 0003 inventory): `dashboard_password_hash`, `dashboard_totp_seed`, `dashboard_session_key`.
- `DASHBOARD_COOKIE_SECURE` (default `true`; `false` allowed only in demo, for a laptop browser that refuses Secure cookies on `http://127.0.0.1`).
- `DASHBOARD_STALE_AMBER_S` (3), `DASHBOARD_STALE_RED_S` (10, must exceed amber), `DASHBOARD_POLL_MS` (500), `DASHBOARD_SESSION_IDLE_H` (24), `DASHBOARD_SESSION_MAX_D` (7), `CONTROL_COMMAND_TTL_S` (300), `DASHBOARD_COMMAND_HIDE_S` (900).
- Compose: a `dashboard-credentials` service (profile `tools`, dashboard image, no secrets, `${SECRETS_DIR:-./secrets}:/out`); the dashboard Dockerfile copies root `DESIGN.md` to `/app/DESIGN.md`.
- New dependencies: dashboard `jinja2`, `python-multipart`, `argon2-cffi`, `pyotp`, `itsdangerous`, `redis` (redis-py, asyncio), `tzdata`, and `qrcode` for the credentials command; trader `redis`.

**Critical test scenarios**:
- Happy path: log in with password and a current code, see the shell with DEMO and a neutral data age, tap stop, see `XRANGE demo:control` gain one entry and the state read `awaiting trader`; append a matching `executed` result by hand and see `confirmed`, verifies **AC-1**, **AC-6**, **AC-7**, **AC-9**, **AC-11**, **AC-12**.
- Failure case: stop the Redis container, tap stop: `503`, `Stop NOT sent`, ERROR log, no `sent` state; stop the trader: data age red within 11 s, then `no heartbeat` after 30 s, verifies **AC-8**, **AC-9**, **AC-13**.
- Failure case: drop the SSE connection (stop the dashboard briefly): link `lost`, values dimmed, recovery on restart, verifies **AC-10**.
- Auth/permission: no cookie → 303; tampered cookie → 303; POST stop without CSRF → 403 and no entry; 6 bad logins → 429 even with the right code while an existing session still works; reused code → `replayed_code`; `redis-cli --user dashboard SET x 1` → `NOPERM`, verifies **AC-1** to **AC-4**, **AC-11**, **AC-14**.
- Design system: token parity and contrast test, `/styleguide` renders every state, `num(1.5)` raises `TypeError`, verifies **AC-15** to **AC-17**.

## Build plan

Tracer Bullet: the first slice is a thin real thread from the trader through Redis and SSE to a logged in browser; later slices thicken the command path, the design system, and the shell.

1. **Thin thread: login, bare shell, live data age.**
   1. `trading_core/snapshot/envelope.py` with unit tests; redis-py in the trader; the heartbeat task in `run_node` with a test against a fake client, satisfies **AC-8**.
   2. Three secret names in `trading_core.secrets`, `DashboardSettings` with its validators, `credentials_guard`, `boot("dashboard", settings_cls=DashboardSettings, secrets=…, guard=credentials_guard)` and `create_app(settings, secrets)`; Compose mounts the three files into `dashboard` only; the `dashboard-credentials` tools service, `python -m dashboard.credentials`, and `scripts/dashboard-credentials.sh`; `dev-secrets.sh` gains `dashboard_session_key` and its "next" line, satisfies **AC-5**, **AC-19**.
   3. `dashboard/auth/` (pure: password verify wrapper, TOTP check with replay guard, lockout counter, session encode and decode with sliding and cap rules) with unit tests on a fake clock; login, logout, the session dependency, CSRF, security headers, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-18**.
   4. Jinja2 setup, `base.html`, a plain `shell.html` with env badge and data age, minimal tokens; `/events` SSE and the poller for the heartbeat only, `dashboard/live/state.py` with unit tests, satisfies **AC-7**, **AC-9**.
2. **Stop path.** `trading_core/control/messages.py` with unit tests; the ACL line change and the extended `test_redis_acl.py`; `POST /control/stop` as a form enhanced by HTMX; the poller reads `control` and `control:results`; the command state partial with every state; the Redis down path, satisfies **AC-11**, **AC-12**, **AC-13**, **AC-14**.
3. **Design system.** Run `impeccable init` to write `PRODUCT.md` (operate mode, the users and constraints above), then write root `DESIGN.md` within this brief; full `tokens.css` and `components.css`; Plex fonts and licence; the component macros and `num`, `pnl`, `ts` filters with tests; `/styleguide`; the token parity and contrast test; one `impeccable critique` and one `impeccable polish` pass on `/styleguide` and the shell (desktop and phone together, one fix round), satisfies **AC-15**, **AC-16**, **AC-17**.
4. **Complete shell.** Side nav and bottom tab bar, the four pages with empty states, the LIVE treatment and both favicons, `shell.js` link status and dimming, vendored htmx, SSE extension, and Lightweight Charts with `vendor/VERSIONS`; httpx tests per page and the 360 × 640 stop button check in `verify.md`, satisfies **AC-6**, **AC-7**, **AC-10**, **AC-18**.

## Consequences

**Positive**:
- The stop path is real and tested from day one; feature 9 only adds the trader side.
- Later pages reuse macros, tokens, and filters, and the parity test stops the code and `DESIGN.md` from drifting apart.
- The login needs no database write and no Redis permission beyond reading two streams.

**Negative / tradeoffs**:
- A single session cannot be revoked on its own; only rotating the session key logs out everywhere.
- The lockout lives in memory: a restart resets it, and someone on the tailnet who triggers the global ceiling can block new logins for 15 minutes (your open sessions keep working, and the 24 hour idle window makes an expired session during an emergency unlikely).
- Losing the authenticator means rerunning the credentials command on the VPS; the seed's master copy in the password manager is the recovery path.
- Polling Redis every 500 ms adds up to 1 s of latency to live values and a few Redis calls per second while a tab is open.
- A 24 hour idle session is a longer window for a lost unlocked device than 12 hours; accepted so the stop button is rarely behind a login when it matters.
- Dark only: a light theme later means a second token set and a second contrast pass.
- The dashboard gains seven dependencies; argon2-cffi and qrcode need wheels for the Linux image (both ship them).

**Neutral**:
- `PRODUCT.md` and `DESIGN.md` sit at the repo root because Impeccable reads them there.
- The `control:results` format is fixed here, before its writer (feature 9) exists.
- Lightweight Charts' licence asks for TradingView attribution; keep its default attribution logo on.

## Follow-up

- [ ] Spec 0003's *Secret inventory* and Redis ACL rule are extended here; `/sync` should flag 0003 so readers find the three dashboard secrets and the new `dashboard` line.
- [ ] `apps/dashboard/AGENTS.md`: list `impeccable` (`.agents/skills/impeccable/`) and `lightweight-charts` (`.agents/skills/lightweight-charts/`) under Agent skills, record `Declined:` the Jinja2 and OTP MCP servers (no live system to reach; a TOTP seed must not reach a third party server), point to root `PRODUCT.md` and `DESIGN.md`, and record the conventions (no inline script or style, no `hx-on`, `Decimal` only into templates, every Redis value shown with its age). `/sync` owns this.
- [ ] Feature 6 writes its position and P&L snapshots in the envelope and extends `DESIGN.md` only through Impeccable; feature 9 writes `{env}:control:results` in this format and adds resume (with a confirm step) and flatten.
- [ ] Feature 12 can alert on `login_failed` bursts and `snapshot_write_failed`.
- [ ] `.agents/` and `skills-lock.json` from the Impeccable install should be committed with this spec.
