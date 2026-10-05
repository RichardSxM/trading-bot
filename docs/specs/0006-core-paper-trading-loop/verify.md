# 0006. Verify: core paper trading loop

Steps you may run to check the feature by hand, on the laptop in demo, after `docker compose build` and `docker compose up -d`. Each names the acceptance criteria it checks. Run them in order; later steps assume the earlier ones passed.

1. **Gate spike (AC-1).** Run the spike from build task 1 and check its four points:
   - the market entry fills within the bid and ask at submit
   - the reduce only stop market fills when the quote crosses its trigger
   - a reduce only market order sent while flat is denied or cancelled, not filled
   - the trader's Redis login can `SCAN` and `UNLINK` `trader-TRADER-DEMO:*`

   Write down here how fees reach the fills: through the sandbox config, or through the instrument.

   You can run it with `docker compose run --rm -T -v "$PWD/scripts/spikes:/spike" trader python /spike/0006_gate.py`. It prints `SPIKE` lines and exits 0 when every check passes.

   **Result, 2026-10-04 (all passed):**
   - entry filled at 85252, inside bid 85251 and ask 85252
   - the stop filled at its trigger, 85249
   - the reduce only market order sent while flat was rejected: "REDUCE_ONLY MARKET SELL order would have increased position"
   - `SCAN` found the probe key and `UNLINK` removed it

   **How fees work:** they come **through the instrument**.
   - `SandboxExecutionClientConfig` has no fee field. The sandbox always uses `MakerTakerFeeModel`, which reads `maker_fee` and `taker_fee` from the instrument in its matching engine.
   - Kraken publishes `PF_XBTUSD` with both fees at 0. With the instrument patched to 0.0005 taker, a 0.002 BTC fill costs 0.09 USD, and the patch still holds 90 s later.
   - The sandbox calls `update_instrument` on every instrument it receives. So the build patches the fees inside the sandbox client on every instrument it gets, so a refresh from Kraken cannot reset them to 0.
   - Note: commission rounds to USD precision (0.01). Below about 20 USD of notional, a 0.05 % fee shows as 0.00.
2. **Startup (AC-2, AC-11, AC-15).** `docker compose logs trader` should show the sandbox client, `placeholder_timed` running, and no env guard refusal. In Postgres, check for:
   - one `paper` `strategy_runs` row in `running`
   - a `strategy_versions` row whose config holds the settings
   - the instrument row

   `redis-cli --scan --pattern 'demo:*'` should still show the pause key (if set) and the control stream.
3. **First round trip (AC-3, AC-4, AC-6, AC-8, AC-9, AC-10).** About 2 s after the next quarter hour, check for each of these in turn:
   - an approved `enter` decision
   - a market entry tagged `decision:<id>`
   - a fill with a non zero fee
   - a reduce only `stop_market` tagged `parent:<entry id>`, with `trigger_price` 0.3 % away

   Ten minutes later, check for an `exit` decision, a reduce only market close, and then the stop `canceled` after the position closed.
4. **Dashboard (AC-16, AC-17, AC-19).** During the hold, the Overview card shows side, size, entry, mark, stop, and unrealized P&L, updating every second. After the close:
   - the Trades page lists the round trip with exit reason `time`
   - the realized figures match `SUM(realized_pnl)` over `paper` runs in Postgres

   Then stop the trader: the card shows "trader offline" within the staleness limit.
5. **Risk refusal (AC-5).** Restart with `PLACEHOLDER_NOTIONAL_USD=300` (expect a start WARNING). The next boundary should write a rejected decision with reason `notional_cap`, a `limit_hit` warning, and no order. Restore the setting.
6. **Stop failure (AC-7, AC-8a).** Restart with `SANDBOX_REJECT_STOP_ORDERS=true`. After the entry fills:
   - the exit loop closes the position within about 5 s
   - `entries_blocked` shows `stop_not_accepted`
   - a critical `order_rejected` event exists
   - the Trades page shows `stop_failure`

   Restore the setting and restart.
7. **Pause (AC-20).** Pause from the dashboard during a hold. The stop and the time exit still work, and the next boundary records a rejected decision `paused`. Resume, and the boundary after that trades.
8. **Postgres outage (AC-12).** During a hold, run `docker compose stop postgres`.
   - The time exit still closes the position.
   - After 30 s, `entries_blocked` shows `recording_unhealthy` and `recording_backlog` is above 0.

   Then run `docker compose start postgres`. The block should clear, `recording_backlog` should return to 0, and the fill count should match the `fill_seen` lines.
9. **Spill and replay (AC-13).** With Postgres stopped and a fill queued, stop the trader. A `<run_id>.jsonl` file appears in the `trader-spool` volume. Start Postgres, then the trader: the rows appear once, and the file is gone.
10. **Crash while positioned (AC-15).** During a hold, run `docker kill` on the trader container. After Docker restarts it, check for:
    - the old run marked `aborted`
    - the position `closed` with `side = flat`
    - a critical `reconcile_mismatch` event keyed `abandoned:<position_id>`
    - exit reason `abandoned` on the Trades page

    The next boundary should trade.
11. **Stale feed (AC-14, AC-18).** Cut the trader's outbound network for 20 s (for example `docker network disconnect`, then reconnect). Expect the `stale_feed` block, the top bar badge with its plain words, and recovery without a restart.
12. **Two hour run (AC-21).** Note the start time T0, leave the bot alone for 2 hours, and note T1. Expect:
    - At least 6 closed `paper` positions between T0 and T1.
    - Every stop accepted in time. This query returns no rows:
      ```sql
      SELECT e.client_order_id
      FROM orders e
      JOIN orders s ON s.run_id = e.run_id AND s.parent_client_order_id = e.client_order_id
      JOIN order_events a ON a.order_id = s.id AND a.kind = 'order_accepted'
      JOIN LATERAL (SELECT max(filled_ns) AS last_ns FROM fills f WHERE f.order_id = e.id) lf ON true
      WHERE e.reduce_only = false AND e.created_at BETWEEN :t0 AND :t1
        AND a.occurred_ns - lf.last_ns > 5000000000;
      ```
    - The `fills` count with `filled_at` between T0 and T1 equals `docker compose logs --since T0 --until T1 trader | grep -c '"fill_seen"'`.
    - Once flat with `recording_backlog` 0, the dashboard's realized figures equal the Postgres sums to the cent.

    **Result, 2026-10-05 (all passed):** window 00:30 to 02:30 UTC, inside one run untouched since 2026-10-04 20:31 UTC.
    - 8 closed `paper` positions, from 8 entries.
    - Every entry had its stop accepted; the late stop query returned no rows.
    - 17 `fills` rows and 17 `fill_seen` lines.
    - No risk events in the window.
    - At 02:46 the dashboard's reader (as `dashboard_app`) gave realized today −1.93, all time −5.20, fees today 1.10, equal to the Postgres sums. A new entry had opened at 02:44, so this was not strictly flat; both read the same closed rows.

## Notes

_Results and dates go here as steps are run._

**2026-10-04, Milestone 2 on the laptop (demo).** These are build checks, not the full walk through; `/check verify` still owes every step.
- Run `01a107db`: the 17:00 boundary skipped while warming up. At 17:15 it went short 0.0011 at 85311.00 with a stop at 85567 (accepted). The 0.05 USD fee reached the balance. The 17:25 time exit closed the position and the sandbox canceled the stop. A restart while flat finished the run as `completed`.
- Running live found three bugs the unit tests missed. All are fixed:
  - NautilusTrader 1.231 passes `portfolio` only to a factory named exactly `SandboxLiveExecClientFactory`, so `FeeSandboxExecClientFactory` takes that name. A test now builds a real node.
  - On a live node, `OrderInitialized` arrives before the order is in the cache. `order_for` rebuilds the order from the event.
  - The stale check counted from time zero before the first quote, which raised a false `stale_feed` at every start. It now counts from `on_start`.
- Run `01a107f4` (after the fixes): the 17:45 entry recorded `order_initialized` ×2, `order_submitted` ×2, `order_accepted` and `order_filled`, with no `recording_failed` and no `stale_feed`.

**2026-10-05, `/check verify` on the laptop (demo), 02:52 to 05:32 UTC. Verdict: FAIL.** Images rebuilt from 2836ec7 first (both containers were running older images). The dashboard was checked over HTTP with curl (no browser was connected), plus your own eyes for the open card and the badges.

Passed, with evidence:

- Step 1 (AC-1): the spike passed all five checks again, exit 0.
- Step 2 (AC-2, AC-11, AC-15 purge): `SANDBOX` client built, `demo_cache_purged`, a `running` `paper` run, the strategy version config, the instrument row.
- Step 3 (AC-4, AC-6, AC-8, AC-9, AC-10): 03:15 round trip, long 0.0011 at 86484, fee 0.05, stop 86224 (0.3 % rounded away) accepted 30 ms after the fill, `exit` decision `time`, reduce only close, stop canceled after `PositionClosed`, realized −0.20 net of 0.10 fees.
- Step 4 (AC-16, AC-17, AC-19): all three snapshots with 30 s TTL and the four new `trader_state` fields. Realized today −2.29, all time −5.56, fees today 1.25, equal to the Postgres sums, labeled "before funding". "Trader offline" showed when the trader's Redis writes timed out (a real outage, see below). Trades page: newest first, sides from the first fill, reasons `time`, `stop`, `stop_failure`, `abandoned`; `page=0`, `page=x`, `fills_page=-1` give 422; `page=999` shows "Nothing on page 999. Back to page 1"; no cookie gives 303 to `/login`.
- Step 5 (AC-5): with notional 300, the start WARNING, then `rejected` `notional_cap`, a `limit_hit` warning, no order.
- Step 7 (AC-18, AC-20): paused while flat, the 04:00 boundary was `rejected` `paused` (`info`), no order. Paused during the 04:30 short, the stop stayed accepted and the time exit closed it at 04:39:53. You saw the top bar badge.
- Step 8 (AC-12): Postgres stopped during a hold, `recording_unhealthy` with backlog 2 after about 17 s, cleared 7 s after Postgres came back, backlog 0. With Postgres down again, the time exit still closed the position.
- Step 9 (AC-13): trader stopped while Postgres was down, 30 items spilled to `01a10a2c….jsonl`, replayed (30) at the next start, file gone, each fill present once. Fills 6 = `fill_seen` 6 for that run.
- Step 10 (AC-15): `docker kill` during the 05:15 short. The old run `aborted`, the position `closed`, `flat`, exit at the last snapshot mark 85476.5, realized −0.05 + 0.01 = −0.04, orders `canceled`, one critical `reconcile_mismatch` `abandoned:01a10a7c…`. The 05:30 boundary traded.
- Step 11 (AC-14): network cut for 22 s, `stale_feed` block and one warning event, cleared by itself 3 s after reconnect.
- Step 12 (AC-21): rechecked in Postgres for 00:30 to 02:30: 8 closed, 8 of 8 stops accepted, the late stop query returns no rows, 17 fills, fees 0.80. The window's `fill_seen` lines were gone with the replaced container, so that half rests on the 2836ec7 record.

Failed, for `/debug`:

- **AC-3 and AC-8, timers fire early.** Every boundary decision since at least 2026-10-04 fired 1 to 10 s before the quarter hour, not 2 s after (for example 03:14:56.6, 03:44:56.9, 04:14:57.5). When flat the boundary fires twice (03:44:56.9 and 03:45:02.2 both decided), because the early fire re arms `next_boundary_ns` for the same quarter hour. The time exit also fires about 4 s early. Likely cause: the alert sleeps a fixed duration while the `clock` service steps the Docker VM clock; nothing checks the wall clock when the alert fires.
- **AC-11, filled orders recorded with the wrong status.** On a live node `OrderInitialized` reaches the recording actor after `OrderSubmitted` and `OrderFilled`, with a later `ts_event`, so the order row ends as `initialized` with `filled_qty` 0 (and the stop as `pending_cancel`). The next start's `close_abandoned` then marks those orders `canceled`. In the paper runs, 25 of 69 filled market orders read `canceled`, and the current entries read `initialized`.

Missing:

- **Step 6 (AC-7, AC-8a runtime):** `SANDBOX_REJECT_STOP_ORDERS` is in the spec but not in the code, so the drill cannot run. One real `stop_not_accepted` event from 2026-10-04 19:45 exists (an older build).

Notes for the next run:

- Step 10: `docker kill` counts as a manual stop, so `restart: unless-stopped` does not restart the trader; it was started by hand. Use `docker compose exec trader kill 1` (or similar) to imitate a crash.
- The recorder sat at its 768 MiB limit and about 280 % CPU (`data_queue at capacity`), starving the VM until the trader's Redis writes timed out. It was stopped from 04:35 to 05:31 for the drills. The trader idles near 80 % CPU, worth a look.
- The run that spilled ended `aborted`, not `completed`, because `finish_run` could not reach Postgres at stop.

**2026-10-05, `/debug` on the two failures.** Both are fixed. They still need a fresh `/check verify` for AC-3, AC-8 and AC-11.

- Timers: a Nautilus alert sleeps a fixed time on the monotonic clock. In the Docker Desktop VM that clock runs about 0.6 % faster than the wall clock, so alerts woke early. The cause was not the `clock` service. Now an alert that wakes early sleeps the rest (`_when_due`).
- Order status: `OrderInitialized` arrives first, but it is stamped by the node clock. The sandbox stamps its later events with the last quote time, which is older, so the guarded upsert dropped them. Now the row's `last_event_ns` is the newest time the order has seen (`order_last_event_ns`).
- Live check at 06:30 UTC: decided at 06:30:02.01, time exit at 06:40:02.025, orders `filled`, `canceled`, `filled`. The 25 older rows stay wrong.
