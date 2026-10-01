# 0003. Rationale: API keys & secrets

The decision record behind [index.md](index.md). `/develop` builds from `index.md`; this file says why.

## Context

The bot will hold an API key that can place real orders on a futures account with leverage. Today every credential is a development password in a gitignored `.env`, passed to services as environment variables through Compose substitution. Specs 0001 and 0002 both deferred real secret handling to this feature, and the `backup` service is a stub waiting for its keys.

The forces: one person operates everything, on one small VPS (4 GB) reached only over Tailscale, with the same Compose file on a laptop. Kraken shut down its futures demo, so demo needs no Kraken key at all; only the live trader ever holds one, and live trading starts at feature 19, months away. The NautilusTrader Kraken adapter reads key variables from the environment on its own when its config fields are empty, and NautilusTrader writes its own log lines (including component configs at startup) that our Python formatter never sees. Environment variables are visible to `docker inspect`, to `/proc/<pid>/environ`, and to every child process.

What a leak costs: a key with withdrawal rights can empty the account; a trade only key can still open bad positions. A leaked log or a public repo is the most likely leak path for a solo project, more likely than a break in to the VPS. The scope's done line asks for three things: keys loaded from a secret store, live keys unable to withdraw, and no secret in a leaked log or the repo.

Not deciding blocks feature 6 onward from using real credentials, leaves the backup stub idle, and keeps development passwords as the pattern every later feature copies.

## Options considered

### Option 1: Root owned env file on the VPS

Keep today's mechanism: a `.env` outside the repo, readable only by root, feeding `${VAR}` substitution, with each service listing only its own variables.

**Pros**:
- No change to code or Compose structure; nothing new to learn.
- Already restricts each secret to the services that list it.

**Cons**:
- Values sit in each container's environment, visible to `docker inspect`, `/proc`, and child processes, and dumped by any crash handler that prints the environment.
- The Kraken adapter's own environment lookup stays a second, silent way in.

### Option 2: Password manager master copy, Compose file secrets (chosen)

The master copy of each secret lives in your password manager. On the VPS each is a file in a root only folder; on the laptop a script generates random development files. Compose mounts each file read only at `/run/secrets/<name>` into only the services that need it, and one loader reads them at boot.

**Pros**:
- Nothing secret in any container's environment, command line, or `docker inspect` output.
- No new service, token, or tool on the VPS; the only copy there is the one the bot needs.
- The laptop runs the same code path with throwaway values, so tests cover production behaviour.

**Cons**:
- Placing files on the VPS is manual; rebuilding the box means placing them again.
- No version history of secrets beyond what the password manager keeps.

### Option 3: SOPS with age, encrypted in the repo

Secrets committed encrypted; the VPS holds one age private key and decrypts at deploy into the same files.

**Pros**:
- Versioned, reviewable, reproducible deploys; rebuilding the VPS needs only the age key.

**Cons**:
- Encrypted Kraken keys sit on GitHub forever; the age key on the VPS becomes the single key to every secret, past and present.
- Two more tools (sops, age) and a decrypt step in every deploy for a one person project.

### Option 4: Hosted secrets service

1Password service accounts, Infisical, Doppler, or Bitwarden Secrets Manager, fetched at start.

**Pros**:
- Good rotation and audit features; one place to change a value.

**Cons**:
- A long lived access token on the VPS that can read every secret, so the VPS still holds a master key.
- A third party in the startup path of the process that holds positions: their outage can stop a restart.

## Rationale

Option 2 wins because it removes the leak paths that matter most here (the environment, the logs, the repo) without adding a service, a token, or a tool to a 4 GB box that one person operates. Options 3 and 4 both put a master key on the VPS anyway, so they do not improve the "VPS compromised" case, and they add moving parts in exchange for rotation convenience that a handful of secrets rotated quarterly does not need. Option 1 fails the core goal: the environment is exactly where secrets leak from.

The sub decisions follow the same logic:

- **Files, never environment variables, for Kraken too.** Refusing `KRAKEN_*` variables everywhere and passing the key explicitly into the execution client config closes the adapter's silent environment fallback. Runner up: allowing env vars for the live trader, rejected as a second way in on the riskiest secret.
- **Our own loader, not pydantic `secrets_dir`.** pydantic-settings can read secret files, but gives no hook to register values for redaction, log a fingerprint, or raise a typed error with a reason, and it silently skips a missing folder. A forty line module does all four.
- **Generic mounted names** (`postgres_password`, `redis_password`) through the Compose `target` field, so code never knows which role it runs as; Compose alone decides. Runner up: per process names in code, rejected because a wrong name in one process is a silent cross role login.
- **Three leak layers.** `SecretStr` stops accidental prints; the formatter's redaction catches f strings and exception text; the canary test is the only thing that sees NautilusTrader's own output, which is where a config dump would leak. Any one alone has a known hole.
- **Fingerprint in the load log.** The first 8 hex of a sha256 of a high entropy key reveals nothing usable but tells you which key version runs. Runner up: name only, rejected because a rotation could not be confirmed from logs.
- **Proof of no withdrawal by checklist, not a probe.** A probe that calls a withdrawal endpoint is itself a call that could move money if the permission were ever on. The checklist plus a screenshot in the password manager is the safe proof.
- **Revoke at Kraken first in a leak.** Kraken's website works even if the VPS is compromised, takes seconds, and the exchange side stop on every position keeps protecting it while the bot is cut off. Runner up: flatten through the bot's kill switch first, rejected because it trusts the possibly compromised box.
- **Rotation every 90 days with a dated setting.** `KRAKEN_KEY_ROTATED_ON` is not secret and survives file copies and restores, unlike a file's modification time.
- **Fail closed key check.** A revoked key should stop the trader at start, not surface as rejected orders under a running strategy. No trading state change is needed meanwhile: an unauthenticated client cannot place an order, and `HALTED` would also block reducing orders on a restart mid position. A five minute pause before the exit keeps Compose's restart policy from hammering Kraken into a rate ban. The cross check of this spec raised these points and the backoff (2026-10-01).
- **Key check as a loop task, not an actor (updated 2026-10-01).** The first design used a `KeyCheckActor`. While building, it turned out NautilusTrader never starts the trader (so never starts any actor) when the Kraken client cannot connect, which is exactly the case the check exists for. A task on the node's event loop runs either way. The fresh model review then found that a Redis backed cache reloads the last run's account before Kraken connects, so the check now requires an account state newer than the start, and that the task was fire and forget, so a crash inside it now stops the node. The pure watcher moves out of `secrets.py` to `trading_core/node/key_check.py`: `secrets.py` is the loader and redaction that every process imports through `logs.py`, and the key check is a node startup concern. It stays free of Nautilus imports, so its tests need no Nautilus; the Nautilus predicate stays in `trader.main` next to the node it reads.
- **Laptop files are world readable inside a private folder.** Without swarm, a Compose file secret is a plain bind mount that keeps the host owner and mode, so a 0400 file owned by the laptop user cannot be read by the container's uid 10001 on Linux. The 0700 folder protects them on the laptop; on the VPS the files are owned by uid 10001 with mode 0400.
- **Hashed Redis ACL file on tmpfs.** Passing users as `--user` command line arguments is fragile to quote, and a missing password file would otherwise silently produce an empty password; checking each file, then writing only hashes to memory, avoids both.
- **Redis ACL users now.** The dashboard boundary ("writes only to the control stream") was a code convention; the server now enforces it, and the trader can no longer flush Redis, the one action spec 0001 warns could wipe both environments. The cost is two more secret files.
- **Backup provisioning only.** The job, its schedule, and the restore drill are operations work that belongs with deployment (feature 7); this feature only proves the container gets its keys.
- **Real key at go live.** The live execution path cannot run before the VPS exists (feature 7) or before go live is allowed (feature 19), so a key made now would sit idle for months while its rotation clock runs. Everything is proven with canary values instead.

## History scan

Run on 2026-10-01 with gitleaks v8.30.1: `gitleaks git --redact --config .gitleaks.toml --log-opts=--all .` over all 24 commits.

- **One finding, a false positive, nothing to rotate.** Rule `generic-api-key` matched `SECRET = "<32 hex>"` in `packages/core/tests/test_boot.py`, commit `9d469e1` on `feat/api-keys-secrets` (never pushed when found). It was a fake value written by the build for a unit test, not a credential. The test now generates the value at runtime, and the finding's fingerprint is in `.gitleaksignore` with that reason.
- **Before this feature**, the history held only the development passwords from `.env.example` (`change-me-dev-only` and friends), which gitleaks does not flag and which never protected anything real; the laptop now uses generated values from `scripts/dev-secrets.sh`.
- After the fix: `no leaks found`.
