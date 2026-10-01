# Review, feat/api-keys-secrets, 2026-10-01

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 49 files, branch vs main
**Verdict**: Changes requested

## Summary
This change moves every password and key into mounted secret files, adds one loader that fails closed and registers values for log redaction, splits the env guard, gives Redis a login per service, adds the live Kraken config path with a startup key check, and adds gitleaks plus a canary leak test. The design follows the spec closely and the unit tests are strong. The two headline issues are that the startup key check can pass for the wrong reason after a restart, and that the canary leak test never runs in CI.

## Major
### 🟠 Key check may pass from a cached account after a restart, `apps/trader/src/trader/main.py:159`
**Problem**: The check treats "an account for venue KRAKEN exists in the Nautilus cache" as proof the key works. The trader cache is backed by Redis with `flush_on_start=False`, and Nautilus loads stored accounts into the cache when the node is built. After one successful live run, the account is already in Redis, so on the next start the check may succeed at once, even when the key was revoked or is wrong. I could not run Docker to confirm that the loaded account is indexed by venue, so this is plausible rather than proven.
**Why it matters**: The spec's own consequence ("a revoked key shows up as a slow restart loop with kraken_auth_failed") would not hold, and the node would sit unauthenticated while the log says "kraken account confirmed". Feature 19 will depend on this.
**Suggested fix**: Confirm only with evidence from this session, for example an account event received after start or an account state timestamp newer than the start time, or clear the Kraken account entry before the check. Add a test that seeds an account into the cache before the check starts with a client that never authenticates, and expects failure. Check by hand: run once, then restart with a bad key.

### 🟠 The canary leak test never runs in CI, `apps/trader/pyproject.toml:16`
**Problem**: `addopts = "-m 'not redis and not canary'"` deselects the canary test, and CI runs plain `pytest -q` per package. The canary needs no Redis (the harness uses an in memory cache), so it could run on the Linux runner.
**Why it matters**: The core promise of the spec is that a leak is caught by a test. Today a future logging change, a Nautilus config change, or a new secret printed somewhere is only caught if someone remembers to run `-m canary`. Verify step 9 was a one time manual run.
**Suggested fix**: Add a CI step for `pytest -q -m canary` in `apps/trader` (about 70 seconds). Leave the Redis marked tests opt in, or add a Redis service to CI.

## Minor
### 🟡 Key check task is fire and forget, `apps/trader/src/trader/main.py:157`
**Problem**: The task from `loop.create_task` is not kept and has no done callback. If `is_confirmed` or `on_failed` raises, the exception is only reported when the task is garbage collected, and the check silently stops.
**Why it matters**: It breaks "never swallow an exception" and fails open: no timeout, no stop. An unauthenticated client cannot trade, so the harm is limited.
**Suggested fix**: Keep a reference, add a done callback that logs the exception critically and stops the node.

### 🟡 `watch_key_check` and the key state logic live in `secrets.py`, `packages/core/src/trading_core/secrets.py:125`
**Problem**: An asyncio polling loop and a trading key check now sit in the secrets loader module, which the spec describes as the loader and redaction. The spec also still says `KeyCheckActor`; the verify notes say index.md needs updating.
**Why it matters**: Cohesion and spec drift. The module is imported by `logs.py` on every process, so it grows its dependency surface.
**Suggested fix**: Move the key check to its own module under `trading_core/node/` (for example `key_check.py`) and update index.md through /architect.

### 🟡 CI downloads gitleaks without a checksum, `.github/workflows/ci.yml:29`
**Problem**: The binary is fetched with curl and piped to tar, with no checksum or signature check.
**Why it matters**: A supply chain risk in the job that guards the secrets. The version is pinned, which helps.
**Suggested fix**: Verify the published sha256 checksums file, or use the official gitleaks action pinned by commit.

### 🟡 `.gitleaksignore` fingerprint depends on the commit hash, `.gitleaksignore:4`
**Problem**: The ignore entry names commit `9d469e1`. If the branch is rebase merged or squashed and history is rewritten, the hash changes and the scan fails again (or the entry becomes dead).
**Why it matters**: A confusing red CI on main after merge.
**Suggested fix**: Merge with a merge commit, or recheck the scan after the merge and update the fingerprint.

### 🟡 Empty `KRAKEN_KEY_ROTATED_ON` from compose.live in a non live run, `compose.live.yaml:9`
**Problem**: `${KRAKEN_KEY_ROTATED_ON:-}` passes an empty string, which pydantic fails to parse as a date. In live that is the intended refusal, but the error message reads as a bad date rather than "required".
**Why it matters**: Slightly confusing operator message during the first live start.
**Suggested fix**: Treat an empty value as None in `TraderSettings` so the "required in live" message shows.

## Nits
- ⚪ `packages/core/src/trading_core/node/key_actors.py:21`, `KeyAgeConfig` is public in core and has no docstring (AGENTS.md rule).
- ⚪ `packages/core/src/trading_core/secrets.py:1`, a module named `secrets` shadows the stdlib name for anyone running a script from that folder; harmless with absolute imports.
- ⚪ `docker/backup/stub.sh:16`, the stub only checks non empty, while the loader also checks length and whitespace; fine for a stub, note for feature 7.
- ⚪ `docs/specs/0003-api-keys-secrets/verify.md`, steps 5 (VPS half) and 14 (CI half) are still open, as expected.

## Strengths
- One loader with typed reasons, raw, URL quoted and JSON escaped redaction, and fingerprints; the shell side (Redis start script, backup stub) mirrors the same rules.
- The canary harness logs every secret on purpose, so the test fails if redaction breaks, and it captures Nautilus's own output.
- Redis ACL is built from hashes on a tmpfs, nothing secret in process arguments, and Redis still runs unprivileged.
- Fail closed behavior throughout: env guard before secrets, secrets before config guard, live refused by an empty allowlist until feature 19.
- The key creation checklist, rotation and leak drill in verify.md are concrete.

## Test coverage
Good. The loader, redaction (including edge cases), key age on `TestClock`, the key check state machine and watcher, settings validation, the live config, env guard, and `run_node` paths all have unit tests. Gaps: no test for the cached account case in the first major finding, the Redis ACL tests and the canary are opt in markers and not run in CI, and there is no automated trader restart test (the restart was checked by hand).
