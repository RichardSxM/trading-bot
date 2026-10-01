# 0003. Keep secrets in files that only their services can read, and prove they never leak

**Date**: 2026-10-01
**Status**: In Progress

## Summary

Every password and API key becomes a small file that Docker hands only to the services that need it, never an environment variable. The master copy of each secret lives in your password manager, the laptop gets random development values from a script, and the Kraken key exists only on the VPS, can trade but never withdraw, and only works from the VPS's address. Three independent layers keep secrets out of logs and the repo (masked types, a redaction step in the logger, gitleaks on every commit), and a canary test proves it, including NautilusTrader's own log lines. Redis gets a login per service, so the dashboard can only read snapshots and send commands. This feature builds and proves everything with fake keys; the real Kraken key is created when you go live (feature 19).

## Requirements

**User stories**:
- As the trader, I want the live Kraken key to be unable to withdraw and unusable from anywhere but the VPS, so a leaked key cannot take my money.
- As the trader, I want no secret in the repo, in any log, or in `docker inspect`, so sharing a log or a screenshot is always safe.
- As the operator, I want a process with a missing or broken secret to refuse to start with a clear message, so I never run half configured.
- As the operator, I want a written key creation checklist, a rotation routine, and a leak drill, so I act the same way under stress as on a calm day.

**Acceptance criteria**:
- **AC-1**: Each secret reaches only the services listed in the *Secret inventory*, as a read only file under `/run/secrets/`; `docker compose config` and `docker inspect <service>` show no secret value and no password or key environment variable for any service.
- **AC-2**: A process whose required secret file is missing, empty after trimming surrounding whitespace, a directory, unreadable, shorter than 16 characters, or containing inner whitespace refuses to start: exit 1 and one JSON log line with `code: secret_unavailable`, the secret's name, and the reason (`missing`, `empty`, `not_a_file`, `unreadable`, `too_short`, `whitespace`), never the value. (The length floor keeps redaction from shredding unrelated text.)
- **AC-3**: Each secret one of our processes (migrate, trader, dashboard, backup) loads produces exactly one INFO log line `secret loaded` with fields `secret` (its name) and `fp` (the first 8 hex characters of the sha256 of the value). Postgres reads its own file in the image entrypoint and logs nothing.
- **AC-4**: When any `KRAKEN_*` key variable (the four names in `env_guard.KRAKEN_KEY_VARS`) is set and not empty, every node refuses to start in every environment (`code: env_guard_refused`, exit 1).
- **AC-5**: Our JSON log formatter replaces every loaded secret value, in raw and in URL quoted form, with `[REDACTED:<name>]` wherever it appears in the line: the message, any field, and exception text.
- **AC-6**: The canary leak test runs a harness subprocess that loads runtime generated canary values through the real loader for every secret the live trader loads, builds and starts the live trader node through `build_config` with an allowlist that permits the Kraken execution client and the Kraken URLs pointed at a closed local port, and runs the key check; it captures all stdout and stderr of the process (NautilusTrader's own lines included) and fails if any canary value, raw or URL quoted, appears.
- **AC-7**: In live, the trader builds the Kraken execution client config through one factory, `trading_core.node.kraken.futures_exec_client(api_key, api_secret)`, with the values read from the secret files; the module allowlist `ALLOWED_EXEC_CLIENTS[("trader", "live")]` stays empty (feature 19 adds Kraken).
- **AC-8**: In live, once the node runs, the trader waits up to `KRAKEN_KEY_CHECK_TIMEOUT_S` (default 60) for a Kraken account state in the Nautilus cache that arrived after this start (an account reloaded from Redis by an earlier run does not count): when it appears it logs `kraken account confirmed`; when it does not, it logs one CRITICAL line with `code: kraken_auth_failed`, stops the node, waits `KEY_CHECK_FAIL_BACKOFF_S = 300`, and exits 1 (so `restart: unless-stopped` retries at most every few minutes, never hammering Kraken). The canary run of AC-6 proves the failure path (with a 2 second timeout and no backoff); unit tests prove both paths, the reloaded account case, and that a crash inside the check also stops the node and exits 1. The real entry point reaches this check only from feature 19, when Kraken joins the live allowlist.
- **AC-9**: In live, the trader refuses to start through boot's existing invalid configuration path (one CRITICAL `invalid configuration, refusing to start` line, exit 1) when `KRAKEN_KEY_ROTATED_ON` is missing, not an ISO date, or in the future (UTC); when the key is older than 90 days it logs one WARNING `kraken_key_rotation_due` (fields `rotated_on`, `age_days`) at start and once per UTC day while it runs.
- **AC-10**: Redis allows a client without a login only `PING`; `trader` and `dashboard` log in as their own Redis users; the `dashboard` user gets `NOPERM` for `SET`, `DEL`, `FLUSHDB`, and any write outside `*:control`; the `trader` user gets `NOPERM` for `FLUSHALL`, `FLUSHDB`, `CONFIG`, and `ACL`; a restarted trader reloads its Nautilus cache as `trader`.
- **AC-11**: Every Postgres password comes from a secret file; `.env.example` holds no password; changing a role password means replacing its file, running `migrate`, and restarting that service, with no code change; changing the owner password runs `scripts/set-pg-owner-password.sh`.
- **AC-12**: The `backup` service receives `restic_password`, `b2_key_id`, `b2_application_key`, and `postgres_password` (the `backup_app` role) as files, logs one `secret loaded` line with a fingerprint for each, then logs `backup not configured` as today.
- **AC-13**: gitleaks blocks a commit that adds a fake Kraken style key (pre-commit hook) and fails CI on a pushed one; the one time scan of the full git history is clean, or each finding is rotated and recorded in [rationale.md](rationale.md#history-scan).
- **AC-14**: `scripts/dev-secrets.sh` creates every non Kraken secret file in `${SECRETS_DIR:-./secrets}` (the eleven names under *File layout*) with a random value (`openssl rand -hex 32`, no trailing newline), the folder mode 0700 and each file mode 0444, never overwrites an existing file, and never creates a Kraken file; every service that mounts a file can read it as uid 10001 (verify step 5).
- **AC-15**: [verify.md](verify.md) holds the Kraken key creation checklist (General API full access, Withdrawal API no access, IP restricted to the VPS, screenshot kept in the password manager), the rotation routine for every secret, the VPS placement steps, and the leak drill (revoke at Kraken first).

## Decision

**Chosen option**: Option 2: password manager as the master copy, Compose file secrets as the only delivery.

Every secret is a file placed once from your password manager (VPS) or generated by a script (laptop), mounted by Compose read only at `/run/secrets/<name>` into only the services that need it, read at boot by one loader in `trading_core.secrets` that fails closed, fingerprints, and registers the value for log redaction; nothing secret is ever an environment variable.

**Implementation skills**: `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no table changes. No secret is ever stored in Postgres, Redis, or the Parquet catalog (spec 0002 already forbids it in JSONB). The "data" here is the secret inventory and the file layout below.

**Secret inventory** (host file name → name inside the container → services):

| Host file | Mounted as | Services | Master copy |
|---|---|---|---|
| `postgres_owner_password` | `postgres_password` | postgres (`POSTGRES_PASSWORD_FILE`), migrate | password manager (VPS) / script (laptop) |
| `postgres_trader_password` | `postgres_trader_password` (migrate), `postgres_password` (trader) | migrate, trader | same |
| `postgres_dashboard_password` | `postgres_dashboard_password` (migrate), `postgres_password` (dashboard) | migrate, dashboard | same |
| `postgres_backup_password` | `postgres_backup_password` (migrate), `postgres_password` (backup) | migrate, backup | same |
| `redis_trader_password` | `redis_trader_password` (redis), `redis_password` (trader) | redis, trader | same |
| `redis_dashboard_password` | `redis_dashboard_password` (redis), `redis_password` (dashboard) | redis, dashboard | same |
| `restic_password` | `restic_password` | backup | same |
| `b2_key_id`, `b2_application_key` | same names | backup | Backblaze, key scoped to one bucket, no `deleteFiles` |
| `kraken_api_key`, `kraken_api_secret` | same names | trader, live only, through `compose.live.yaml` | Kraken key page, VPS only, never on the laptop |

The recorder and the `tools` service get no secret. The two Kraken entries are declared (top level `secrets:`) and mounted only in `compose.live.yaml`. Mounting under a generic name (`postgres_password`, `redis_password`) through the Compose `target` field means every process reads the same names, and only Compose decides which role's password a service holds.

**File layout**:
- Compose top level `secrets:` maps each host file as `file: ${SECRETS_DIR:-./secrets}/<host file>`. `SECRETS_DIR` is used only for Compose substitution and reaches no service.
- Laptop: `./secrets/` (already gitignored, mode 0700), filled by `scripts/dev-secrets.sh` with files mode 0444. A Compose file secret without swarm is a plain bind mount that keeps the host owner and mode (the `uid`, `gid`, `mode` keys are ignored), so a 0400 file owned by your laptop user would be unreadable by uid 10001 on Linux; the 0700 folder is the protection on the laptop, the world readable mode is what lets the container read it. The script creates: `postgres_owner_password`, `postgres_trader_password`, `postgres_dashboard_password`, `postgres_backup_password`, `redis_trader_password`, `redis_dashboard_password`, `restic_password`, `b2_key_id`, `b2_application_key` (the last three are placeholders on the laptop; nothing uses them until feature 7).
- VPS: `SECRETS_DIR=/etc/kfs/secrets`, the folder owned by root with mode 0700, each file owned by uid 10001 (the image's `app` user) with mode 0400; this works because the bind mount keeps host ownership. The two Redis files need no different owner: the `redis` service's `sh -c` starts as root (the image only drops privileges when its first argument is `redis-server`), reads them, and then hands over to the image's own entrypoint (see *Redis ACL*). It lives outside the git checkout, so a `git clean` or a fresh clone never touches it.
- `compose.live.yaml` is an override used only on the VPS in live (`docker compose -f compose.yaml -f compose.live.yaml …`): it declares the two Kraken secrets at the top level, mounts them into `trader`, and adds `KRAKEN_KEY_ROTATED_ON: ${KRAKEN_KEY_ROTATED_ON:-}` and `KRAKEN_KEY_CHECK_TIMEOUT_S` to the trader's environment. The base file never names them, so the laptop needs no Kraken file and Compose never fails for a missing one.

**Loader and process contract** (`packages/core/src/trading_core/secrets.py`):

| Function / class | Inputs | Returns | Errors |
|---|---|---|---|
| `read_secret(name, secrets_dir)` | secret name, folder (from `Settings.secrets_dir`) | `SecretStr`; logs `secret loaded` with `fp`; registers the raw and `quote(value, safe="")` forms for redaction | `SecretUnavailable(name, reason)` |
| `load_required(names, secrets_dir)` | the process's required names | `Mapping[str, SecretStr]` | first `SecretUnavailable` |
| `fingerprint(value)` | a plain string | first 8 hex of sha256 | none |
| `redact(text)` | a log line's text | the text with every registered value replaced by `[REDACTED:<name>]` | none |
| `SecretUnavailable` | | typed exception; message names the secret and reason, never the value | |
| `reset_registry()` | none | clears the redaction registry; for tests only (the registry is process global) | none |
| `key_rotation_age(rotated_on, today)` | two UTC dates | age in days, or raises `ValueError` for a future date | `ValueError` |

- `boot()` gains a `secrets: tuple[str, ...]` parameter and runs in this order: (1) settings (a bad `KRAKEN_KEY_ROTATED_ON` fails here, AC-9), (2) logging, (3) the env variable guard `env_guard.check_environ(process)`, which needs no secrets, (4) `load_required`, mapping `SecretUnavailable` to the AC-2 line (its `env` field comes from the validated settings) and exit 1, (5) the config guard `guard(settings, secrets)`, which builds the node config with the secrets and calls `env_guard.check_exec_clients(...)`, (6) the clock check. It returns one object, `Boot(settings, secrets)`. The guard type becomes `Callable[[S, Mapping[str, SecretStr]], None]`.
- Consumers that do not go through `boot` call `load_required` themselves after configuring logging: `trading_core.db_roles.main` (the four migrate names) and the Alembic `migrations/env.py` (owner `postgres_password`, to build its URL). `db_roles.role_passwords` and `db_roles.connect` take the loaded secrets instead of `RoleSettings`, which is deleted.
- Required secrets per process: migrate `postgres_password`, `postgres_trader_password`, `postgres_dashboard_password`, `postgres_backup_password`; trader `postgres_password`, `redis_password`, plus `kraken_api_key` and `kraken_api_secret` in live; dashboard `postgres_password`, `redis_password`; recorder none.
- `Settings.postgres_password` is removed; `Settings.database_url` becomes `database_url(password: SecretStr) -> str`, called by the dashboard, the trader, Alembic `env.py`, and the integration test conftest (which writes throwaway files into a temporary folder and points `SECRETS_DIR_IN_CONTAINER` at it). Its quoted password is already registered for redaction by the loader.
- `JsonFormatter.format` passes the final JSON string through `redact` as its last step, so fields and exception text are covered by one call.
- The `backup` stub keeps its Alpine image and shell command; for each of `postgres_password`, `restic_password`, `b2_key_id`, `b2_application_key` in that order it checks the file is non empty (`test -s`, else prints a `secret_unavailable` line and exits 1) and prints `{"ts":…,"level":"INFO","process":"backup","logger":"backup","msg":"secret loaded","secret":"<name>","fp":"<8 hex>"}`, with `fp` from `printf %s "$(cat <file>)" | sha256sum | cut -c1-8`; then `backup not configured` as today.

**Kraken live path**:
- `futures_exec_client(api_key, api_secret)` in `trading_core/node/kraken.py` builds `KrakenExecClientConfig` with the key fields set explicitly (so the adapter never falls back to its own environment lookup) and `environment=KrakenEnvironment.LIVE`; optional URL overrides exist only as parameters the canary harness uses.
- `trader.main.build_config(settings, secrets, kraken_urls=None)`: in live it adds the Kraken execution client from the factory and registers `KrakenLiveExecClientFactory`; `guard(settings, secrets, allowlist=ALLOWED_EXEC_CLIENTS)` checks it against the passed allowlist, so with the module default the live trader is refused until feature 19 adds Kraken. The parameter exists so the canary test can pass its own allowlist without patching the module.
- The env guard splits in two: `check_environ(process, environ)` refuses any non empty `KRAKEN_*` key variable in every process and environment (the old `env == "demo" or process == "recorder"` condition goes), and `check_exec_clients(exec_clients, process, env, allowlist=ALLOWED_EXEC_CLIENTS)` keeps the allowlist check.
- `TraderSettings(Settings)` adds `kraken_key_rotated_on: date | None`, required (validator) when `trading_env == "live"`; `boot("node", settings_cls=TraderSettings, …)`.
- **Key check (AC-8)**: no trading state change is needed, since an unauthenticated client cannot place an order anyway, and holding `HALTED` would also block reducing orders on a restart mid position. The check is a task on the node's event loop, not an actor: when the Kraken client cannot connect, NautilusTrader never starts the trader, so an actor would never run.
  - `trading_core/node/key_check.py` (no Nautilus import) holds the pure `key_check_state(account_confirmed, elapsed_s, timeout_s)` and `watch_key_check(is_confirmed, on_failed, timeout_s, poll_s, now)`, which polls once a second (or a quarter of the timeout, whichever is shorter), logs `kraken account confirmed` when confirmed, and at the deadline logs `kraken_auth_failed` and calls `on_failed`.
  - `trader.main.kraken_account_confirmed(cache, since_ns)` is the predicate: `cache.account_for_venue(Venue("KRAKEN"))` is not None and its `last_event.ts_init >= since_ns`. NautilusTrader 1.231.0 reloads accounts from Redis at build (`load_cache` defaults to on, and `flush_on_start=False`), so only a state created by this run proves the key works.
  - `trader.main.run_node` records `started_ns = time.time_ns()` before building the node, creates the task with `loop.create_task(...)`, keeps it in a local for the life of the node, and adds a done callback: if the task raised, it logs CRITICAL `kraken_key_check_crashed` with the exception and stops the node (fails closed). `on_failed` and the callback both set a `failed` flag and call `node.stop()`; after `node.dispose()`, a set flag sleeps `fail_backoff_s` (default `KEY_CHECK_FAIL_BACKOFF_S`) and returns 1.
  - The timeout and backoff are parameters of `run_node`, so the canary harness uses 2 and 0. Unit tests drive `watch_key_check` with a fake clock, the predicate with a real `Cache` seeded with an account state at a chosen `ts_init`, and `run_node` with a fake node.
- **Key age (AC-9)**: a small `KeyAgeActor` in `trading_core/node/` calls `key_rotation_age` in `on_start` and then on a 24 hour timer on the Nautilus clock whose first fire is the next UTC midnight, and logs `kraken_key_rotation_due` when the age is over `KEY_MAX_AGE_DAYS = 90`.

**Redis ACL** (`compose.yaml`, `redis` service):
- The service mounts `redis_trader_password` and `redis_dashboard_password`, has a `tmpfs` at `/run/redis-acl`, and starts with `sh -c` that, for each file, refuses to start unless it is non empty (`test -s`, else one JSON `secret_unavailable` line and exit 1, so a missing file never becomes an empty password), computes its sha256 (`printf %s "$(cat /run/secrets/<file>)" | sha256sum`), writes the user lines below with `#<hash>` to `/run/redis-acl/users.acl` (owner `redis`, mode 0400), then `exec docker-entrypoint.sh redis-server --aclfile /run/redis-acl/users.acl` plus today's flags, so Redis itself runs as the image's `redis` user, not root. Only hashes are written, and nothing secret appears in process arguments.
- Users: `default on nopass resetkeys -@all +ping` (keeps the Compose healthcheck and `redis_check.redis_reachable` working without a login) · `trader on #<hash> ~* &* +@all -@admin -flushall -flushdb` · `dashboard on #<hash> resetkeys %R~*:snapshot:* %W~*:control -@all +@read +xadd +ping +hello +auth +client|setname +client|setinfo`.
- The trader passes `username="trader"` and the password to `DatabaseConfig` (confirm the field names in 1.231.0). Each process gets `REDIS_USERNAME` (non secret) in its Compose environment; `Settings` gains `redis_username: str | None`.
- A feature that adds a new Redis key the dashboard must read or write extends the `dashboard` rule in that feature's spec.

**Canary harness** (`apps/trader/tests/canary_main.py`, run by `apps/trader/tests/test_canary.py` as a subprocess, marker `canary` registered in the trader's pytest config, dev image only):
- The test generates fresh random canary values at runtime (no literals, so gitleaks needs no allowlist), writes them as files into a temporary folder, and starts the harness with `TRADING_ENV=live`, `SECRETS_DIR_IN_CONTAINER=<tmp>`, `KRAKEN_KEY_ROTATED_ON=<today>`, `PROCESS_NAME=trader`.
- The harness skips `boot` (it needs NTP and Redis): it configures logging, runs the real `load_required`, builds the node with `build_config(settings, secrets, kraken_urls=<http and ws at 127.0.0.1:9>)` but with an in memory cache (no Redis database) and no data client, checks it against an allowlist containing Kraken, runs it through `run_node` with the key check at 2 s timeout and 0 backoff, and must exit 1 with `kraken_auth_failed`.
- The test captures the subprocess's stdout and stderr (NautilusTrader's Rust logger writes there directly) and fails if any canary value, raw or URL quoted, appears, or if the exit code or the `kraken_auth_failed` line is missing.

**Repo guard** (gitleaks):
- Pin one gitleaks version in both `.pre-commit-config.yaml` (the `gitleaks/gitleaks` hook repo) and the CI step, and bump them together, like ruff.
- `.gitleaks.toml` extends the default rules with one rule `kraken-api-secret`: a value of 40 or more base64 characters assigned to a name that contains `kraken` and `key` or `secret` (case insensitive).
- CI checks out with `fetch-depth: 0` and runs `gitleaks git` on the pushed range; the one time history scan runs `gitleaks git --log-opts=--all` locally.
- The hook test commits a scratch file with `KRAKEN_FUTURES_API_SECRET=` followed by 64 random base64 characters.

**API surface** (scripts and commands, the operator's interface):

| Command | Inputs | Output | Errors |
|---|---|---|---|
| `scripts/dev-secrets.sh` | `SECRETS_DIR` (default `./secrets`) | missing files created, existing ones listed as kept | exit 1 when the folder is not writable |
| `scripts/set-pg-owner-password.sh` | none (reads the mounted `postgres_password` inside the `postgres` container over its local socket, value passed through a heredoc, never argv) | `ALTER ROLE` applied | exit 1 when postgres is not running |
| `docker compose run --rm migrate` | role password files | roles updated (`ALTER ROLE … PASSWORD`), schema migrated | AC-2 refusal |
| `docker compose -f compose.yaml -f compose.live.yaml up -d` | Kraken files, `KRAKEN_KEY_ROTATED_ON` | live trader (refused by the allowlist until feature 19) | AC-2, AC-4, AC-8, AC-9 |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| any secret load | secret value | `/run/secrets/<mounted name>`, from `Settings.secrets_dir` (default `/run/secrets`; its env alias is `SECRETS_DIR_IN_CONTAINER`, set only by tests, so it never collides with the Compose only `SECRETS_DIR`) |
| `secret loaded` log | `fp` | derived: first 8 hex of sha256 of the trimmed value |
| redaction | the strings to replace | the loader's registry: raw value and `urllib.parse.quote(value, safe="")` |
| Postgres connection | user, password | `POSTGRES_USER` (Compose env, per service) and the mounted `postgres_password` |
| Redis connection | user, password | `REDIS_USERNAME` (Compose env) and the mounted `redis_password` |
| Redis ACL hashes | `#<sha256>` per user | derived inside the `redis` container from its mounted files |
| Kraken exec client | api key, api secret | mounted `kraken_api_key`, `kraken_api_secret` (live override only) |
| key check | "authenticated" | derived: `cache.account_for_venue(Venue("KRAKEN"))` is not None and its `last_event.ts_init` is at or after `started_ns` |
| key check | `started_ns` | `time.time_ns()` in `run_node`, taken before the node is built |
| key check | timeout | `KRAKEN_KEY_CHECK_TIMEOUT_S` setting on `TraderSettings`, default 60 (Nautilus's own connect timeout is 30 s, so this leaves room for a slow start) |
| key check | backoff before exit | constant `KEY_CHECK_FAIL_BACKOFF_S = 300` in the trader |
| key age warning | `age_days` | `key_rotation_age(KRAKEN_KEY_ROTATED_ON, today)`, today from the UTC clock (Nautilus clock in the actor) |
| key age warning | threshold | constant `KEY_MAX_AGE_DAYS = 90` in `trading_core.secrets` |
| dev secret values | random value | `openssl rand -hex 32` |
| VPS folder | host path | `SECRETS_DIR` in the VPS `.env` (Compose substitution only) |

**Key invariants**:
- No secret value is ever an environment variable, a command line argument, a log field, a Postgres or Redis value, or a tracked file.
- Every process loads all its required secrets right after the env variable guard, before it touches the network or another service; a missing one stops it (fail closed).
- Kraken keys exist only on the VPS, only in live, only inside the `trader` container.
- Secret values contain no whitespace; every consumer (Python loader, shell hashing, psql heredoc) trims only the trailing newline and gets the same value.
- The live Kraken key never has withdrawal permission; no code calls a withdrawal endpoint.

**Security model**:
- Least privilege per service: each service receives only its own row's secrets from the inventory (the migrate service is the only one that holds all three role passwords, as in spec 0002).
- Redis: the dashboard may only read `*:snapshot:*` and append to `*:control` (the "dashboard writes only to control" rule enforced by the server); the trader may do anything except admin commands and flushes; anyone without a login may only `PING`.
- Kraken: General API full access, Withdrawal API no access, IP restricted to the VPS's public IPv4 if Kraken's key page offers it. A leaked key can at worst place trades, which exchange side stops and feature 9 limit.
- VPS: secret files readable only by uid 10001 inside a root only folder; the master copy and the permission screenshot live in your password manager.
- No regulated personal data is involved; this is credential hygiene, not a compliance scope.

**Configuration required**:
- `SECRETS_DIR`: host folder of the secret files, Compose substitution only (laptop default `./secrets`, VPS `/etc/kfs/secrets`).
- `KRAKEN_KEY_ROTATED_ON`: UTC date the live Kraken key was created, required in live, VPS only.
- `REDIS_USERNAME`: Redis user per service, set in `compose.yaml` (not in `.env`).
- `KRAKEN_KEY_CHECK_TIMEOUT_S`: seconds to wait for the Kraken account at a live start, default 60, passed through `compose.live.yaml`.
- `SECRETS_DIR_IN_CONTAINER`: tests only, points the loader at a temporary folder.
- Removed from `.env` / `.env.example`: `POSTGRES_PASSWORD`, `POSTGRES_TRADER_PASSWORD`, `POSTGRES_DASHBOARD_PASSWORD`, `POSTGRES_BACKUP_PASSWORD`.
- Prerequisites outside code: a Backblaze B2 bucket and a key scoped to it (before feature 7 uses it); the Kraken key itself at feature 19.

**Critical test scenarios**:
- Happy path: `scripts/dev-secrets.sh`, then `docker compose up -d`: every service healthy, one `secret loaded` line per secret with a fingerprint, verifies **AC-1**, **AC-3**, **AC-11**, **AC-14**.
- Failure case: delete `secrets/postgres_dashboard_password`, start the dashboard: exit 1, `secret_unavailable`, reason `missing`, no value; repeat for empty, a directory, and a value with a space, verifies **AC-2**.
- Leak: canary leak test over the live trader startup, verifies **AC-5**, **AC-6**, **AC-7**, **AC-8**.
- Permission: `redis-cli --user dashboard` runs `SET x 1` → `NOPERM`; `--user trader` runs `FLUSHALL` → `NOPERM`; no login, `GET x` → `NOAUTH`/`NOPERM`, verifies **AC-10**.
- Guard: `-e KRAKEN_FUTURES_API_KEY=x` on the trader with `TRADING_ENV=live` → `env_guard_refused`, verifies **AC-4**.
- Key age: `KRAKEN_KEY_ROTATED_ON` 91 days ago → warning; tomorrow's date → refused, verifies **AC-9**.

## Build plan

Tracer Bullet: one secret goes all the way through first (script, file, Compose, loader, log, redaction, connection), then each segment thickens to the full inventory.

1. [x] **Thin thread.** `trading_core/secrets.py` (loader, `SecretUnavailable`, fingerprint, redaction registry and `reset_registry`, `key_rotation_age`) with unit tests; `boot(secrets=…)` in the new order, the split env guard, and the AC-2 exit path; `redact` as the last step of `JsonFormatter.format`; `scripts/dev-secrets.sh`; Compose top level `secrets:` with `SECRETS_DIR`; the dashboard mounts `postgres_dashboard_password` as `postgres_password` and connects with it, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-14**.
2. [x] **All Postgres secrets.** `postgres` uses `POSTGRES_PASSWORD_FILE`; migrate, trader, and backup roles from files; `db_roles.main` and Alembic `env.py` load their own secrets, `RoleSettings` is deleted, and `Settings.database_url(password)` replaces the env password at every call site; `scripts/set-pg-owner-password.sh` (also the laptop cutover for the existing `pgdata` volume); passwords removed from `.env.example`; update `test_settings.py`, `test_db_roles.py`, and the integration conftest to the file path, satisfies **AC-1**, **AC-11**.
3. [x] **Redis ACL.** `redis` service users from hashed files; `REDIS_USERNAME` and `redis_password` for trader and dashboard; `DatabaseConfig` login for the trader; integration test of the denials, and a trader restart that reads back a cache entry written before the restart (this also proves the trader's ACL covers every command the 1.231.0 Redis cache uses), satisfies **AC-10**.
4. [x] **Kraken live path.** Env guard live cases (update `test_env_guard.py` and 0001's guard tests); `futures_exec_client` with optional URL parameters; `build_config(…, kraken_urls=…)` and `guard(…, allowlist=…)`; `compose.live.yaml`; `TraderSettings` with `KRAKEN_KEY_ROTATED_ON` and `KRAKEN_KEY_CHECK_TIMEOUT_S`; `KeyAgeActor`; the key check task and the exit with backoff; the canary harness and test (see *Canary harness*). If the canary finds a value in NautilusTrader's own output, mitigating it is part of this task, satisfies **AC-4**, **AC-6**, **AC-7**, **AC-8**, **AC-9**.
5. [x] **Backup secrets, repo guard, runbook.** Backup stub mounts and fingerprints its four files; gitleaks (see *Repo guard*) as a pre-commit hook and a CI step before `Sync`; run `gitleaks git` over the full history and record the result in `rationale.md`; finish the runbook in `verify.md`; update `packages/core/AGENTS.md` key files for `secrets.py`, satisfies **AC-12**, **AC-13**, **AC-15**.
6. [ ] **Key check module.** Move `KeyCheckState`, `key_check_state`, and `watch_key_check` from `trading_core/secrets.py` to `trading_core/node/key_check.py` unchanged (logger name follows the module), move their tests from `test_secrets.py` to `test_key_check.py`, repoint the imports in `trader.main` and the `key_actors.py` docstring, and drop `asyncio`, `time`, `Callable`, and `Literal` from `secrets.py` if nothing else there uses them, satisfies **AC-8** (no behavior change).

## Consequences

**Positive**:
- A secret never shows in `docker inspect`, `/proc/<pid>/environ`, a child process, or a pasted log; the worst case of a leaked Kraken key is unwanted trades, never a withdrawal.
- The laptop runs exactly the VPS code path (same files, same loader), so tests cover what production runs.
- The fingerprint line lets you confirm a rotation took effect from logs alone.
- Redis permissions now enforce the dashboard boundary from spec 0001 at the server, and the trader can no longer flush Redis by mistake.

**Negative / tradeoffs**:
- Placing secrets on the VPS is a manual step (copy from the password manager, set owner and mode); losing the VPS means placing them again by hand. Feature 7 should script it.
- Every new secret now touches four places: the inventory table, Compose, the process's required list, and the dev script (and the password manager on the VPS).
- Redis ACL rules must grow with every new key the dashboard touches; a forgotten rule shows up as `NOPERM` at runtime, not at build time.
- The canary test depends on NautilusTrader's startup output; a Nautilus upgrade can change it, so the test reruns on every upgrade.
- The laptop's existing `pgdata` volume keeps its old owner password until `scripts/set-pg-owner-password.sh` runs once (or the volume is recreated).

**Neutral**:
- `docker compose` on the VPS in live takes two `-f` files; feature 7 should wrap that in its deploy command.
- A live start with a bad key now costs a five minute pause per retry; a revoked key shows up as a slow restart loop with a `kraken_auth_failed` line each time, which feature 12 turns into an alert.
- Scope wording "separate demo and live keys" no longer applies: demo has no Kraken keys at all (spec 0001 amendment).

## Follow-up

- [ ] `/scope`: reword feature 4 (no demo keys; "loads keys from a secret store" means the Compose file secrets here), move the backup job, nightly `downsample_snapshots()`, and restore drill to feature 7 (spec 0002's follow up item named feature 4), and add to feature 19: create the live key per [verify.md](verify.md#kraken-key-creation-checklist), set `KRAKEN_KEY_ROTATED_ON`, and add Kraken to `ALLOWED_EXEC_CLIENTS[("trader", "live")]`.
- [ ] Feature 7: script the VPS secret placement and wrap the two file `docker compose` command in the deploy routine.
- [ ] Feature 12: turn `kraken_key_rotation_due`, `kraken_auth_failed`, `kraken_key_check_crashed`, and `secret_unavailable` into phone alerts.
- [ ] Feature 5 and feature 12: their new secrets (dashboard session secret, push service token) follow the four step recipe in *Negative / tradeoffs*.
- [ ] `/sync`: add a root `AGENTS.md` rule ("secrets only as files via `trading_core.secrets`, never env vars"), gitleaks in `## Tooling`, and record the declined gitleaks Agent Skill under `Declined:`.
