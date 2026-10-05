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
