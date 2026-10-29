# Verify: restart & outage safety · spec 0023 · updated 2026-10-29
_Steps derived from spec 0023 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## Commands
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_compare.py tests/test_reconcile_plan.py` → every finding kind and every planned action passes → AC-6 to AC-12, AC-23, AC-28
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_node_reconcile.py tests/test_node_reconcile_actions.py tests/test_kill_switch.py` → actions run in the fixed order, the replacement stop comes before any cancel, the quiet compare and the confirming fetch hold → AC-7 to AC-13, AC-27
- [ ] `docker compose run --rm -T migrate pytest -q -m integration tests/integration/test_reconcile_queries.py` → the newest recorded stop of the opening order gives the trigger price → AC-9 (Postgres fallback)
- [ ] `docker compose run --rm --no-deps -T trader pytest -q tests/test_smoke.py` → live loads `ReconcileExecutor`, demo does not → AC-7, AC-23
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_recover.py tests/test_node_reconcile_recover.py` → fills match by venue id, else client id; held trade ids are skipped; a full page inside the window is refused; through a real `LiveExecutionEngine` the fills land on our order with Kraken's price, time, and trade id, ending `FILLED` or `CANCELED` with the filled quantity → AC-11
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_untrusted.py tests/test_reconcile_executor.py tests/test_kill_switch.py` → the untrusted set goes to memory before Redis; the residual close sends Kraken's fresh quantity reduce only with no position id, nothing when flat; the flatten skips the strategy's own close and the wait for flat reads Kraken → AC-8, AC-9
- [ ] `docker compose run --rm --no-deps -T trader pytest -q tests/test_cache_reset.py` → `trader.cache reset` deletes only `trader-<id>:*` and the untrusted set, and refuses while the heartbeat snapshot is fresh → AC-8, AC-26

## UI / manual (laptop demo, after the manual promote)
- [ ] Start the trader in demo with a `donchian_scalp` position open → periodic checks stay `clean`; the position's own stop counts as its stop → AC-6, AC-9, AC-23
- [ ] In demo, place an order on the sandbox the cache does not hold (or wait for a real mismatch) → one `reconciliation_mismatches` row with `action = none`, one critical push, entries not blocked by it → AC-13, AC-23
- [ ] Logs show `reconcile_not_quiet` only while orders are in flight, never a finding caused by a fill landing mid fetch → AC-27

## Live drill (task 9, VPS, `LIVE_MODE=drill`)
- [ ] D4: `trader.drill foreign-order` → `unknown_order`, cancelled on Kraken within 60 s, critical push → AC-10, AC-13
- [ ] D6: `trader.drill cancel-stop`, then restart → `missing_stop`, the stop re placed at the recorded trigger price → AC-9
- [ ] D7: `trader.drill foreign-position` → `unknown_position`, `cache_fix: synced`, closed by `ReconcileExecutor`, halt `reconcile_mismatch`, Resume needed → AC-7
- [ ] D12: half size stop → full size stop placed first, then the half one cancelled, clean on the next check → AC-9
- [ ] Stop the trader with a position and its stop open, let the stop trigger on Kraken, start again → `local_only_order` adopted with `cache_fix: recovered`, the position closed in the cache, no `missing_stop`, no halt → AC-11
- [ ] With Kraken flat and the cache still holding a position no fill explains → `position_mismatch` with `cache_fix: manual`, the instrument in `live:reconcile:cache_untrusted`, halt `reconcile_mismatch`, the flatten finishes without timing out; then stop the trader, wait 30 s, run `docker compose run --rm trader python -m trader.cache reset` (it refuses while the trader runs), start, and the start check is clean → AC-8, AC-26

## Value sourcing
- [ ] Which fills are ours: a position whose fills include an `EXTERNAL` or `RECONCILIATION`/`VENUE` order → `position_mismatch` with `fills_not_ours`, never adopted → AC-6
- [ ] Order matching: Kraken reports its own client id for our order → still matched on the venue id, no finding → AC-10, AC-11
- [ ] Quantity matching: sizes differing below the size increment → clean; one increment apart → `position_mismatch` → AC-6 to AC-8
- [ ] Trigger price: the cached stop of the opening order wins; with the cache lost, the newest `orders.trigger_price`; with neither → flatten and halt (`no_trigger_price`) → AC-9
- [ ] Stop quantity: the replacement is the Kraken position quantity → AC-9
- [ ] Already crossed: long with mid ≤ trigger, or short with mid ≥ trigger → `closed_at_market`, no halt; no quote younger than `RISK_STALE_QUOTE_S` → waits that long, then `no_fresh_quote`, flatten and halt → AC-9
- [ ] Recovered fill: Kraken's `FillReport` price, venue time, and trade id as returned; a trade id the cache holds is skipped → AC-11
- [ ] Residual close: Kraken's quantity and side from a fresh fetch right before each attempt, reduce only market, nothing when flat → AC-8
- [ ] Stop check: `uses_stop` of the position's strategy; `BuyAndHold`, `RandomTaker`, and the `ReconcileExecutor` need none → AC-9
- [ ] Wait for flat: an untrusted instrument is flat only when a fresh Kraken fetch says so; an error counts as not flat → AC-8

## Acceptance-criteria coverage
- AC-6 compare tests, `local_view` test, value sourcing · AC-7 plan, actions, D7 · AC-8 plan, actions, untrusted, executor, kill switch, reset, drill step · AC-9 plan, actions, integration, D6, D12 · AC-10 compare, actions, D4 · AC-11 recover, plan, actions, real engine recover tests, drill step · AC-12 compare, plan · AC-13 actions, demo step · AC-23 plan, smoke, demo step · AC-27 actions, demo step · AC-26 reset tests, drill step · AC-28 plan, actions
