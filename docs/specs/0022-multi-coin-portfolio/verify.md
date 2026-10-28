# Verify: multi coin portfolio · spec 0022 · updated 2026-10-28

## AC-1 hash baseline (2026-10-28)

`code_hash(code_closure(m, root))` for every `trading_core.node` module (32) on origin/main bed1415, the commit `feat/multi-coin-portfolio` starts from, run through `scripts/spikes/hash_check.py` in `kfs/lab:dev` against a `git archive` of main's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `9c8c2a41d51b414ce8f21c140972c3ae3abc217a7fb2f8df88a0b724becd76b3`. The close out (AC-18) runs the same list on the branch: it must match line for line, with no module added or edited.

## AC-2 and AC-2a ETH data (2026-10-28)

`lab fetch-candles 2022-03 2026-10 --instrument PF_ETHUSD.KRAKEN` on the laptop (`--no-deps`, the AC-2a build of `feat/multi-coin-portfolio`) exited 0: 39 months fetched (2023-08 to 2026-10), 17 skipped as already complete (2022-03 to 2023-07, stored before AC-2a), 2026-10 stored as incomplete up to 2026-10-28 03:31 UTC (04:31 Amsterdam). Funding came from the import of 2026-10-28 (27,827 export rows) plus the API for the newest charges.

- **First candle minute**: 2022-03-23 10:05 UTC (BTC: 10:04). The one gap row is the time before listing, 2022-03-01 00:00 to 10:04 (32,284 minutes).
- **First whole month**: 2022-04, the same as BTC, so the first month both coins share is **2022-04** (AC-15's `from = 2022-04-01` stands).
- **Per year** (missing minutes / gap rows / missing funding charges): 2022: 32,284 / 1 / 0 (all before listing); 2023: 0 / 0 / 0; 2024: 0 / 0 / 0; 2025: 0 / 0 / 7; 2026 to date: 0 / 0 / 3. The missing charges fall in the same months and counts as BTC's (2025-07 1, 2025-08 1, 2025-11 4, 2025-12 1, 2026-02 2, 2026-05 1), so they are gaps in Kraken's funding history, not in the import.
- **`margin_maint`**: the pin's `0.005` matches Kraken's published first tier `maintenanceMargin` 0.005 (initial 0.01, tier up to 500,000 USD) from `/derivatives/api/v3/instruments` on 2026-10-28; `tickSize` 0.1 matches the pin's tick.

**Off tick roundings (AC-2a)**: the only off tick opens and closes are the two cases the spec names, stored as it says:

- 2023-08-17 22:55 close `1660.691517005051` stored 1660.6 (clamped to the stored high), and 22:56 open of the same raw value stored 1660.7.
- 2023-10-16 13:30 close `1617.853075194912` stored 1617.9, and 13:31 open (also its low) stored 1617.9.

So the most in one month is 2, far under `OFF_TICK_BODY_MAX` (10). The other real roundings are highs and lows, as spec 0014 AC-1 already handles: 2023-08 5, 2023-09 3, 2023-10 8, 2023-11 2. `price_rounded_rows` per month: 2023-08 6, 2023-09 3, 2023-10 9, 2023-11 2. Later months (2024-03 26, 2024-04 1, 2024-05 22, 2024-06 3, 2024-11 2, 2024-12 5, 2025-10 6, 2026-01 2, 2026-06 3) are noise snaps only (within `1e-8`), with no rounding listed.

**BTC unchanged**: no file under the BTC tree (`candles/data`, `candles/gaps`, `candles/manifests` at the root) was written on 2026-10-28. The new rule only changes a candle with an open or close off the tick, which before AC-2a was a bad row that stopped the month. Every stored BTC month built with none, so their rows stay byte identical, and none is fetched again.
