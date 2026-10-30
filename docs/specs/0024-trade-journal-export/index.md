# 0024. Export the trade journal as CSV from the Trades page, with EUR amounts from a daily ECB rate fetch

**Date**: 2026-10-30
**Status**: In Progress

## Summary

You get an "Export journal" section on the dashboard's Trades page. You pick a date range (Amsterdam calendar days, ending yesterday at the latest) and live or paper, then click Check. You see the totals, your account balance at the start and end, and whether the numbers match the dashboard's P&L. From there you download two files: `journal.csv` (every fill, fee, and funding payment, each with a EUR amount) and `summary.csv` (the same totals and checks, for your records). The EUR amounts come from the ECB's daily USD per EUR rate, which the alerter now checks for every hour. If any rate is missing, the export refuses rather than hand you an incomplete tax file.

## Requirements

**User stories**:
- As the trader, I want every fill, fee, and funding payment of a date range as one CSV, so I can analyse my trading in my own tools.
- As the trader, I want each row in EUR at the official ECB rate, plus my balance at the start and end of the range, so my Dutch tax records are complete and anyone can check them.
- As the trader, I want to see that the export's totals match the dashboard P&L before I rely on it, so a recording bug can't slip into my records unnoticed.
- As the trader, I want to hear about it when the rate fetch stops working, long before tax season.

**Acceptance criteria**:
- **AC-1**: The Trades page (`/trades`) shows an "Export journal" section above its lists. It has a start date, an end date (both Amsterdam calendar dates, the end inclusive), and a mode picker (`live`, `paper`; never both in one file, so paper and real money never share a total). By default it shows 1 January to yesterday, both in yesterday's Amsterdam year, with mode `live` when the dashboard's env is `live` and `paper` otherwise. **Check** loads the summary panel (AC-7 to AC-9) into the section through HTMX, with no full page reload. Once a check passes, the panel shows two download links, `journal.csv` and `summary.csv`, carrying the same three parameters.
- **AC-2**: A range covers `[start 00:00 Europe/Amsterdam, (end + 1 day) 00:00 Europe/Amsterdam)`, converted to UTC. The range ends in the past, so its rows are settled and its rates published. It is refused (a message in the panel, 422 from the CSV endpoints, nothing exported) when a date is malformed, `start > end`, `end` is today or later (Amsterdam), or the mode is not `live` or `paper`. The endpoints take the three parameters as plain strings and validate them in `trading_core.journal.bounds`, so a bad value never becomes FastAPI's JSON 422. There is no maximum range length (AC-14 keeps memory flat). Daylight saving changes come from `zoneinfo` `Europe/Amsterdam`, so a day may be 23 or 25 hours long.
- **AC-3**: `journal.csv` holds one header row, then one line per `journal_rows` row (AC-13) of the dashboard's own env, the chosen modes, and the range, in `journal_rows` order. Its columns are exactly, in this order: `env, kind, mode, at_utc, at_amsterdam, instrument_id, side, quantity, price, liquidity, amount, currency, fx_rate, fx_rate_date, eur_amount, strategy, strategy_version, run_id, position_id, source_id`. The file is RFC 4180 CSV (comma separated, `"` quoting only where needed, CRLF line ends), UTF-8 with no BOM, sent as `text/csv; charset=utf-8` with `Content-Disposition: attachment; filename="kfs-journal-<env>-<mode>-<start>-<end>.csv"`. A range with no rows gives the header row only.
- **AC-4**: Value formatting in both CSVs. Decimals are written in plain notation (never an exponent) with a dot. `quantity`, `price`, `amount`, and `fx_rate` keep the stored precision exactly. `eur_amount` is recomputed in `export.py` from `amount` and `fx_rate` with exact Decimal division, then rounded once to 0.01 with `ROUND_HALF_UP` (half away from zero, so `-0.005` becomes `-0.01`), and always written with two decimals. It never re-rounds `journal_rows.eur_amount`. `at_utc` is ISO 8601 with microseconds and `Z` (`2026-10-30T12:02:11.123456Z`). `at_amsterdam` is the same instant in Europe/Amsterdam with its offset (`2026-10-30T13:02:11.123456+01:00`). `fx_rate_date` is `YYYY-MM-DD`. Ids are canonical lowercase UUIDs. A value that does not apply (side on a funding row, say) is an empty field.
- **AC-5**: Nothing is exported in either of two cases. First, a journal row in the range has no ECB rate (`fx_missing`: a currency other than USD, or no `fx_rates` row on or before its UTC date). Second, a row's or a bound's UTC date is later than the newest stored `rate_date`; its rate isn't published yet, and a later export would otherwise change its EUR amount. The rate convention stays spec 0002's: the ECB rate of the row's UTC date, or the latest earlier one. The panel shows "No ECB rate for <earliest such UTC date> yet. The alerter fetches it hourly, so try again later" (or "Currency <X> has no EUR rate" for a non USD currency) and no download links. Both CSV endpoints return 409 with that message as `text/plain`.
- **AC-6**: When the range holds a fill on an inverse instrument (`UnsupportedInstrumentError`), nothing is exported. The panel shows the error's message and both CSV endpoints return 409.
- **AC-7**: Totals. Per currency: `trading` (sum of `fill` rows), `fees` (sum of `fee` rows), `funding` (sum of `funding` rows), and `net` (their sum), all from the native `amount` at full precision. In EUR: the same four, each the sum of that kind's rounded `eur_amount` values from AC-4, so a spreadsheet sum of the `eur_amount` column gives exactly `eur net`. The panel and `summary.csv` also show the row count.
- **AC-8**: Balances at the range's start and end bounds (AC-2). For each `(account_ref, currency)`, use the latest `account_snapshots` row of the env with `captured_at <= bound` whose run has the chosen mode (a row with NULL `run_id` counts as `live`, and only in the `live` env). Show `balance_total` natively, with its `captured_at` and `run_id` so an old snapshot is visible, and in EUR, rounded like AC-4. A USD balance uses the `fx_rates` rate for the bound's UTC date (or the latest earlier date). A EUR balance uses rate 1. Any other currency shows native only, with EUR blank, and never refuses. A pair with no snapshot on or before a bound shows "none recorded" and never refuses the export. Paper balances belong to per run simulated accounts that reset at each run start; the panel says so.
- **AC-9**: The P&L check compares `journal net` (the USD `net` from AC-7) with `dashboard net`. `dashboard net` is `sum(realized_pnl + funding_total)` over the env's positions of the chosen mode with `status = closed` and `closed_at` in the range; that is the same per position formula as `dashboard.data.pnl`, with a mode and range added. It also counts `unlinked rows`: journal rows in the range with NULL `position_id` (fills on stub orders, say), with their USD sum. The check is `not_comparable` when any position of the chosen mode crosses a range edge, or when `unlinked rows > 0`. A position crosses an edge when `opened_at < start` and (`closed_at` is NULL or `closed_at > start`), or `opened_at < end` and (`closed_at` is NULL or `closed_at >= end`). Otherwise it is `match` when `|journal net - dashboard net| <= 0.01 × max(1, positions counted)` USD, and `mismatch` otherwise. In every state the panel shows both figures, the difference, the tolerance, `positions counted`, `positions crossing an edge`, `unlinked rows` and their sum, and the status. `mismatch` gets a warning mark. `not_comparable` says which of the two reasons applies. Neither blocks a download.
- **AC-10**: `summary.csv` (`Content-Disposition: attachment; filename="kfs-journal-summary-<env>-<mode>-<start>-<end>.csv"`, the same CSV dialect) has the header `section,item,currency,value` and these rows. `range` rows: `env`, `mode`, `start`, `end`, `start_utc`, `end_utc`, `time_zone` (`Europe/Amsterdam`), `generated_at_utc`, `git_commit`, `rows`, and for mode `paper` a `note` row reading `paper trading, not real trades`. `totals` rows: `trading`, `fees`, `funding`, `net` per native currency, and the same four with currency `EUR`. `balance_start` and `balance_end` rows, per account: item `<account_ref>` with the native currency and value, the same item with `EUR` (an empty value for "none recorded" or a blank EUR), and items `<account_ref>.captured_at` and `<account_ref>.run_id`. `check` rows: `status`, `reason` (empty, `edge`, or `unlinked`), `journal_net`, `dashboard_net`, `difference`, `tolerance`, `positions_counted`, `positions_crossing_edge`, `unlinked_rows`, `unlinked_net` (currency `USD` where it applies).
- **AC-11**: The panel, `summary.csv`, and the sums of `journal.csv` always agree. One core function computes the totals, balances, and check, and both the panel and `summary.csv` render its result. Each request reads inside one `REPEATABLE READ` read only transaction, so its passes see one snapshot. Because the range ends in the past (AC-2), separate requests see the same rows too. A test sums a generated `journal.csv` and compares against it.
- **AC-12**: All three journal endpoints need the dashboard session, exactly like the other pages, and answer a missing session the same way. The exception is an HTMX request (`HX-Request` header) to the Check endpoint, which gets `HX-Redirect` to the login page, so the login page is never swapped into the panel. Each successful download logs `journal_exported` (INFO) with `env`, `mode`, `start`, `end`, `file` (`journal` or `summary`), and `rows`, never an amount. A refused one logs `journal_export_refused` with the reason code (`fx_missing`, `inverse_instrument`, `bad_range`).
- **AC-13**: `journal_rows` gains, additively over spec 0002 AC-14, a `modes` parameter (a non empty set of `paper` and `live`; the default stays both) and, per row: `mode` (`strategy_runs.mode`), `liquidity` (`fills.liquidity` on fill and fee rows, NULL on funding), `strategy` (`strategies.name`), `strategy_version` (`strategy_versions.version_hash`), `fx_rate` and `fx_rate_date` (the `fx_rates` row used, NULL when missing). Every existing field, its sign, and its order stay unchanged.
- **AC-14**: Memory stays flat. The export reads the range one Amsterdam calendar month at a time through `journal_rows` and streams `journal.csv` out as it goes, holding at most one month of rows at once. The check computes its totals in one pass over the same chunks. The stream is a sync generator (Starlette runs it in the threadpool). It opens its own Postgres session through the `Callable[[], Session]` factory, the way `pnl.py` does, never a request dependency that might close before streaming starts. A database error after the first byte aborts the response (the chunked transfer never completes), so you never get a file that is cut short but looks complete. This assumes a month stays under about 100 000 rows. A one year range of 200 000 rows works within the dashboard's `mem_limit`.
- **AC-15**: The alerter fills `fx_rates` from the ECB Data Portal. At start and then every hour, whenever the newest stored `rate_date` is before today (Europe/Amsterdam), it fetches from the day after that date (or `FX_HISTORY_FROM` when the table holds none) up to today. A failed fetch simply tries again at the next hour, with no extra retry state. HTTP 404 (the API's answer for a period with no observations, such as a weekend or TARGET holiday) means zero rows: a success that writes nothing and logs nothing. That's why the check happens hourly and never spams `fx_fetch_failed`. Each row is `rate_date`, `base_currency = EUR`, `quote_currency = USD`, `rate` (the `OBS_VALUE` parsed straight from its text into a Decimal, never a float), `source = ECB`. Writes are idempotent: an identical existing row is a no op. An existing row with a different rate raises `RecordConflictError`, logs `fx_rate_conflict` (ERROR, with the date and both rates) and keeps the stored row. The parser reads the `TIME_PERIOD` and `OBS_VALUE` columns by header name; its test fixture is a real Data Portal `csvdata` response, captured once and committed. A response that fails to parse (bad CSV, a missing column, a non positive or non numeric rate, a date outside the request) is rejected whole, with nothing written and `fx_fetch_failed` logged with the reason. A successful fetch logs `fx_fetched` with the count and newest date.
- **AC-16**: Migration `0013_alerter_fx_rates` grants `alerter_app` `SELECT, INSERT` on `fx_rates` only, and its downgrade revokes them. `dashboard_app` keeps its existing read only access.
- **AC-17**: Alert rule `fx_rates_stale` (`Tier.WARNING`, a condition) opens when `fx_rates` holds no EUR/USD row or its newest `rate_date` is more than `FX_STALE_DAYS` (default 6) calendar days before today (Europe/Amsterdam). It closes on the next fresh row and renotifies at the engine's normal pace.
- **AC-18**: No test reaches the network. The ECB parser is tested on a fixture response, and the alerter's fetch step takes its HTTP client by injection.

## Decision

**Chosen option**: Option 1: Dashboard export over an extended `journal_rows`, rates fetched by the alerter

The dashboard renders and streams the export from Postgres as `dashboard_app`, using pure core functions for formatting, rounding, totals, and the check. The alerter fetches the daily ECB rate into `fx_rates`.

**Implementation skills**: `fastapi` (`terminalskills/skills`, `.claude/skills/fastapi/`) · `htmx` (`damusix/skills`, `.claude/skills/htmx/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no new table.
- `fx_rates` (spec 0002, unchanged): PK `(rate_date, base_currency, quote_currency)`, `rate > 0`, `source`. Rows here are always `EUR`/`USD`/`ECB`. Kept forever.
- Read only joins: `fills`, `funding_payments` → `strategy_runs` (mode, env) → `strategy_versions` (version_hash) → `strategies` (name); `instruments` (settle currency, multiplier, inverse); `positions` (check); `account_snapshots` (balances).
- Migration `0013_alerter_fx_rates`: the grant in AC-16, nothing else.
- `JournalRow` (core, frozen dataclass) gains `mode: RunMode`, `liquidity: Liquidity | None`, `strategy: str`, `strategy_version: str`, `fx_rate: Decimal | None`, `fx_rate_date: date | None` (AC-13).

**Code placement** (Clean Architecture, per `AGENTS.md`):
- `trading_core.recording.queries`: the extended `journal_rows` (AC-13), plus `journal_check_positions(session, env, modes, start, end)` (the closed positions sum, count, and edge crossings for AC-9) and `balances_at(session, env, modes, at)` (AC-8, with each bound's rate).
- `trading_core.journal` (new, pure, no I/O): `bounds.py` (Amsterdam dates to UTC bounds, validation, month chunks; AC-2, AC-14), `export.py` (CSV header and line formatting, EUR rounding, a `JournalTotals` accumulator, check verdict, summary rows; AC-3, AC-4, AC-7, AC-9 to AC-11). It writes into a text sink the caller passes in, so the code stays pure.
- `trading_core.fx.ecb` (new, pure): `parse_ecb_csv(text, start, end) -> list[FxRateRow]` and `ecb_url(base, start, end)`; typed `FxParseError`.
- `trading_core.recording.repository`: `write_fx_rates(session, rows)`, idempotent, raising `RecordConflictError` on a differing duplicate (the existing convention).
- `trading_core.alerts.rules`: `FX_RATES_STALE`. `trading_core.alerts.checks`: `fx_stale_check(newest, today, days)`.
- `apps/alerter/src/alerter/fx.py`: the fetch loop step (httpx, injected), supervised like the other loops in `service.py`; the staleness observation, checked once a minute.
- `apps/dashboard/src/dashboard/data/journal.py` (session work) and `web/journal.py` (routes), plus the templates `partials/journal_form.html` and `partials/journal_panel.html`. Styles go in `static/css/` and nothing inline (CSP).

**API surface** (dashboard, all GET, same session dependency as the other pages):
| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/trades` | GET | existing page params | page plus the export section with defaults (AC-1) | session | as today |
| `/trades/journal/check` | GET | `start: str` (req, `YYYY-MM-DD`), `end: str` (req), `mode: str` (req, `live\|paper`), all validated in core (AC-2) | HTML partial: totals, balances, check, download links (AC-7 to AC-9) | session (HTMX without session: `HX-Redirect`) | always 200 so HTMX swaps it (htmx skips 4xx and 5xx by default): a bad range, a missing rate, an inverse instrument, or Postgres being down render the refused or error panel state with its message and no links |
| `/trades/journal.csv` | GET | same three | streamed `text/csv` attachment (AC-3, AC-4) | session | 422, 409 (`text/plain` message), 503 |
| `/trades/journal-summary.csv` | GET | same three | `text/csv` attachment (AC-10) | session | 422, 409, 503 |

`journal.csv` runs the refusal checks (AC-5, AC-6) over every chunk **before** the first byte is sent, since a 409 can't be sent once streaming has started. It makes two passes, validate and then stream, inside one `REPEATABLE READ` transaction (AC-11).

**Alerter** (no HTTP surface): `GET {FX_ECB_URL}?startPeriod=<YYYY-MM-DD>&endPeriod=<YYYY-MM-DD>&format=csvdata`, 20 s timeout, no credentials. 200 means parse, 404 means zero rows, anything else (or a timeout) means `fx_fetch_failed` with the status.

**Value sourcing**:
| Action | Value produced / displayed | Source |
|---|---|---|
| form | default start, end | derived: yesterday in `zoneinfo("Europe/Amsterdam")` from the server clock is the end; start is 1 January of that year |
| form | default mode | derived: `live` when `Settings.trading_env` is `live`, else `paper` |
| range | `start_utc`, `end_utc` | derived from the input dates at Amsterdam midnight (AC-2) |
| refusal | newest published rate date | `max(fx_rates.rate_date)` (AC-5) |
| check | unlinked rows and sum | journal rows in range with NULL `position_id` (AC-9) |
| balances | `captured_at`, `run_id` | `account_snapshots` (AC-8) |
| range | env | dashboard `Settings.trading_env` |
| journal.csv | `kind`, `at_utc`, `instrument_id`, `side`, `quantity`, `price`, `amount`, `currency`, `run_id`, `position_id`, `source_id` | `journal_rows` (spec 0002 AC-14) |
| journal.csv | `mode`, `liquidity`, `strategy`, `strategy_version`, `fx_rate`, `fx_rate_date` | `journal_rows` additions (AC-13): `strategy_runs.mode`, `fills.liquidity`, `strategies.name`, `strategy_versions.version_hash`, the `fx_rates` row used |
| journal.csv | `at_amsterdam` | derived: `at` converted to `Europe/Amsterdam` |
| journal.csv | `eur_amount` | derived: `amount ÷ fx_rate`, `ROUND_HALF_UP` to 0.01 (AC-4); replaces the full precision `eur_amount` of spec 0002 in the file |
| totals | per currency and EUR sums, row count | derived in `JournalTotals` from the rows (AC-7) |
| balances | `balance_total`, account, currency | `account_snapshots` (AC-8) |
| balances | EUR balance | derived: USD balance ÷ `fx_rates` rate on or before the bound's UTC date; EUR at rate 1; other currencies blank; rounded like AC-4 |
| check | dashboard net, positions counted, crossing edges | `positions.realized_pnl + funding_total`, `status`, `opened_at`, `closed_at` via `journal_check_positions` (AC-9) |
| check | tolerance | decided here: 0.01 USD × max(1, positions counted) |
| summary.csv | `generated_at_utc`, `git_commit` | server clock; `Settings.git_commit` |
| fetcher | whether to fetch, from date | fetch when newest `fx_rates.rate_date` < today (Amsterdam); from newest + 1 day, else `FX_HISTORY_FROM` |
| fetcher | rate rows | the ECB CSV's `TIME_PERIOD` and `OBS_VALUE` columns |
| stale rule | newest date, today | `max(fx_rates.rate_date)`; today in Europe/Amsterdam |

**Key invariants**:
- No float anywhere in the path: rates, amounts, and totals are `Decimal` from parse to file.
- The export never writes to Postgres or Redis (the dashboard stays read only).
- A file is either complete with every EUR amount filled in, or it isn't sent (AC-5, AC-6).
- The panel's numbers equal `summary.csv`'s, and both equal the `journal.csv` sums (AC-11).
- `fx_rates` rows never change once written. ECB reference rates are final, so a differing duplicate counts as an error, never an update.
- The only env is the dashboard's own, so demo and live never mix in one file.

**Security model**: a single user behind the existing session login (password plus TOTP, spec 0004) over Tailscale only. The export carries financial records but no secrets or keys. GET only, no state change, so no CSRF surface. No rate limit beyond the existing login lockout, since the port binds to `127.0.0.1` behind Tailscale. The audit trail is the `journal_exported` log line (AC-12). The alerter's ECB call is unauthenticated and outbound only. `alerter_app` gains insert on one reference table and nothing else (AC-16). Not regulated data in a GDPR sense (your own records). Tax compliance is your own responsibility; this export supports it and isn't tax advice.

**Configuration required** (all have defaults, so nothing needs adding to `/etc/kfs/kfs.env` before the deploy):
- `FX_ECB_URL`: default `https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A`
- `FX_HISTORY_FROM`: the first rate date to backfill, default `2026-01-01`
- `FX_STALE_DAYS`: default `6` (AC-17); a TARGET holiday like Easter or Christmas can leave the newest rate 5 days old with nothing wrong

All three are fields of the alerter's settings class in `apps/alerter/src/alerter/settings.py` (built on `trading_core.settings`) and validated at startup (a URL with https scheme, an ISO date not in the future, an integer from 1 to 14). No fetch time setting is needed, since the hourly check (AC-15) picks up each day's rate within an hour of ECB publishing it (around 16:00 CET).

**Critical test scenarios**:
- Happy path: a seeded paper run with fills (one with a rebate), a funding payment, and rates spanning a weekend. `journal.csv` columns, order, rounding, and both time columns are exact; totals, balances, and the check `match`. Verifies **AC-1, AC-3, AC-4, AC-7, AC-8, AC-9, AC-10, AC-11, AC-13**.
- Daylight saving: a range covering the last Sunday of October gives a 25 hour day, and a fill at 00:30 Amsterdam on 1 January lands in the new year's range. Verifies **AC-2**.
- Missing rate: delete the rates before a fill's date, and both CSVs return 409 naming that date while the panel shows no links. The same happens for a row dated after the newest stored rate. Verifies **AC-5**.
- Mismatch and not comparable: alter one position's `realized_pnl` by 0.05 to get `mismatch` with downloads still allowed. Open a position across the end bound to get `not_comparable` (edge), and add a fill with NULL `position_id` to get `not_comparable` (unlinked). Verifies **AC-9**.
- Bad input and HTMX: `end` set to today, `mode=both`, and `start=2026-13-01` each give a 200 refused panel from Check and 422 from the CSVs. A Check with `HX-Request` and no session gets `HX-Redirect`. Verifies **AC-2, AC-12**.
- Mid stream failure: a database error after the first chunk aborts the response, with no complete looking file. Verifies **AC-14**.
- Streaming: 200 000 seeded rows over 12 months export with flat memory, and the refusal check runs before the first byte. Verifies **AC-14, AC-5**.
- Fetcher: the fixture CSV inserts rows, a second run is a no op, a 404 writes nothing and logs no failure, a differing rate raises and keeps the stored row, and a malformed body writes nothing. Verifies **AC-15, AC-18**.
- Stale: newest rate 7 days old opens `fx_rates_stale`, 5 days old does not, and a fresh row closes it. Verifies **AC-17**.
- Grant: `alerter_app` can insert into `fx_rates`, but not update it or insert anywhere new. Verifies **AC-16**.
- Auth: no session on each journal endpoint behaves like `/trades` without a session, and no file is sent. Verifies **AC-12**.

## Build plan

Tracer Bullet: first one thin real thread from ECB to `fx_rates` to a downloaded CSV, then thicken it with the check, the balances, and the summary.

1. [x] Migration `0013_alerter_fx_rates`, `trading_core.fx.ecb` parser with a fixture test, and `repository.write_fx_rates`, satisfies **AC-15, AC-16, AC-18**.
2. [x] Alerter `fx.py` hourly fetch step and the three settings, wired into `service.py`'s supervised `_every` loops; on the laptop, the first start backfills from `FX_HISTORY_FROM`. Satisfies **AC-15, AC-18**.
3. [x] Extend `journal_rows` (modes, new fields) with its integration tests, keeping every spec 0002 AC-14 test green, satisfies **AC-13**.
4. [x] Thin thread: `trading_core.journal.bounds` and the CSV half of `export.py`, `/trades/journal.csv` with the refusal checks, and the bare form on `/trades` with a direct link (no panel yet), satisfies **AC-1 (form part), AC-2, AC-3, AC-4, AC-5, AC-6**.
5. [x] `JournalTotals`, `balances_at`, `journal_check_positions`, the check verdict, the HTMX panel (`/trades/journal/check`), and `summary.csv`, satisfies **AC-1, AC-7, AC-8, AC-9, AC-10, AC-11**.
6. [ ] Monthly chunking with the two pass stream, the logging, the auth tests, and a 200 000 row memory test, satisfies **AC-12, AC-14**.
7. [ ] `fx_rates_stale` rule and check in core, wired into the alerter, satisfies **AC-17**.
8. [ ] Styleguide samples for the panel's states (match, mismatch, not comparable, refused) per `DESIGN.md`, and the dashboard and alerter `AGENTS.md` lines via `/sync`, satisfies **AC-1, AC-9**.
9. [ ] VPS check: after the deploy, confirm `fx_fetched` backfilled 2026, then export the demo paper range since the 0f12982 deploy (mode `paper`) and save the panel result and both files' sums in `verify.md`, satisfies **AC-5, AC-9, AC-11, AC-15**.

## Consequences

**Positive**:
- Your tax and analysis records come straight from the fills, at the official rate, with each EUR amount checkable from its rate and date.
- A recording bug shows up as a visible mismatch, rather than as a silently wrong file.
- No new service, table, or secret, and no kfs.env change.

**Negative / tradeoffs**:
- `not_comparable` will be common for ranges that cut through an open position. The check only gives a verdict when the range edges fall while you're flat.
- The tolerance (one cent per position) can hide a systematic error smaller than a cent per trade. The journal itself stays exact, and only the verdict is loose.
- The alerter now also does a data job. If it's down, rates stop arriving, though the stale alert and the export's refusal both catch that.
- `journal.csv` makes two passes over the data (validate, then stream), so a long range reads Postgres twice.
- Paper balances are per run simulated accounts that reset at each run start, so they mean little. They're shown for completeness only.
- The ECB API's CSV format is outside your control. A change to it makes the fetch fail closed and alerts you after 4 days.

**Neutral**:
- The CSV's `eur_amount` is rounded, while `journal_rows.eur_amount` stays full precision for other callers.
- Live rows only exist after feature 19. Until then, a `live` export returns the header and zero totals.

## Follow-up

- [ ] Spec 0002's `Signs` line says `realized_pnl` is "net of fees and funding", but `dashboard.data.pnl` (spec 0011 AC-18) adds `funding_total` to it. The code and spec 0011 are right (net of fees only). The spec 0002 line needs correcting on its next update.
- [ ] Feature 19 should list feature 18 among its live prerequisites (already noted in the scope) and run a `live` export during the canary.
- [ ] Ask your tax adviser whether the box 3 value they need is the 1 January balance this export gives, or the position value at mark price (not included here).
