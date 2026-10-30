# 0023. Restart and outage safety: decision record

## Context

The trader holds positions on Kraken Futures through NautilusTrader 1.231.0. Today it has no way to rebuild its view from Kraken. `LiveExecEngineConfig(reconciliation=False)` is set in `apps/trader/src/trader/main.py`, with a comment deferring it to this feature. That was safe while only demo ran: demo trades through Nautilus's sandbox (a simulated exchange inside the trader), Kraken's futures demo was shut on 14 July 2026, and spec 0006 settled that a demo restart closes leftovers as abandoned. Live is refused by the env guard until feature 19, and this feature must be done before it.

Several pieces already exist and must keep working:
- Spec 0010's latched halt key in Redis, which survives restarts and flattens 5 s after start.
- Spec 0006's `stale_feed` entry block, its recording queue and spool, and its abandoned close.
- Spec 0005's deploy, which waits for flat, and its autoheal restarts.
- Spec 0013's alerter, which pushes any CRITICAL log line that carries an `event_key`.
- The `reconcile_mismatch` and `orphan_fill` risk kinds and the `reconciliation` order origin, which exist in the schema but have no producer.

The forces:
- **Restarts are routine.** Autoheal, reboots, and crashes all restart the trader, and spec 0005 wants it to heal itself without you. A restart that needs a human every time defeats that.
- **The adapter fails open.** In 1.231.0 the Kraken execution client catches any exception while building order, fill, or position reports, logs it, and returns an empty list. Nautilus reads an empty list as "nothing open". During one of Kraken's scheduled maintenance windows (503s), that could make the trader believe it is flat while a position sits on Kraken. Your rules say anything touching orders fails closed.
- **No private reconnect signal.** The pinned Kraken Futures websocket client exposes no reconnect callback to Python, so "after a dropped connection" needs a proxy.
- **Nothing to practise on.** With no Kraken demo, the only real exchange is your live account. Without a deliberate plan, the first real restart with a position would happen with real capital in feature 19.
- **Strategies keep state in memory.** Exit timers and exit plans die with the process, so even a perfectly rebuilt position is unmanaged unless the strategy can pick it back up.
- **One trader, a bot only account.** Anything on the account the bot did not create is an anomaly, not your manual trade.

Without this decision, feature 19 would put real money on a trader that, after any crash, either forgets a live position or trusts a view that a single 503 can corrupt.

## Options considered

### Option 1: Nautilus reconciliation plus our own fail closed check (chosen)

Turn Nautilus reconciliation on, so the engine rebuilds its cache from Kraken's reports at start (orders, fills, positions, including inferred fills for gaps). Add our own check on top. It fetches Kraken's view through a wrapper that raises on errors, compares it with the cache in a pure core function, takes the safe actions, gates entries, and writes a report. It runs at start, on reconnect, every 60 s, and on demand.

**Pros**:
- The engine does the hard, version specific work of rebuilding order and position state. We don't reimplement it.
- Our layer closes the fail open gap and owns the policy (adopt, replace, flatten, halt), which stays pure and unit testable in core, per the Clean Architecture rule.
- One report per check gives evidence and a dashboard view.

**Cons**:
- Two layers to understand. A bug can come from Nautilus's reconciliation or ours.
- Our periodic check duplicates part of what Nautilus's open and position checks would do, so we must switch those off to avoid two opinions.
- Every strategy base needs a resume hook.

### Option 2: Nautilus reconciliation only

Turn on `reconciliation`, `open_check_interval_secs`, and `position_check_interval_secs`, and trust the engine.

**Pros**:
- Least code. The engine's built in path, maintained upstream.

**Cons**:
- It inherits the adapter's "error reads as empty" gap. A maintenance 503 during a periodic check can mark live orders closed locally.
- No policy of our own: unknown positions are adopted as `EXTERNAL` and nobody manages them. No entry gate, no report, no alert.
- Strategies still lose their exit timers.

### Option 3: Our own reconciler only

Keep Nautilus reconciliation off and rebuild state ourselves from Kraken's REST API.

**Pros**:
- Full control of every step, with no surprises from the engine.

**Cons**:
- We would have to write order and position state into the Nautilus cache ourselves, which the engine's internals do not support cleanly, and which breaks on every Nautilus upgrade.
- Much more code, for a one person team.

### Option 4: Always flatten on restart

On every live start, cancel everything and close every position, then trade fresh.

**Pros**:
- Simplest possible rule, and easy to reason about.

**Cons**:
- Every autoheal, reboot, or crash turns a good trade into a forced exit, paying the spread and fees. That conflicts with spec 0005's self healing.
- It still needs a fail closed view of Kraken to know what to close, so most of Option 1's fetch work remains.
- It does nothing for a network blip while the process stays up.

## Rationale

Option 1 is the only one that answers all the forces. The engine is better than us at rebuilding cache state, so we use it, as the stack decision (spec 0001) intended. It also has a gap that matters exactly when it matters most: Kraken maintenance is a known, recurring event, and in 1.231.0 an error during it reads as "nothing open". So the decision on what is true, and what to do about it, sits in our own layer, which treats any error as unknown. That layer is pure core logic behind two views, so it can be tested exhaustively without infrastructure, which is the only way to cover crash timings we cannot easily reproduce.

The policy follows your answers. Adopt what our own fills explain (a partial fill is still ours). Replace a stop at its recorded price, placing the new one before cancelling the old. Cancel any unknown order, which is safe because the account is bot only. Flatten and latch a halt for anything unexplained. Entries resume by themselves after a clean check, so autoheal keeps working, while a latched halt still needs your Resume. Option 4 was rejected because it punishes routine restarts. Option 2 was rejected because it leaves the fail open gap and has no policy.

Within the chosen option, the calls I made:
- **`stale_feed` clearing as the reconnect trigger.** The pinned websocket client has no reconnect callback, and a network loss hits both sockets. The 60 s check is the backstop. Runner up: polling `is_active()` on the private socket, which reaches into adapter internals.
- **A pre build start fetch.** Fetching through our wrapper before Nautilus's own startup reconciliation means the engine never reconciles against an unreachable Kraken. Runner up: letting Nautilus reconcile first and then correcting, which can cancel and re place our own stop after a 503.
- **The stop price source.** The cache first, because Redis keeps it in live. Postgres second, because the recording queue can lag a crash. Runner up: a settings distance from the mark, which you rejected, because it changes the strategy's risk.
- **Drill mode on the real account.** Without a Kraken demo, it is the only way to see Kraken's real report shapes, real acknowledgement timing, and a real position across a restart before feature 19. A minimum size position costs cents. Runner up: a fake Kraken server, which proves only what the fake models.
- **Nautilus periodic checks off.** Two periodic opinions with different failure behaviour would fight. Ours fails closed.

Added after the cross check (another model read the draft):
- **A `ReconcileExecutor` strategy.** The kill switch closes positions only through each strategy's own exit, and a Nautilus Actor cannot submit orders, so an unknown position had no one to close it. A small strategy that owns only reconcile actions fills that gap and joins the kill switch flatten. Runner up: letting the kill switch submit orders itself, which would break spec 0010's single exit path.
- **Quiet compare plus a confirming fetch.** Kraken's view and the cache are read at different moments, so a fill in between looks like a mismatch, and every unexplained mismatch halts. Comparing only when nothing is in flight, and acting only on what two fetches agree on, removes that false halt source.
- **One target stop, repeatable.** Defining the goal as exactly one full size stop at the recorded price makes the replace safe to repeat after a crash between placing and cancelling. This amends the "stop never cancelled" rule of specs 0006 and 0010 for that one case only.
- **Report only in demo, and while flattening or halted.** The sandbox view can only produce false findings, and acting during a flatten would race the kill switch.
- **Heartbeat during the start retry.** The node healthcheck has a 90 s start period, so without it autoheal would kill the 300 s start retry at about two minutes.

Added after the task 3 build (2026-10-29), when the build found that AC-8 as written cannot work in 1.231.0:
- **Recover missed fills, else halt and reset by hand.** A netting fill goes to `{instrument}-{strategy_id}` (`execution/engine.pyx:1561`), so a flat or smaller position report makes Nautilus add an opposite `EXTERNAL` position instead of shrinking ours. The usual reason the cache holds more is a missed fill of our own order, most often the stop. Feeding Kraken's real fill for that order to Nautilus closes the strategy's own position the normal way, and it also stops AC-11 from marking a filled stop canceled. The adapter already does this itself for a single order (`_generate_futures_filled_status_report`, a 5 minute window); we use the same `request_fill_reports` call with the full lookback. What recovery cannot explain is rare and halts. You reset the cache by hand, and the next start rebuilds it from Kraken. Runners up: a made up closing fill on the strategy's order (fully automatic, but it records a fill that never happened and wrong P&L), and halt plus a hand reset every time (it halts on every missed stop fill).
- **The executor closes Kraken's real size, reduce only.** When the cache is wrong, the strategy's own close would send the cache's size. A reduce only order for Kraken's quantity can never open or flip a position. Runner up: halt and close by hand.
- **`uses_stop` on the strategy.** `BuyAndHold` and `RandomTaker` never place a stop by design, so AC-9 would flag every check. The owning strategy now declares whether its positions carry a stop. Runners up: giving them stops (it changes how they trade), and refusing them in live (it keeps a critical alert every minute in demo).

## Evidence: the code and engine facts this decision rests on

Checked on 2026-10-29 in the trader image (NautilusTrader 1.231.0):

- **Our config.** `apps/trader/src/trader/main.py:195` sets `LiveExecEngineConfig(reconciliation=False)`. `CacheConfig` uses Redis with `flush_on_start=False` and `use_instance_id=False`, so live cache keys survive restarts. Demo purges them (`purge_demo_cache`).
- **Engine defaults.**

  | Setting | Default |
  |---|---|
  | `reconciliation` | `True` |
  | `reconciliation_lookback_mins` | `None` |
  | `reconciliation_startup_delay_secs` | 10.0 |
  | `inflight_check_interval_ms` | 2000 |
  | `inflight_check_threshold_ms` | 5000 |
  | `inflight_check_retries` | 5 |
  | `open_check_interval_secs` | `None` |
  | `position_check_interval_secs` | `None` |
  | `generate_missing_orders` | `True` |
  | `filter_unclaimed_external_orders` | `False` |
- **Adapter reports.** `nautilus_trader/adapters/kraken/execution.py` implements `generate_order_status_reports`, `generate_fill_reports`, and `generate_position_status_reports` for futures. Each wraps its request in `except (asyncio.CancelledError, Exception)`, logs, and returns whatever was collected, which is empty on an error (around lines 445 and 847).
- **Startup failure.** `system/kernel.py:1342` logs "Execution state could not be reconciled" and returns `False` when `reconcile_execution_state` fails. The node then does not start trading.
- **In flight check.** `live/execution_engine.py:1475` and `:1505` mark an `ACCEPTED` or `SUBMITTED` order that is "not found at venue" as `REJECTED` after the retries. Combined with the fail open adapter, this is why AC-14 blocks entries until our own check confirms.
- **No reconnect hook.** `KrakenFuturesWebSocketClient` exposes `connect`, `close`, `is_active`, `is_closed`, `wait_until_active`, and the subscribe methods, but no reconnect callback.
- **Strategy bases with protective stops.** `node/placeholder.py`, `node/bar_rule.py`, `node/scalp_base.py`, and `node/macd_baseline.py` place `stop_market` orders with `reduce_only=True` and GTC, tagged with `parent:`. `orders.trigger_price` and `parent_client_order_id` exist in Postgres.
- **Env guard.** `ALLOWED_EXEC_CLIENTS[("trader", "live")]` is empty today (`packages/core/src/trading_core/env_guard.py`).
