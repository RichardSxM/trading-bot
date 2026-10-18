# Verify: candle backtest lab · spec 0014 · updated 2026-10-18
_Steps derived from spec 0014 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. Milestone 3 steps so far; later milestones append._

## Commands
- [ ] `docker compose run --rm --no-deps lab fetch-candles 2021-12 2022-04` → exit 64 before any request (month before 2022-03) → AC-1
- [ ] After a full fetch, open `catalog-lab/candles/manifests/2023-08.json` → `price_rounded_rows` 13 and `complete: true`; the log has INFO `lab_candles_price_rounded` for 2023-08 listing the low `28319.01027283563` at 2023-08-17T00:44:00Z stored as `28320` → AC-1, AC-4
- [ ] Read the 2023-08 bars and look at minute 2023-08-17 00:44 → low 28320, every price a whole number, no stored high above Kraken's raw high or low below its raw low → AC-1, AC-3
- [ ] Delete `price_rounded_rows` from one complete month's manifest, then run `fetch-candles` for that month without `--refetch` → the month is fetched again (no `lab_candles_month_skipped`) and the new manifest has the field → AC-4
- [ ] With the field deleted again, run `lab backtest` over a range touching that month → exit 2, "no committed candles for <month>; run lab fetch-candles" → AC-4, AC-6
- [ ] Serve a month whose candle has an open of `100.0000001` (fake Kraken, as in `apps/lab/tests/test_candle_fetch.py`) → exit 1, ERROR `lab_candles_bad_rows` names the open, no manifest for that month, earlier months stay → AC-3
- [ ] Serve a 429 storm → `lab_fetch_retry` waits 1, 2, 4, 8, 16 s, then ERROR `lab_fetch_failed`, exit 1, earlier months committed → AC-5
- [ ] With a small `LAB_CATALOG_CAP_GB`, run `fetch-candles` → exit 3 with ERROR `lab_catalog_full` before the month's first request → AC-5
- [ ] `--refetch` a stored month whose next month opens with quiet rows at a different price → exit 1 with bad rows scoped to the next month, the refetched month still committed → AC-3
- [ ] `range_problems` over 2022-03-24 to 2026-09-30 on the full tree → no problem → AC-6

## Value sourcing
- [ ] Price rounding: a high of `25920.8322615` → 25920 (floor), a low of `28319.01027283563` → 28320 (ceiling), `39522.99999999999` → 39523 (noise snap), an open `100.00000001` → 100, an open `100.0000001` → bad row; the tick comes from the pinned instrument (`price_increment` 1) → AC-1
- [ ] Volume rounding: `0.3565999999999999` → 0.3566, `0.12345` → bad row; the step comes from the pinned instrument (`size_increment` 0.0001) → AC-1
- [ ] `complete`: a fetch of the running month gives `complete: false` and is fetched again on the next run → AC-1

## Recorded runs
- AC-23 full fetch: one uninterrupted `fetch-candles 2022-03 2026-10` started 2026-10-18 07:45:45 UTC (image at 561abfe) and took 757 s with exit 0, under the 30 minute budget. 56 months, 2,404,662 bars, 78 MB. Rounded candles: 2023-08 13, 2023-09 5, 2023-10 8, 2023-11 5, 2023-12 2, 2024-01 2 (the last two months are noise snaps only). Every manifest has `price_rounded_rows`, and only 2026-10 is incomplete.

## Acceptance-criteria coverage
- AC-1: fetch refusal, rounding, value sourcing steps · AC-3: bad open, refetch next month check · AC-4: manifest field, outdated manifest · AC-5: retries, catalog cap · AC-6: missing month refusal, full range check · AC-23: the recorded full fetch (the backtest timings come with task 20)
