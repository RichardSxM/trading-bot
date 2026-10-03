# Review, feat/design-system-dashboard-shell, 2026-10-03

**Reviewed by**: Sonnet 5.5 (author model not stated)
**Scope**: 187 files (about 172 non-lock, non-vendored), branch vs main (13 commits)
**Verdict**: Changes requested

## Summary
The branch builds the dashboard shell, a password plus TOTP login in a signed cookie, the stop path to the `control` stream, the live poller over SSE, the design tokens and components, and the trader heartbeat writer. Architecture follows AGENTS.md closely: pure logic in core and `auth/`/`live/state.py`, no NautilusTrader import in the dashboard, CSP with no inline script or style, secrets via files only, and a fail closed stop path. Vendored asset sha256 values in `VERSIONS` match the files on disk. Two issues deserve a fix before merge: the no-JavaScript stop retry ends on a 405 page, and unbounded concurrent argon2 verifies can exceed the dashboard's 256 MiB limit.

## Major
### 🟠 No-JS stop retry redirects to `GET /control/stop` (405), `apps/dashboard/src/dashboard/web/control_routes.py:81`
**Problem**: A failed plain-form stop (503 or CSRF 403) renders the page at URL `/control/stop`. The "Retry stop" tap then posts with `Referer: …/control/stop`. `referer_path` returns `/control/stop` because `safe_next` only checks the character set, and the success branch does `RedirectResponse(referer_path(...), 303)`. The browser then does `GET /control/stop`, which answers 405. The same path is used by `login_redirect_target` (`web/session.py`): a lost stop retried from that page sends `next=/control/stop`, so after login the user lands on a 405. `render_referring_page` guards this (`if path not in PAGES: path = "/"`), but the success redirect and login `next` do not.
**Why it matters**: This is the no-JavaScript path the spec calls out as the panic case (user story 2, AC-11). The stop is sent, but the user sees "Method Not Allowed" and may tap again or doubt it worked. No test covers a retry from the error page (`test_control.py` always sends `Referer: /`).
**Suggested fix**: Resolve the referer to a known page (the `PAGES` keys, else `/`) in one shared helper used by the success redirect, `login_redirect_target`, and `render_referring_page`. Add tests for a retry with `Referer` set to `/control/stop`, and for a lost stop from that page.

### 🟠 Unbounded concurrent argon2 verifies can OOM the 256 MiB dashboard, `apps/dashboard/src/dashboard/web/auth_routes.py:96`
**Problem**: `asyncio.to_thread(context.passwords.verify, …)` runs argon2id at its defaults (64 MiB each). The lockout is checked before the verify but failures are recorded only after it, so any burst of concurrent POSTs `/login` all pass `is_locked` and run in parallel. The default executor allows `min(32, cpus + 4)` threads. Compose sets `mem_limit: 256m` for the dashboard (`compose.yaml`), so about 4 parallel verifies plus the app exceeds it.
**Why it matters**: A tailnet peer (or a buggy client) can get the container OOM killed, taking down the page that holds the stop button. This is an availability hole in the one component that must stay up.
**Suggested fix**: Serialize or cap verifies with a small semaphore (1 or 2), and/or count an attempt against the lockout key before starting the verify and refund it on success. Add a test with concurrent attempts.

## Minor
### 🟡 SSE subscribes after the snapshot is taken, `apps/dashboard/src/dashboard/web/page_routes.py:187`
**Problem**: `events` reads `poller.cached` (or `render_now`) in the handler, but `poller.subscribe()` runs only when the generator first iterates (`event_stream`, line 161). A partial published in between is never delivered, and the poller publishes only on change, so `overview-activity` (which changes only on a new command or result) can stay stale until the next change. A cache left over from an idle period also shows an old age for the short time before the woken poller ticks.
**Why it matters**: A small window, but it is a frozen value that looks live, which the spec forbids.
**Suggested fix**: Subscribe first, then take the snapshot; or have the poller force a tick and republish all partials to a new subscriber.

### 🟡 Lockout key is spoofable through `X-Forwarded-For`, `apps/dashboard/src/dashboard/web/auth_routes.py:43`
**Problem**: When the peer is loopback or the gateway and no `Tailscale-User-Login` header exists (the laptop browser, or a non-serve path), the first `X-Forwarded-For` value is client supplied, so rotating it gives a fresh per key window each time. Only the global 20 per 15 minute cap then limits guessing.
**Why it matters**: Matches the spec's accepted model (the header only keys the lockout), but the per key limit is bypassable. The global cap and 6 digit TOTP plus argon2 still bound it.
**Suggested fix**: Note it in the spec's risks, or ignore `X-Forwarded-For` unless `Tailscale-User-Login` is also present.

### 🟡 `Cache-Control: no-store` and CSP only on `text/html` and event-stream, `apps/dashboard/src/dashboard/web/middleware.py:62`
**Problem**: Redirects (login, logout, stop 303) and the plain-text 403 carry no `no-store`. The 303 after login carries the session cookie.
**Why it matters**: Low risk (the cookie is HttpOnly and the body is empty), but the spec's header set is described for every response of the app.
**Suggested fix**: Set `Cache-Control: no-store` for every non-static response.

### 🟡 Large generated and binary files committed, `.agents/skills/impeccable/scripts/live-browser.js`, `.impeccable/review/*.png`
**Problem**: About 13.5k lines of vendored skill script plus 7 review screenshots are in the diff. The spec follow-up does ask to commit `.agents/` and `skills-lock.json`, but the screenshots are working artifacts.
**Suggested fix**: Drop `.impeccable/review/*.png` (or gitignore them); keep the skill install as a separate commit.

### 🟡 Trader snapshot client is never closed, `apps/trader/src/trader/main.py:162`
**Problem**: `snapshot_client(...)` creates a redis-py client that is not `aclose()`d when the writer is cancelled.
**Suggested fix**: Close it in the writer's `finally` (or after `run_until_complete`).

### 🟡 Test gaps
Covered well: login, lockout, replay, sessions, CSRF, stop, 503, activity, poller, tokens, filters. Missing: retry from `/control/stop` referer (see Major 1), concurrent logins (Major 2), a subscribe versus publish race, and `shell.js` behavior (link status, sendError), which relies on live verification only.

## Nits
- ⚪ `apps/dashboard/src/dashboard/web/auth_routes.py:73`, the parameter `next` shadows the builtin (also line 85).
- ⚪ `apps/dashboard/src/dashboard/credentials.py:131`, `--dev` is documented as laptop only but nothing stops it on a live host.
- ⚪ `apps/dashboard/src/dashboard/live/poller.py:127`, `link_lost()` is documented to skip Redis, but `render_now` also returns it on timeout; the two docstrings could cross reference.

## Strengths
- Auth logic is pure, small, and fake clock tested: both factors evaluated on every attempt, replay guard under one lock and only advanced when the password matched, fixed window lockout with bounded keys, strict `safe_next` (rejects `//`, backslash, `/login`).
- The stop path is fail closed with a 1 s deadline on XADD, never says "sent" on failure, works as a plain form, and the 503 page avoids a second Redis read.
- CSP is `'self'` only with no inline script, style attribute, or `hx-on`; vendored assets are hash pinned and licensed; the poller bounds queues, restarts on crash, and renders `link lost` instead of stale values.

## Test coverage
Strong for new logic: about 140 dashboard tests plus core and trader tests for the envelope, control messages, and heartbeat writer, including error and Redis down cases. Gaps are the two scenarios in Major 1 and 2, the SSE subscribe race, and browser side JS.
