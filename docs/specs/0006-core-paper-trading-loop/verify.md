# 0006. Verify: core paper trading loop

Steps you may run to check the feature by hand, on the laptop in demo, after `docker compose build` and `docker compose up -d`. Each names the acceptance criteria it checks. Run them in order; later steps assume the earlier ones passed.

1. **Gate spike (AC-1).** Run the spike from build task 1 and check its four points:
   - the market entry fills within the bid and ask at submit
   - the reduce only stop market fills when the quote crosses its trigger
   - a reduce only market order sent while flat is denied or cancelled, not filled
   - the trader's Redis login can `SCAN` and `UNLINK` `trader-TRADER-DEMO:*`

   Write down here how fees reach the fills: through the sandbox config, or through the instrument.
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

## Notes

_Results and dates go here as steps are run._
