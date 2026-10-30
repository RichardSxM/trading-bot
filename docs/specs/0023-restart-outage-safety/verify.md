# Verify: restart & outage safety · spec 0023 · updated 2026-10-30
_Steps derived from spec 0023 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## Commands
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_compare.py tests/test_reconcile_plan.py` → every finding kind and every planned action passes → AC-6 to AC-12, AC-23, AC-28
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_node_reconcile.py tests/test_node_reconcile_actions.py tests/test_kill_switch.py` → actions run in the fixed order, the replacement stop comes before any cancel, the quiet compare and the confirming fetch hold → AC-7 to AC-13, AC-27
- [ ] `docker compose run --rm -T migrate pytest -q -m integration tests/integration/test_reconcile_queries.py` → the newest recorded stop of the opening order gives the trigger price → AC-9 (Postgres fallback)
- [ ] `docker compose run --rm --no-deps -T trader pytest -q tests/test_smoke.py` → live loads `ReconcileExecutor`, demo does not → AC-7, AC-23
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_recover.py tests/test_node_reconcile_recover.py` → fills match by venue id, else client id; held trade ids are skipped; a full page inside the window is refused; through a real `LiveExecutionEngine` the fills land on our order with Kraken's price, time, and trade id, ending `FILLED` or `CANCELED` with the filled quantity → AC-11
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_untrusted.py tests/test_reconcile_executor.py tests/test_kill_switch.py` → the untrusted set goes to memory before Redis; the residual close sends Kraken's fresh quantity reduce only with no position id, nothing when flat; the flatten skips the strategy's own close and the wait for flat reads Kraken → AC-8, AC-9
- [ ] `docker compose run --rm --no-deps -T trader pytest -q tests/test_cache_reset.py` → `trader.cache reset` deletes only `trader-<id>:*` and the untrusted set, and refuses while the heartbeat snapshot is fresh → AC-8, AC-26
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_unresolved.py tests/test_node_reconcile_resume.py` → an entry with no answer from Kraken blocks entries with `order_unresolved` until answered; one the in flight check rejected (`UNKNOWN`, `ORDER_NOT_FOUND_AT_VENUE`) holds until the next clean check; a clean check that lifts `reconciling` hands its finish time to every strategy; `resume_position` runs once per open position on the first clean check, and a resume that raises flattens and halts → AC-14, AC-15, AC-16
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_node_resume.py` → every strategy base ignores a signal older than the check, keeps the resting stop on resume, rebuilds its exit timers from `ts_opened`, exits at once when the hold is over, cancels resting entries; `BuyAndHold` keeps holding, `RandomTaker` uses its timed exit, the `ReconcileExecutor` closes → AC-15, AC-16

- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_reconcile_intent.py tests/test_node_reconcile_intent.py` → an entry is `entry_submitted` and a reduce only exit `flatten_submitted` until its terminal event, a protective stop changes nothing, a stop fix shows `stop_replacing` while it runs, then `hold` → AC-18
- [ ] `docker compose run --rm --no-deps -T dashboard pytest -q tests/test_sides.py tests/test_reconciliations.py tests/test_reconcile_badge.py tests/test_styleguide.py` → the card shows Wanted, Trader, Kraken; a side or size difference is `disagree`; a missing, `unknown`, or older than 3 intervals exchange view reads `Kraken unknown`, never flat; the Reconciliation page lists checks newest first with their findings, 404 past the last page; the badge links to it → AC-18, AC-19
- [ ] `docker compose run --rm -T migrate pytest -q -m integration tests/integration/test_reconciliations_page.py` → checks newest first, findings in written order, 50 per page, a failed check keeps its error and null counts → AC-19
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_alerts_checks.py` → `reconciling`, `exchange_unknown`, and `order_unresolved` are in the stuck entry block set and push a warning after the usual threshold → AC-21
- [ ] `docker compose run --rm -T dashboard pytest -q -m redis` → the `dashboard` Redis user reads `*:snapshot:*`, so `{env}:snapshot:exchange` too → AC-18 (security model)
## UI / manual (laptop demo, after the manual promote)
- [ ] Start the trader in demo with a `donchian_scalp` position open → periodic checks stay `clean`; the position's own stop counts as its stop → AC-6, AC-9, AC-23
- [ ] In demo, place an order on the sandbox the cache does not hold (or wait for a real mismatch) → one `reconciliation_mismatches` row with `action = none`, one critical push, entries not blocked by it → AC-13, AC-23
- [ ] Restart the trader in demo while flat → the start check is clean, no `position_resumed` (the sandbox restores no position, spec 0006), entries open again → AC-16 (resume itself is proven in D2 and D5)
- [ ] After that restart, no `ENTER` decision from a bar that closed before the start check finished (`entry_skipped_stale_signal` if one arrives) → AC-15
- [ ] Logs show `reconcile_not_quiet` only while orders are in flight, never a finding caused by a fill landing mid fetch → AC-27

- [ ] Overview with a demo position open → the card's top reads Wanted `hold`, Trader and Kraken the same side and size, Kraken with its age under 60 s → AC-18
- [ ] Stop the trader for 3 minutes (or block its Redis writes) → Kraken reads `Kraken unknown` in red once the exchange snapshot is older than 180 s or gone, never `flat` → AC-18
- [ ] Click the `kraken check` badge → the Reconciliation page lists the start check and any reconnect check, newest first, each finding with its action → AC-19
## Live drill (task 9, VPS, `LIVE_MODE=drill`)
- [ ] D2: resting entry across a kill → start check clean, the resume cancels it, one order ever on Kraken → AC-16
- [ ] D3: `rest_order_and_die` → exactly one order on Kraken after the restart, `order_unresolved` until it resolves, no duplicate → AC-14, AC-15
- [ ] D5: known position across a kill → adopted, stop unchanged, clean, `position_resumed` → AC-16
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
- [ ] Unresolved entries: the cache's in flight entry orders at start plus entries the in flight check rejected since the last clean check; reduce only orders never count → AC-14
- [ ] Check finish time: `reconciliations.finished_ns` of the clean check, compared with the bar's or quote's `ts_event` (a timer's scheduled tick) → AC-15
- [ ] Exit timers on resume: the cached position's `ts_opened` plus the strategy's own config → AC-16
- [ ] Wait for flat: an untrusted instrument is flat only when a fresh Kraken fetch says so; an error counts as not flat → AC-8

- [ ] Wanted: `trader_state.intent` for the card's instrument; a flatten sent but not filled shows `flatten submitted` while Trader and Kraken still show the position → AC-18
- [ ] Trader: `{env}:snapshot:position`; a red aged or missing one reads `unknown` and never raises a disagreement → AC-18
- [ ] Kraken and age: `{env}:snapshot:exchange`, age from `fetched_ns`; `status: unknown` reads `Kraken unknown` → AC-18
- [ ] Disagree: sizes written differently but equal (`0.0011` and `0.00110`) agree; another side or size disagrees → AC-18
- [ ] Badge and history: `trader_state.last_clean_check_ns`, `last_check_outcome`, the halt; the page reads `reconciliations` joined to `reconciliation_mismatches` → AC-19
## Acceptance-criteria coverage
- AC-6 compare tests, `local_view` test, value sourcing · AC-7 plan, actions, D7 · AC-8 plan, actions, untrusted, executor, kill switch, reset, drill step · AC-9 plan, actions, integration, D6, D12 · AC-10 compare, actions, D4 · AC-11 recover, plan, actions, real engine recover tests, drill step · AC-12 compare, plan · AC-13 actions, demo step · AC-23 plan, smoke, demo step · AC-27 actions, demo step · AC-26 reset tests, drill step · AC-28 plan, actions · AC-14 unresolved, actor resume tests, D3 · AC-15 actor and base resume tests, demo step, D3 · AC-16 actor and base resume tests, D2, D5 · AC-18 intent, sides, card render, demo step · AC-19 badge, history page, integration query, demo step · AC-21 alert checks
