# 0004. Rationale: design system and dashboard shell

## Context

Feature 5 sets the look and the frame of the private dashboard (`apps/dashboard`) before any real page exists. Feature 6 will put the first live position and P&L on it, and feature 9 will make the stop button flatten and halt the bot. The scope asks for a small, clear visual language (numbers, tables, charts, status colors, an always visible stop button) and a shell behind a login only you can pass.

The forces that shaped the choice:
- **Who uses it, and when.** One trader, often on a phone, sometimes at night, sometimes under stress while a position moves. Misreading DEMO for LIVE, a frozen number for a fresh one, or a sent command for a confirmed one costs money.
- **What the dashboard is allowed to do.** Spec 0001 keeps it a reader: no NautilusTrader import, and its only write is the `control` stream. Spec 0002 makes its Postgres role SELECT only; spec 0003 lets Redis enforce read only snapshots plus appends to `*:control`. So nothing about a login can be stored server side without widening those boundaries.
- **Stack already chosen.** FastAPI, HTMX, SSE, Lightweight Charts, no JavaScript build step (spec 0001); secrets only as files through `trading_core.secrets` (spec 0003); Tailscale with `tailscale serve` as the outer wall.
- **No design exists yet.** There is no `DESIGN.md`, no screenshot, no design tool file. You chose to set the direction in this spec and use the Impeccable skill (installed in this run) to build and polish it.

If this stays undecided, feature 6 would invent the look, the login, and the live update path inside its own build, and every later page would copy or contradict it.

## Options considered

### Option 1: Thin shell, Tailscale identity as the login, CSS framework

Trust the `Tailscale-User-Login` header that `tailscale serve` adds, allow only your login, style with a classless framework (Pico) plus overrides, poll from the browser with `hx-trigger="every 1s"`.

**Pros**: least code; no secrets to manage; nothing to log in to.

**Cons**: anyone holding one of your unlocked devices reaches the stop button and every number; browser polling contradicts spec 0001's SSE choice and costs a request per second per tab; a framework fights a dense dark look.

### Option 2: Server rendered shell on own tokens, password plus TOTP in a signed cookie, Redis poller over SSE (chosen)

Hand written tokens and component macros, Jinja2 pages and partials, a stateless signed cookie after argon2id password plus TOTP, one server task per process that polls Redis and pushes changes over SSE, and a stop button that is a plain form HTMX enhances.

**Pros**: fits every existing boundary with one small ACL widening (read two streams); two factors on top of Tailscale with no stored session; stop path independent of JavaScript; one place where live state is read.

**Cons**: no single session revocation; in memory lockout resets on restart; seven new dependencies; 500 ms polling latency.

### Option 3: Passkey login with server side sessions in Redis, keyspace notifications for push

WebAuthn passkeys (Face ID or Touch ID), sessions as Redis keys with server side revocation, and Redis keyspace notifications instead of polling.

**Pros**: strongest and fastest login; revoke one session; push without polling.

**Cons**: needs a new Redis write rule (`*:session:*`) and pub/sub channel grants, loosening spec 0003's boundary; WebAuthn needs a library, client JavaScript, a stored credential the dashboard cannot write, and a recovery path; keyspace notifications need server config and are lossy by design.

## Rationale

Option 2 is the only one that keeps the boundaries specs 0001 to 0003 just made the database and Redis enforce, while still giving two factors beyond Tailscale. The stop button is the reason the login exists; a device left unlocked is a more likely threat for one trader than a tailnet breach, which rules out Option 1. Option 3 buys real strength, but every gain costs a loosened permission or a new moving part, and its single session revocation matters little for one user who can rotate the session key.

On the look, the calm terminal direction follows from the usage scene: dense tabular numbers, color only for meaning, so the red stop button and a red stale age are the loudest things on screen. Green and red for P&L follow every exchange you already read; the sign and arrow make it safe for red and green colorblindness. UTC display matches the logs, the database, and Kraken, with Amsterdam time a hover away.

The 500 ms Redis poller is the simplest push that fits the current ACL: one reader per process, change detection before sending, and no pub/sub grants. The latency is well below what a human acts on, and features that need tick speed (the scalper itself) never go through the dashboard.

Decisions settled while writing (pick, why, runner up):
- **CSRF**: `SameSite=Strict` plus a per session synchronizer token; Strict alone is defeated by same site subdomain tricks on a shared ts.net name. Runner up: double submit cookie.
- **TOTP window**: ±1 step with an in memory replay guard; covers phone clock drift and stops a shoulder surfed code from being reused. Runner up: ±0 steps (more failed logins on drift).
- **Lockout key**: the `Tailscale-User-Login` header, then `X-Forwarded-For`, both trusted only from a loopback peer, plus a global ceiling of 20 failures and a 1,000 key cap; per key alone could be bypassed by rotating a header, global alone would let anyone on the tailnet lock you out with 5 tries. Runner up: global counter only.
- **Session idle window**: you picked 12 hours sliding; the cross check showed that an expired session plus a lockout can put the stop button behind a login in an emergency, so the idle window became 24 hours with a "session ends" cue in the last hour, and a lost stop tells you plainly it was not sent. The 7 day cap stays.

Cross check (another model) found 21 gaps in the first draft, all applied: the boot order for credential checks, seed normalization, app wiring, the heartbeat outside the live block, Redis timeouts, HTMX error swaps, the lost stop after an expired session, `next` validation, the `/events` auth answer, lockout details, factor order and replay locking, command display lifetime, stream trimming, first paint state, link status thresholds, data age edge cases, filter inputs, contrast pairs (the stop red and `faint` starting colors failed AA and were changed), `DESIGN.md` in the image, the credentials service, and smaller login behaviors.
- **Security headers**: a strict `self` only CSP with no inline code; possible because htmx runs with `allowEval: false` and `shell.js` holds the few behaviors. Runner up: CSP with nonces (needs per request nonce plumbing).
- **Redis down on stop**: fail closed and say `Stop NOT sent` with retry; a silent failure on the stop button is the worst outcome here. Runner up: queue in the browser and resend (hides the failure).
- **SSE reconnect**: the htmx SSE extension's built in reconnect with backoff, plus `link lost` dimming; no custom reconnect code. Runner up: a hand written EventSource wrapper.
- **Number filters**: keep the `Decimal`'s own precision and refuse `float`, so the template never rounds money (AGENTS.md: no floats for money). Runner up: format to a fixed number of decimals (wrong for BTC versus ETH sizes).
- **Where auth logic lives**: `apps/dashboard/src/dashboard/auth/` as pure functions, not `packages/core`; only the dashboard logs in, and core stays shared domain logic. Runner up: `trading_core/auth/`.
- **Heartbeat writer**: a task on the node's event loop like the existing key check, not an actor timer; an actor callback must not block on Redis I/O, and the task pattern already exists in `run_node`. Runner up: an actor with a sync client and short timeout.
- **Design file location**: root `PRODUCT.md` and `DESIGN.md` (Impeccable's format and location, so every Impeccable command loads them). You first picked `apps/dashboard/design.md`, then moved it once Impeccable's convention came up.

Settled during the build (2026-10-02, update in place):
- **Activity feed source**: the redesign put an Activity region on Overview with no named source. You picked commands now, growing later: the newest 20 `{env}:control` entries with their outcomes, because they already exist in Redis, the poller already reads both streams, and they answer "what just happened?" honestly today. Fills (feature 6) and alerts (feature 12) join the same feed when they exist. Runners up: remove the region until feature 6 (less to see), or keep an empty placeholder (empty for months).
- **TradingView attribution**: the original line said to keep the library's default logo, but that logo injects an inline style the strict CSP blocks. You picked a visible "Charting by TradingView" text link under every chart, which meets the licence's NOTICE request for attribution plus a link. Runner up: loosen `style-src` for the logo (weakens AC-18 on every page for one logo).
- **Critique round, 2026-10-03** (`impeccable critique` in `/check verify` step 14 found two P1 issues, 24/40). A failed or unhandled stop was 13 px red text in the strip, the same weight as everything else, while PRODUCT.md promises "calm until something is wrong; then unmissable": added the alert bar (AC-21), one line at a time, absolute times so a screen reader is not flooded. `link lost` named two different failures (the browser's stream and the dashboard's Redis read), so a green `link live` could sit beside dead values: Redis failures now read `no data` and turn the link amber. The command state gained its age (Principle 1), Activity collapses runs of identical rows, number column heads lost Mono, the Trades board no longer stretches into a void, and Activity's empty state says what it shows today. Runners up: a full page takeover on failure (too loud for an amber state), and a `Stop pending` button state (it would read as a debounce, and AC-11 says every tap appends). The second critique round (28/40) found one more P1: a stop a live trader ignores stayed calm indigo for the whole 300 s, so the amber "Stop not acknowledged" alert now fires after `DASHBOARD_ACK_WARN_S` (10 s), and every stop alert names the way out (close positions on Kraken directly). Left for later, recorded here: a server side heartbeat history so the tape is full on first paint, and renaming `link live` (the env badge also says LIVE).
