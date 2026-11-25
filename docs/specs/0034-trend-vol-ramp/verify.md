# 0034 · trend_vol_ramp: how to verify it

These are the steps you can run to check each acceptance criterion. Steps 1 and 2 run on the laptop. Steps 3 to 6 need the VPS demo after task 6 (the switch).

## On the laptop

1. **Unit and node tests (AC-1 to AC-5, AC-7, AC-9).** Rebuild, then run the new core tests. All 67 should pass.
   ```bash
   docker compose build
   docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q \
     tests/test_strategy_trend_vol_ramp.py tests/test_node_trend_vol_ramp.py tests/test_trend_vol_ramp_closure.py
   ```
   The closure test proves no other strategy's hash moved (AC-9). The trader test `test_its_version_is_not_trend_vols` shows the new version is distinct.

2. **Restart query (AC-6) and dashboard words (AC-7).** `tests/integration/test_latest_decisions_by_coin.py` covers the new query on a throwaway Postgres, and `apps/dashboard/tests/test_portfolio.py` covers the `Building` and `Stopped` words.
   ```bash
   docker compose run --rm -T migrate pytest -q -m integration tests/integration/test_latest_decisions_by_coin.py
   docker compose run --rm --no-deps -T dashboard pytest -q tests/test_portfolio.py
   ```

## On the VPS demo (after task 6)

3. **First daily run (AC-3, AC-10).** At the first 00:05 UTC run (01:05 Amsterdam), the trader log should show `ramp_target` for all 12 coins, each working target about a third of its full target. The dashboard plan view should read `Building 33%` for each coin with a target.

4. **No entry at start (AC-3).** Before that first daily run, restart the trader once. The book should stay flat until 00:05 UTC, with no new entry at start.

5. **Hourly stop check (AC-4, AC-10).** Over the first day, the log should show no `ramp_stop_check_failed`.

6. **A stop when one fires (AC-4, AC-5, AC-8).** Look for three things: a `stop_vol` decision row, a `ramp_stop_hit` alert, and the coin reading `Stopped (long)` or `Stopped (short)` on the dashboard until its signal turns. A restart in between should log `ramp_stop_restored` and keep the coin at 0 (AC-6).
