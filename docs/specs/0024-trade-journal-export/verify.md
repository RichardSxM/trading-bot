# Verify: trade journal export · spec 0024 · updated 2026-10-31
_Steps derived from spec 0024 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## VPS check (task 9)
Ran 2026-10-31 17:41 UTC (18:41 Amsterdam) on the VPS at 2b7b728 (deployed 17:39 UTC, no promote, `kfs.env` unchanged). The export ran through its own read paths (`dashboard.data.journal.read_summary` and `open_journal`, the functions behind the panel, `summary.csv`, and `journal.csv`), as `dashboard_app` inside the running dashboard container. Range `2026-10-30` to `2026-10-30`, mode `paper`, env `demo`: the only full Amsterdam day since the 0f12982 deploy (2026-10-30) that ends before today.

- [x] Alembic at `0013_alerter_fx_rates`. On first start the alerter logged `fx_fetched` with count 213 and newest `2026-10-30`; `fx_rates` holds 213 rows, `2026-01-02` to `2026-10-30` (2026-01-01 is a TARGET holiday, so no rate) → AC-15
- [x] All 38 journal rows have a rate (12 fill, 12 fee, 14 funding; the first fill, 2026-10-29 23:40 UTC, uses the 2026-10-29 rate 1.113). The end bound (2026-10-30 23:00 UTC) is on the newest `rate_date`, so nothing is refused → AC-5 (the refusal cases, a missing rate and a date after the newest rate, are proven by the integration tests, not on live data)
- [x] Check: `match`. journal net 4.36558045 USD, dashboard net 4.36228045 USD, difference 0.0033 within the 0.06 tolerance (6 positions counted), 0 crossing an edge, 0 unlinked rows → AC-9
- [x] `summary.csv` totals: trading 5.8133, fees −1.45, funding 0.00228045, net 4.36558045 USD; trading 6.01, fees −1.33, funding −0.01, net 4.67 EUR. Sums of `journal.csv` (38 rows): `amount` 4.36558045 USD, `eur_amount` 4.67 EUR. Both equal the summary's net → AC-11
- [x] Balances: start KRAKEN-001 1008.22 USD (905.86 EUR, captured 2026-10-29 22:59:51 UTC), end 998.29 USD (893.56 EUR, captured 2026-10-30 22:59:54 UTC) → AC-7. Open question: the balance fell 9.93 USD while the paper net was +4.37 USD. In paper mode the fills are simulated and the balance is the Kraken demo wallet, so the two need not agree, but this is worth a look.
