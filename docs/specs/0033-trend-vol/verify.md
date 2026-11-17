# 0033 · Verify notes

## AC-9a fetch figures (2026-11-16)

The three coins the first fetch refused (2026-11-15), refetched after the AC-9a edges amendment (014828e), with `fetch-candles 2022-03 2026-10 --instrument PF_<COIN>USD.KRAKEN` on the laptop lab. Every month the fetch wrote came back `complete`, and no fetch logged a bad row. Months before a coin's listing are skipped as empty. Figures are the sums of the `lab_candles_month_done` lines over the coin's months.

| Coin | Months | Bar rows | Gap rows (missing minutes) | Price rounded rows | Volume rounded rows | History tick months | Dropped span |
|---|---|---|---|---|---|---|---|
| DOGE | 53 (2022-06 to 2026-10) | 2,295,852 | 1 (28,308) | 8,455 | 307 | 51 (to 2026-08), tick 0.000001 | none |
| TRX | 53 (2022-06 to 2026-10) | 2,295,840 | 1 (28,320) | 1,461 | 41 | 51 (to 2026-08), tick 0.000001 | none |
| QNT | 39 (2023-08 to 2026-10) | 1,710,699 | 1 (21) | 394 | 3,067 | none | 2023-08-17 21:44 to 22:05 UTC, 21 raw candles removed |

- DOGE and TRX: every month up to 2026-08 builds on the history tick (`lab_candles_tick_history`), and 2026-08 straddles the 07:22 UTC change with no bad row. Months from 2026-09 build on the pinned tick 0.00001.
- QNT: the dropped span is the only gap in its history (`lab_candles_span_dropped`, removed 21), so 2023-08 has 44,619 bar rows, 21 missing minutes, and still counts as `complete`.
- The other nine coins were not fetched again; a coin with neither pin key builds exactly as before (AC-9a).

## AC-9 first 12 coin book replay (2026-11-16)

`lab book replay --strategy trend_vol` with the defaults (cut hour 0, band 0.3, book vol target 0.5, lookback 30, cost 0.0007 a side). The first day every coin is ready is 2025-05-24, so the window is 526 days. Report `trend_vol_h00_band0.3_2025-05-24_2026-10-31.json`.

| Period | First | Last | Days | Multiple | Sharpe | Sortino | Calmar | Max DD | Gross | Closed | BTC corr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| full | 2025-05-24 | 2026-10-31 | 526 | 0.887 | 0.12 | 0.17 | -0.15 | 0.538 | 2.10 | 563 | -0.15 |
| half 1 | 2025-05-24 | 2026-02-10 | 263 | 0.769 | -0.34 | -0.47 | -0.58 | 0.523 | 1.95 | 290 | -0.29 |
| half 2 | 2026-02-11 | 2026-10-31 | 263 | 1.152 | 0.63 | 1.00 | 0.94 | 0.230 | 2.25 | 273 | 0.02 |
| 2025 | 2025-05-24 | 2025-12-31 | 222 | 1.254 | 0.99 | 1.44 | 1.56 | 0.289 | 1.92 | 207 | -0.16 |
| 2026 | 2026-01-01 | 2026-10-31 | 304 | 0.707 | -0.44 | -0.65 | -0.65 | 0.522 | 2.23 | 356 | -0.15 |

Totals: 1,862 trades, 4,450 band skips, 0 small, 0 close missing, 0 no fill, 0 unplanned days; fees 2,464.06, funding −154.91, 178 days with a missing funding rate. This is a tool check (AC-9), not a study result; the study and gate are tasks 7 to 9.

## AC-10a parity runs (2026-11-17)

`lab book parity --band 0.3` and `--band 0`, window 2024-01-01 to 2024-06-30 (182 days), the 12 coin set, equity 10,000, cost 0.0007 a side. The pure replay is the reference; the independent copy (`apps/lab/crosscheck/book_independent.py`, no `trading_core` import) and the Nautilus book mode are each compared to it. Reports `parity_trend_vol_band0.3_2024-01-01_2024-06-30.json` and `parity_trend_vol_band0_2024-01-01_2024-06-30.json`.

| Band | Engine | Verdict | Worst day | Pure return | Engine return | Cumulative gap | Side misses | Wall time |
|---|---|---|---|---|---|---|---|---|
| 0.3 | independent | PASS | 0.00 bps | +65.5536% | +65.5536% | 0.000 pp | 0 | not timed |
| 0.3 | Nautilus | PASS | 1.63 bps (2024-02-10) | +65.5536% | +64.8398% | 0.714 pp | 0 | 1,175 s |
| 0 | independent | PASS | 0.00 bps | +55.8307% | +55.8307% | 0.000 pp | 0 | not timed |
| 0 | Nautilus | PASS | 1.82 bps (2024-06-05) | +55.8307% | +55.1574% | 0.673 pp | 0 | 1,040 s |

Nautilus figures: band 0.3 had 785 decisions, 1,078 fills, funding −536.17; band 0 had 2,163 decisions, 2,560 fills, funding −542.08.

- **First run failed (same day, before the fix).** Nautilus missed the pass rule on both bands: 39.42 bps (band 0.3) and 41.99 bps (band 0), both on 2024-05-21, with a 1.399 pp cumulative gap at band 0.3. The independent copy passed exactly. Cause: the book mode added funding to the daily marks after the fact but never booked it into the engine account, so the strategy sized from a balance about 540 too high, and by May its book was 4 to 5% larger than the pure replay's. Fix: `FundingModule` in `apps/lab/src/lab/book_mode.py`, a Nautilus simulation module that books `-qty × mid × rate` into the account at each charge time (the last quote before it, rounded to the cent on a running total). The book mode test now uses a high funding rate, which fails on the old code (20.5 bps) and passes on the fix. The strategy and the pure replay did not change, so this is the same single trial already in the `rationale.md` ledger; no ledger change.
- **Funding coverage in the window.** Only BTC, ETH, SOL, and XRP have funding rates in 2024; ADA, DOGE, LINK, QNT, TRX, UNI, and ZEC have none before 2025-11-12. Every engine skips a missing rate the same way, so the comparison is like for like, but this window understates funding for those seven coins. HYPE has no candles before 2025-01, so 11 coins traded here.
- **Small steady drift.** The Nautilus book ends about 0.7 pp below the pure replay on both bands (about 0.4 bps a day, always the same sign), well inside the pass rule. The cause is not yet traced; worth a look before any longer Nautilus window.
