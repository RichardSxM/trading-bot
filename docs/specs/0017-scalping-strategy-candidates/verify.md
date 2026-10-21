# Verify: scalping strategy candidates · spec 0017 · updated 2026-10-21
_Results and verify steps for spec 0017. The audit record below was written before any candidate study ran (AC-3)._

## Audit record (AC-3)

- **Batch folder**: `lab-reports/audit-20261021/` (`audit.json`, `audit.md`), generated 2026-10-21T15:34:17Z over the 43 development days 2022-04-01 to 2025-10-01.
- **Pooled spread p95**: 6.2615 bps. Every child's replay run file sets `max_spread_bps = "6.27"` (rounded up to 2 places), never changed afterwards.
- **Median absolute 5 minute move**: 5.8884 bps (1 minute 2.4704, 15 minute 10.2615).
- **`reachable`**: `maker_entry` (7 bps, needs 10.5) false; `taker_entry` (10 bps, needs 15) false.
- **Run order**: all three candidates enter maker, so the flag warns about all of them and moves none (Order rule, step 2). The order stands: the four candle screens (`bollinger_rsi_screen`, `bollinger_rsi_screen_adx`, `vwap_retest_screen`, `vwap_retest_screen_adx`) first; then replay studies one at a time, `bollinger_rsi`, then `vwap_retest`, each only if its screen passed; then `delta_divergence` once its AC-17 pre check passes.

Warning, in plain words: the typical 5 minute move is smaller than the 10.5 bps a maker round trip needs to clear 1.5 times its cost. Only 28.5 % of 5 minute moves and 49.2 % of 15 minute moves reach 10.5 bps. A candidate has to pick its moments well to pass; the reward floor (`min_target_bps`) already refuses targets closer than that.

Notes for later (no effect on the order or the cap):

- The pooled p95 is driven by the thin 2022 days (day p95 up to 16.2 bps). Since 2023-10 every day's p95 is under 2 bps, so the 6.27 cap mostly binds on 2022 days.
- The per day mean absolute funding rate jumps by about five orders of magnitude between 2022-09-01 (0.24) and 2022-10-01 (0.0000055). This looks like a unit change in the recorded funding feed, not the market. No gate reads this column; check it before any later work uses funding.
