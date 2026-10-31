# 0024. Trade journal export: decision record

## Context

Feature 18 owes you a CSV of every fill, fee, and funding payment for any date range, for your own analysis and your Dutch tax records. Its totals have to match the dashboard's P&L. Spec 0002 already built the read side: `journal_rows` turns fills, fees, and funding into signed cash flows with a EUR amount from `fx_rates`. Spec 0002 also left three things to this feature: something that fills `fx_rates` (nothing writes it today, so every EUR amount is missing), the rounding of EUR amounts in the file, and checking the journal sums against `positions.realized_pnl`.

The forces at play:
- **The dashboard reads only.** It reads Postgres and Redis and writes only to the `control` stream. It sits behind a session login on Tailscale, reachable from your phone or laptop.
- **Tax records live in Amsterdam time.** The tax year is a Dutch calendar year, while every stored time is UTC.
- **The file has to be complete or nothing.** A tax file that's quietly missing EUR amounts is worse than no file.
- **The data is small but grows.** Demo paper trading makes a few fills a day, while a scalper live could make thousands. A year's export must not load everything into the dashboard's limited memory.
- **No new no default settings.** Every new no default compose setting has to be added to `/etc/kfs/kfs.env` by hand, or the deploy rolls back.
- **No floats for money,** and pure logic lives in `packages/core`.

## Options considered

### Option 1: Dashboard export over an extended `journal_rows`, rates fetched by the alerter (chosen)

A form, panel, and two streamed CSV endpoints on the Trades page, with pure core formatting. The alerter, which already runs typed daily loops and makes outbound HTTPS calls, fetches the ECB rate.

**Pros**: no new service or secret; reachable from any device; the dashboard keeps reading only; the stale rate alert comes from the process that already sends alerts.
**Cons**: the alerter takes on a data job; streaming with a refusal check needs two passes.

### Option 2: A command on the VPS

`kfs-compose run --rm dashboard journal-export ...` writes the CSV to a file.

**Pros**: no UI work; simplest to test.
**Cons**: you need ssh and then have to copy the file off the VPS; there's no panel, so the match check is buried in a log.

### Option 3: The nightly backup job fetches the rates

Add curl plus psql to `docker/backup/run.sh`.

**Pros**: already runs nightly.
**Cons**: Alpine shell with no typed tests; Decimal parsing in shell is fragile; it can't raise an alert itself.

### Option 4: A new one shot fx container on a host timer

**Pros**: the cleanest separation.
**Cons**: one more image, role, systemd timer, and health check to deploy and watch, for one HTTP call a day.

## Rationale

You chose the dashboard download, the alerter, and the ECB Data Portal API. Each one reuses something that already runs. The dashboard already has the session, the design system, and the `dashboard_app` read path. The alerter already has supervised loops, httpx, and the push channel the stale warning needs. And the Data Portal takes a start date, so one call backfills any gap.

A few smaller calls were made in the spec:
- **Refusing on a missing rate** follows the fail closed rule, applied to a tax record.
- **Rounding per row** means the EUR column adds up in a spreadsheet, at the cost of a cent or so against exact math over a year. That's the trade most accountants expect.
- **The check's `not_comparable` state** is honest. The journal counts cash at fill time, while the dashboard counts P&L at position close, so the two only agree when every position in the range opened and closed inside it.
- **The tolerance of one cent per position** covers Nautilus rounding each position's realized P&L to the currency's precision. The runner up, an exact match, would fail on every real range.
- **One mode per file, and a range that ends yesterday at the latest** came out of the cross check. Mixing paper and real money gave a meaningless tax total. An open day's rows could change their EUR amount once that day's rate was published.
- **Monthly chunks with a validate pass** keep memory flat and still allow a 409 before streaming starts. The runner up, building the file in memory, is simpler but grows with the range.

## Amendments

- **2026-10-31, AC-15 empty 200.** The first `/check verify` found `fx_fetch_failed` (parse, missing column) logged every hour on a Saturday. The ECB had answered 200 `text/csv` with an empty body, not the 404 AC-15 expected. AC-15, the alerter request line, and the fetcher test line now treat a blank or whitespace only 200 body like a 404: zero rows, nothing logged. A 200 with a body that fails to parse still logs `fx_fetch_failed`, so a real format change keeps failing closed. Built in 2a0dbc9.
