# Runbook: recover from a crash, a network loss, or a Kraken outage

_Spec 0023 (AC-26). For you, on the VPS, when something went wrong with a position open._

The trader checks its own books against Kraken (a **reconciliation check**) at every start, every 60 s, when the network comes back, and when you ask. Most problems fix themselves. This page tells you what you are looking at, and the few times you need to step in.

All commands run on the VPS as root. `control` below is short for:

```bash
sudo kfs-compose exec -T dashboard python -m dashboard.control_cli
```

## 1. First look

1. **Is the trader running?** `sudo kfs-compose ps trader`. A trader that keeps restarting on its own is normal while Kraken is unreachable (see section 3).
2. **What does it believe?** `control state` prints `trader_state`. Read `entries_blocked`, `halted`, `halt_reason`, `open_positions`, `protective_stops`, `last_check_outcome`, and `intent`.
3. **What does Kraken say?** Three ways, from quickest to most certain:
   - The dashboard's position card, column *Kraken*, with its age. "Kraken unknown" means the last fetch failed or is older than 3 minutes. It never means flat.
   - The exchange snapshot the last check wrote:
     ```bash
     sudo kfs-compose exec -T redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_dashboard_password)" redis-cli --user dashboard get live:snapshot:exchange'
     ```
   - Kraken's own website (futures.kraken.com, Open orders and Positions). This one is the truth. Use it whenever the two above disagree with it.
   - Only in drill mode (`LIVE_MODE=drill`): `sudo kfs-compose run --rm --no-deps -T trader python -m trader.drill show`.
4. **What did the checks find?** The top bar badge and the Reconciliation page on the dashboard (System, then Reconciliation), or the log:
   ```bash
   sudo kfs-compose logs --since 30m trader | grep -E '"code": "(reconcile_check|reconcile_mismatch|reconcile_failed|reconcile_halt)"'
   ```
5. **Run a check now** if you want a fresh answer: `control recheck`. It prints the outcome (`clean`, `mismatch`, `failed`).

## 2. What each entry block means

You see these in `entries_blocked` and on the dashboard. Each one only stops **new entries**. Exits and the stops resting on Kraken keep working.

| Block | Meaning | What you do |
|---|---|---|
| `reconciling` | A start check, or the check after a network drop, has not finished clean yet. | Nothing. It clears by itself on the next clean check. If it stays for more than a few minutes, the alerter warns you; then look at section 3. |
| `exchange_unknown` | The last check could not read Kraken (error, timeout, maintenance). The trader retries, 5 s first, doubling up to 60 s. | Check status.kraken.com. Nothing else, unless it lasts long and you hold a position: then watch the position on Kraken's website. It clears on the next clean check. |
| `order_unresolved` | An entry order was sent and Kraken has not answered yet, or Nautilus closed one it could not find. This stops a duplicate entry. | Nothing. It clears on Kraken's answer or the next clean check. |
| `stale_feed` | No fresh market data. Usually the network. When it clears, a `reconnect` check runs. | Nothing, unless it lasts. |
| `halted` | A latched halt (spec 0010). Only your Resume lifts it. | Section 5. |

## 3. Kraken or the network is down

- **While running.** You see `stale_feed`, then `reconciling`, and the periodic checks fail with `exchange_unknown`. The trader does not trade, and it never treats an error as "flat". Your stop stays on Kraken and protects the position. When the network comes back, a `reconnect` check runs and the blocks clear by themselves if it is clean.
- **At start.** The trader tries Kraken for up to 5 minutes (`RECONCILE_START_TIMEOUT_S`), logging `reconcile_start_retry`. Then it exits 1 (`reconcile_start_timeout`) and Docker starts it again. It stays healthy while it retries. It never starts a node whose state was not reconciled. Once Kraken answers, the start check runs and trading resumes on its own.
- **Kraken maintenance.** Expected 503s and failed checks. Wait it out. See status.kraken.com for the window.

You only need to act when an outage lasts and you hold a position you are not comfortable leaving to its stop. Then use Kraken's website to close it, and the next check flattens and halts what is left (an `unknown_position` or `position_mismatch`). Press Resume once the start check is clean.

## 4. What each finding means

A finding is something a check could not explain. Each one writes a row on the Reconciliation page, a critical risk event, and a critical log line that pushes to your phone.

| Finding | Meaning | What the trader did |
|---|---|---|
| `unknown_order` | An order on Kraken the trader does not know (not ours, or ours but closed locally). | Cancelled it. No halt. |
| `local_only_order` | An order in the trader's books Kraken no longer has. Usually a fill it missed while down, often the stop. | Fetched Kraken's fills and booked them (`cache_fix: recovered`), or marked it cancelled when there were none. No halt. |
| `missing_stop` | A position with no stop on Kraken. | Placed the stop again at the recorded trigger price. If the price had already passed it, closed at market. If it could not, flattened and halted. |
| `stop_size_mismatch` | The stop is the wrong size, or there are extra stops. | Placed the right one first, then cancelled the others. Never left the position without a stop. |
| `unknown_position` | A position on Kraken the trader cannot explain. | Closed it (the `ReconcileExecutor`) and halted with `reconcile_mismatch`. |
| `position_mismatch` | A position with the wrong side or size, or fills that are not ours. | Closed it and halted. When the trader's books hold more than Kraken, it is `cache_fix: manual`: see section 6. |
| `duplicate_entry` | Two entries for the same strategy, or a position built from two entries. | Cancelled every open entry, closed the position, and halted. |

While the trader is flattening or halted, a check only reports. It still cancels unknown entry orders, books missed fills, and closes an instrument whose books it no longer trusts. It never touches stops then.

## 5. When to press Resume

A `reconcile_mismatch` halt stays until you press Resume (dashboard, or `control resume`). Press it only when all of these hold:

1. Kraken is flat for the instrument, on Kraken's website, with no open order you did not expect.
2. You know why the finding happened (a crash, an outage, something you did by hand), and it will not repeat.
3. The last check is `clean`: run `control recheck` and read the outcome.
4. If the halt came with `cache_fix: manual`, you did the cache reset in section 6 first.

Resume also lifts a pause. A deploy still waits for flat as usual.

## 6. Reset the cache after a `cache_fix: manual` halt

This happens when the trader's books hold more than Kraken (for example a fill it can never recover). The trader no longer trusts its books for that instrument: it adds it to `live:reconcile:cache_untrusted`, halts, and has the `ReconcileExecutor` close whatever Kraken really holds. The books stay wrong until you reset them. Do it in this order:

1. **Let the executor finish.** Watch Kraken's website until the instrument is flat with no open order. The log shows `reconcile_residual_close`, then `reconcile_residual_flat`.
2. **Stop the trader** and make sure it really stopped:
   ```bash
   sudo kfs-compose stop trader
   sudo kfs-compose ps trader       # must not show it running
   ```
3. **Wait 30 s.** The reset refuses while the trader's heartbeat is fresh.
4. **Reset the cache:**
   ```bash
   sudo kfs-compose run --rm trader python -m trader.cache reset
   ```
   It prints how many keys it deleted. It deletes only the trader's Nautilus cache (`trader-<trader id>:*`) and the untrusted set. The halt stays latched.
5. **Start the trader:** `sudo kfs-compose start trader`.
6. **Check the start check is clean:**
   ```bash
   sudo kfs-compose logs --since 5m trader | grep '"code": "reconcile_check"'
   ```
   Look for `"trigger": "start"` with `"outcome": "clean"`. Then press Resume (section 5).

A reset while Kraken still holds a position is safe: the start check finds an `unknown_position`, so it flattens and halts again. Then start over at step 1.

## 7. Prove it still works

`scripts/restart-drill.sh demo` runs the demo drills on the laptop. `sudo /opt/kfs/current/scripts/restart-drill.sh live` runs the full live drill on the VPS, only with `LIVE_MODE=drill` set in `/etc/kfs/kfs.env`. Each drill prints what it expected, what it saw, and PASS or FAIL. Set `LIVE_MODE=off` again afterwards.
