# 0014. Rationale

## Context

Scope feature 13 asks for hour level backtests with realistic fees, funding, and slippage over any date range, with the strategy code shared with the paper loop. The hour level candidates (feature 17) need years of data, across more than one market regime, to say anything. The only historical data in the repo today is order book data: the free Tardis first of month days (about 55) and a few days of our own recording. That suits the scalping simulator (spec 0008) but cannot give continuous years.

The forces: you want free data only. Money must never be a float. Strategies must run unchanged between backtest and paper (the core promise of spec 0001's NautilusTrader choice). The laptop has a 4 GB Docker VM, and the lab must stay laptop only and demo only. The scope carries a specific warning: swing based signals often look ahead, which makes backtests look far better than live. Feature 15 will score candidates and must be able to read results without a new path.

Without this feature, no hour level strategy can be judged, so the fallback set (feature 17) and the promotion gate (feature 15) both stall.

## Options considered

### Option 1: Nautilus native bar backtest

Feed stored 1 minute `Bar` objects straight to the engine with `bar_execution=True` and let Nautilus' bar based matching fill orders (open, high, low, close as four internal price updates, adaptive high or low order), with Nautilus' `FillModel` for slippage.

**Pros**:
- Least code: the engine already turns bars into fills.
- Strategies can subscribe to the external 1 minute bars directly.

**Cons**:
- Its high or low ordering cannot follow the position, so "stop first" cannot be enforced.
- `FillModel` slippage is one tick with a probability, close to nothing on BTC; a fixed bps cost would need post processing that breaks the link between engine fills and rows.
- Strategies would subscribe to external bars in the backtest and internal bars live: a config difference between backtest and paper.

### Option 2: synthetic quote and trade path (chosen)

Turn each 1 minute candle into four position aware path points, each emitted as a quote whose spread carries the slippage and as a trade carrying a quarter of the volume, into an L1 venue in the existing lab structure.

**Pros**:
- Stop first and slippage live in the data the engine sees, so engine fills, rows, and reports agree.
- Strategies build their own bars from trade ticks exactly as in the order book replay and live, with the same config.
- Reuses the lab's pool, registry, recorder, funding, metrics, bundle, and report code.

**Cons**:
- The stream must read the position each minute, which ties data feeding to engine state (proved in the gate).
- Four points per minute make stop fills land at the minute's extreme.

### Option 3: a separate vectorized backtester

A pandas or pure Python loop over candles, outside Nautilus.

**Pros**:
- Very fast, simple to reason about.

**Cons**:
- Strategy code would be written twice, breaking the "same code" requirement and spec 0001's decision.
- Floats creep in; fees, funding, and risk checks would be reimplemented.

### Option 4: build candles from Tardis or our own trades

**Pros**:
- Exact candles from trades we can audit.

**Cons**:
- Free Tardis days are first of month only, and our own recording covers days, so no continuous years.

## Rationale

Option 2 is the only one that keeps both promises the scope makes: the same strategy code (no external versus internal bar split, no rewrite) and honest costs (slippage and stop first are enforced by the data itself, not patched afterwards). Its main risk, reading the position while feeding data, is exactly what gate G-3 measures before anything else is built, and a failed gate brings this back to `/architect`. Option 1 is cheaper, but its fills cannot be made pessimistic in the way you chose, and the external bar subscription would make backtest and paper configs differ. Option 3 fails the core requirement outright. Option 4 fails the data requirement.

Kraken's own charts endpoint and funding export are free and cover the exact contract since launch (the funding endpoint alone covers only a rolling year, see *Gate findings settled*). A different venue's longer history was rejected because its prices and funding differ. Monthly Parquet follows the repo's market data pattern (Parquet plus DuckDB for research, nothing about market data in Postgres) and keeps fetch, commit, and refetch in one unit. The standard library client avoids pinning a dependency for two GET endpoints. One continuous engine run per range matches how hour strategies hold (hours to days). Cutting at midnights or gaps, as the order book lab must, would force fake closes, and positions riding through a data gap is the honest gap risk. The candle versus order book parity proves the shared code numerically without touching the VPS trader, which still runs one hard wired strategy.

Calls made on your behalf (each with its runner up):
- Path point times `+1 ms / +20 s / +40 s / +59.999 s`: the bar timer at a minute boundary runs before that minute's open, so a decision on a close fills at that close. Runner up: all four points at the close time, which breaks ordering.
- Slippage as the quote spread, not a post fill charge: the fills in Postgres are the prices paid. Runner up: a cost line in metrics.
- Buy and hold sized by a fixed notional with no stop (`check_entry` accepts a fixed quantity): a fair benchmark. Runner up: risk sized with a far stop.
- MACD baseline sized by fixed notional with a 3 % protective stop: comparable to buy and hold and it exercises the stop path. Runner up: risk sized entries, deferred to feature 17.
- Funding mark price = the last path point price: candles carry no mark, and the error is a rate times a few bps. Runner up: also fetch mark candles.
- Liquidation price from the equity at position open: it matches how `check_entry` estimated it. Runner up: recompute with live equity each quote.
- Look ahead cuts excluding the first and last 10 % of hours: cuts there test little. Runner up: uniform over all hours.
- Parity on `1-MINUTE-LAST` bars: enough decisions in a few recorded days. Runner up: `1-HOUR-LAST`, too few decisions to reach a ratio.

## Cross check (2026-10-17)

A read only critique on another model found 16 gaps and 6 soundness points before acceptance. All were resolved in `index.md` with its recommended fixes. The load bearing ones:
- Quotes now use a named `QUOTE_SIZE`, because the pinned instrument has no maximum quantity.
- At each path point the trade now comes before the quote, because an L1 book may take its top from a trade and erase the slippage spread. Gate G-3 asserts it.
- Funding rows are stamped so the row at a boundary is the rate charged there and known by then (gate G-2), and a warmup row before the range keeps the first entry from being refused as `funding_stale`.
- The path order's position source is a `PositionSideFlag` actor, with the entry minute exception stated.
- The liquidation guard's inputs are named (the entry verdict's equity, the fee in USD). The ruin case (equity at or below zero) is defined.
- The candle path's own plan, result, and variant types are named, instead of claiming spec 0008's types fit as they are.
- The report gains a marked to market drawdown and its duration, exposure, a long and short split, profit factor, expectancy, and a data quality block. Several of these also came from a revision 2.0 scope draft reviewed the same day.
- The precision of 2022 history against the pinned tick is checked in gate G-1 before Milestone 2 starts.

## Gate findings (2026-10-17)

Milestone 1 ran `scripts/spikes/0014_gate.py` on the laptop: `candles` and `funding` with the host Python (public, unauthenticated GETs only, no Kraken key), `engine` in the `kfs/trader:dev` image (NautilusTrader 1.231.0) with the pinned `PF_XBTUSD.KRAKEN` instrument. Data: the charts endpoint for 2022-03, 2022-04, 2023-06, 2024-06, 2025-06 and 2026-09, the historical funding endpoint, the charts `funding` analytics, and the free Tardis `derivative_ticker` day 2026-10-01. **Result: the gate does not pass.** The first five findings below need a spec change. The rest confirm the design or are build notes.

**Needs a decision**
- **Free funding history reaches back only about one year.** The historical funding endpoint is a rolling window: 8,812 rows from 2025-10-15T08:00Z to 2026-10-17T19:00Z. It ignores date parameters. The charts `funding` analytics starts around 2026-04. So no free source found here covers 2022-03 to 2025-10. AC-2, AC-6 (a funding row at every boundary) and AC-15 cannot hold for multi year ranges, and AC-23's full history run cannot pass AC-6. A few hours inside the window are missing too.
- **Volume is off the size step from float noise.** Every month has volume strings like `0.3565999999999999`: from 113 to 21,588 rows per month. AC-3 as written refuses every month, and AC-1 says an off step month goes back here. Prices are always whole integers on tick 1, so only volume is affected.
- **A minute with no trades comes back as a flat candle at the previous close with volume 0**, not as a missing row. 9,543 of 12,357 minutes in 2022-03, about 68 % of 2022-04, and 337 minutes in 2026-09. AC-4 counts missing minutes only, so these would never count as gaps. AC-9 would emit size step synthetic trades for them (volume 0 floors to the step), and the stale quote block would never fire inside a quiet stretch.
- **Full history speed misses AC-23.** One 30 day month (345,600 events, a `1-HOUR-LAST-INTERNAL` subscriber) ran in 57.4 s, about 6,000 events a second, so 55 months take about 53 minutes against the 30 minute budget. Peak RSS was 409 MB, well inside 1.5 GB.
- **An hour with no candles still produces an hour bar.** With no trades for a whole hour the internal aggregator emits a bar at the hour close with open = close = the last price and volume 0. AC-11 does not say whether a strategy should see that bar.

**Confirmed as the spec assumes**
- Candles (AC-1): pages hold up to 2,000 rows, paging uses `more_candles` and the last row's `time`, `time` is the minute's open in ms, and every value is a string decimal. No duplicates across page edges, every row on a whole minute, high and low always consistent. The earliest minute is 2022-03-23 10:03 UTC (the spec says 2022-03-22). Fetching takes about 10 s a month, so about 10 minutes for the whole history.
- Funding stamp (AC-2): the rate is a JSON number, so parse it with `parse_float=Decimal`. Tardis's rate charged at T equals Kraken's row stamped T − 1 h, so Kraken stamps the start of the period and the row belongs at stamp + 1 h, as AC-2 says.
- Engine (AC-8 to AC-10): a zero size `TradeTick` is refused (`ValueError: 'size' not a positive integer, was 0`). A timer at T runs before the path point at T + 1 ms. With the trade before the quote, a market buy on the first hour bar (close 60,020) fills at the close point's ask 60,033, at the bar's own timestamp. A sell stop with trigger 59,500 fills at 58,988, the bid of the 59,000 low, not at its trigger. A buy limit at 59,500 does not fill on a touch (low 59,500, ask 59,512) and fills at 59,500 as MAKER on a trade through (low 59,480).
- Hour bars (AC-11): bars build from iterator fed trades. A run starting at 00:30 gets a partial first bar at 01:00 (30 minutes of volume).
- Side flag (AC-9): with one minute chunks, a fill at minute 3.5 is seen when minute 4 is generated, so the flag lags by at most one minute. A plain `Actor` gets no position events by default, so `PositionSideFlag` must subscribe to `events.position.*` on the message bus.

## Gate findings settled (2026-10-17)

A second `/architect` pass the same day settled every finding. Two more problems came up while checking the code, and are settled here too.

### New evidence

- **Kraken's funding export.** Kraken support's "Export historical funding rates" page offers a free ZIP (about 110 MB, no login, last updated 2026-02-16) with one CSV per derivatives market. `exports/PF_XBTUSD.csv` has 30,431 rows, columns `timestamp,tradeable,absolute_rate,relative_rate`, from 2022-03-22 16:00 to 2026-02-01 00:00 UTC. Every stamp is on a whole hour. Of the endpoint's 8,812 rows, 2,604 fall inside the export, and all 2,604 have the same stamp and the same relative rate. So the export uses the same stamp convention (period start) and the same rate as the endpoint the gate already checked against Tardis.
- **Funding was every 4 h until 2022-09-29 12:00 UTC** (1,145 steps of 4 h), hourly after that. The spec assumed hourly everywhere.
- **Six funding holes** of 2 to 3 h: after 2025-07-10 21:00, 2025-08-31 03:00, 2025-11-01 15:00 (3 h), 2025-11-02 03:00, 2025-11-19 04:00, 2025-12-04 08:00. AC-6 as written would refuse any range crossing one of them.
- **Stale quote block clash.** The shared stale check (`trading_core/node/placeholder.py`, every 1 s, stale after `stale_quote_s` 10) would trip between path points about 20 s apart. It would raise a stale feed block and a risk event every minute, and could refuse an entry at an hour close, depending on timer order. The gate did not test this.
- **Funding staleness.** `check_entry` refuses `funding_stale` when `funding_periods > 0` and the rate is missing, and the strategy's market context drops a rate older than `context_max_age_funding_s` (7200). In the 4 h era that would refuse entries for about half of every period.

### Decisions (your picks, each with its runner up)

- **Funding history: Kraken export plus endpoint, merged** (AC-2, AC-2a). Full history since launch, free, and from Kraken itself, so it matches the contract. Rows are never deleted, because the endpoint's window rolls. Runner up: shorten ranges to the endpoint's year (one market regime only). Rejected: ranges with no funding, which flatter long holds and need `use_market_context` off.
- **Import by hand: `lab import-funding <file>`.** You download the ZIP; the lab reads only `PF_XBTUSD.csv`. The CMS link is hashed and will change, and this is a one time step. Runner up: a URL setting the lab downloads from.
- **4 h era: charge each row at stamp + its period** (AC-2, AC-15), with gate G-4 settling whether the rate is per period or per hour. Runner up: start ranges at the hourly switch, losing six months including the 2022 crash.
- **Funding holes: carry the last rate for up to 3 h** (AC-6, `FUNDING_CARRY_MAX`), and count the carried charges in the report. That covers every known hole and still refuses a real outage. Runner up: carry any hole, which hides an outage.
- **Funding staleness in candle runs: 18 000 s** (one 4 h period plus 1 h) as the candle run file's `[context]` default. Paper and replay keep 7200. No core change. With the 3 h carry cap it behaves as "fresh until 1 h after the next charge". Runner up: a core rule built on `next_funding_ns`, deferred to Follow-up.
- **Volume noise: round when within `1e-8` of a step, and count** (AC-1, AC-3). That fixes noise like `0.3565999999999999` and still catches a real precision change. Runner up: round everything, which would hide one.
- **Volume 0 flat minutes: a quiet minute, one quote and no trade** (AC-1, AC-9). No volume is invented, quotes stay fresh as in the real market, and quiet minutes are counted apart from missing ones. Runner up: drop them as missing, which would make the stale block refuse entries through most of early 2022.
- **Stale quote block: 300 s in candle runs** (AC-13), the same 5 minute rule as gap rows. Runner up: 90 s, which would raise many risk events on early 2022 data.
- **Empty hour bar: strategies see it, as in paper** (AC-11). Nautilus' default `time_bars_build_with_no_updates = true` applies to the paper trader too, so backtest and paper agree. Runner up: switch it off in both, which changes spec 0006's trader config.
- **Speed: AC-23 budget 60 min.** One minute chunks keep the stop first order, which matters more than the time of an occasional run. Runner up: keep 30 min and add a profiling task of unknown size.
- **Earliest range start: 2022-03-24** (`CANDLE_RANGE_FLOOR`), the first whole day after the first minute (2022-03-23 10:03). AC-6 also refuses a range before the first stored minute's day. Runner up: allow 2022-03-23, a half empty first day.

### Cross check of the revision (2026-10-17)

A read only critique on another model found 11 gaps in the revision, and you chose to apply every recommended fix. The load bearing ones:
- One pure `expected_charge_times` drives the manifest, the range check, and the funding charges, because `funding_boundaries` and `LabRecorder` assume hourly charges.
- A per hour finding in G-4 multiplies the rate by 4 at import, not at charge time, so `market_context()` and `check_entry`'s funding cost see the same rate as the payments.
- Funding rows go into the month of their charge time and carry a `source` column; an import never changes `fetched_at`.
- The stale check moves into a shared `StaleQuoteBlock` mixin with a configurable interval (60 s in candle runs), because a 1 s timer would add about 140 million events to a full history run.
- Quiet means flat with volume 0 after rounding; a non flat candle that rounds to 0 stays a trade minute. The stream starts at the range's first trade minute.
- `LabRecorder` counts `zero_volume_bars` from the strategy's own internal bar type; data quality figures are labelled per month touched.

## Gate deltas (2026-10-17)

The build plan's deltas 3a to 3c, run with the same spike (`scripts/spikes/0014_gate.py`: `candles` with the host Python, `engine-b` in the `kfs/trader:dev` image with the pinned instrument mounted at `/spike/PF_XBTUSD.KRAKEN.json`).

**G-1b (3a): passes.** Over the six sampled months every volume that is off the size step is within `1e-8` of one and rounds (113 in 2022-03, 658 in 2022-04, 3,614 in 2023-06, 6,990 in 2024-06, 8,503 in 2025-06, 21,588 in 2026-09). No volume is further off. Every volume 0 row is flat and equal to the previous close (quiet minutes: 9,543, 29,177, 8,182, 1,686, 758, 337). No candle rounds to volume 0 without being flat, so `zero_volume_trade_rows` is 0 in every sampled month.

**G-3b (3c): passes.**
- With `stale_quote_s` 300 and a 60 s check, the hour closes at 01:00 to 03:00 are not blocked, and the 04:00 close after a 7 minute gap is blocked. For contrast, the paper rule (10 s, checked every 1 s) blocks every one of those hour closes on the path, which confirms the clash this revision fixed.
- An hour made only of quiet minutes (one quote each, no trade) adds no trade (480 trades from the 120 traded minutes) and builds a bar with volume `0.0000`, and the strategy receives that bar unblocked.
- An actor subscribed to `PF_XBTUSD.KRAKEN-1-HOUR-LAST-INTERNAL` next to the strategy gets the very same bar objects (3 of 3, identical), and the data engine holds one aggregator for that bar type.
- One 30 day month: 46.7 s with the 60 s check (about 7,400 data events a second) against 53.2 s with a 1 s check (2.6 million timer events). 55 months come to about 43 minutes, inside AC-23's 60 minute budget.

**G-4 (3b): passes. A 4 h era rate is charged once per 4 h period, so there is no × 4 at import and `CANDLE_RANGE_FLOOR` stays 2022-03-24.** Run with the `funding4h` check on Kraken's funding export (30,431 rows) and the free Tardis `derivative_ticker` days 2022-05-01 to 2022-09-01 (4 h era) plus 2022-10-01, 2023-06-01 and 2025-06-01 (hourly era). A quick web search found no dated Kraken note on the 2022 switch to hourly funding, so G-4 rests on the data.
- The export's 4 h era holds 1,145 rows, every step exactly 4 h, the last at 2022-09-29 08:00 and the first hourly row at 2022-09-29 12:00, which matches `FUNDING_HOURLY_FROM`.
- Stamp: on all five 4 h era days, Tardis's `funding_rate` at each charge time equals the export's `absolute_rate` stamped 4 h earlier (35 of 35), so a stamp is the start of its period and the charge time is stamp + 4 h.
- Meaning: `relative_rate` ÷ the previous period's mark minus index premium has a median of about 0.125 in the 4 h era (0.103 to 0.132 per day) and about 0.043 in the hourly era (0.042 to 0.044), about 2.9 times as large. The hourly rate is about premium ÷ 24, so a per hour rate in the 4 h era would sit near 0.042 and a per period rate near 0.167. The data sits close to the per period value and far from the per hour one.

## Off tick prices (2026-10-18)

Build plan task 12 (the full history fetch) stopped at 2023-08 with `lab_candles_bad_rows`. Gate G-1 sampled one month a year (2022-03, 2022-04, 2023-06, 2024-06, 2025-06, 2026-09) and so missed the stretch where Kraken's charts return highs and lows off tick 1. Fetching month by month found them in six months only: 2023-08 (13 rows), 2023-09 (5), 2023-10 (8), 2023-11 (6), 2023-12 (2), 2024-01 (2). Every other month from 2022-03 to 2026-10 passes AC-3 as written.

### Off tick evidence

The 36 rows, each read with its neighbouring candles (a scratch script over the public charts endpoint):
- **4 are float noise** (2023-12-02, 2023-12-24, 2024-01-03, 2024-01-05), for example a high of `39522.99999999999` and a low of `42938.99999999999`, each `1e-11` below a whole number, like the volume noise AC-1 already rounds.
- **32 are real fractions**, all in 2023-08 to 2023-11, for example a low of `28319.01027283563` (2023-08-17 00:44 UTC, volume 151 BTC) and a high of `25920.8322615` in the 2023-08-17 crash. Every one is the high or the low of a fast move with heavy volume, 50 to 760 USD beyond the candle's open and close, and the next candles trade in the same area on whole numbers. Most likely they are fills Kraken prints off the tick (liquidations or similar); the candles alone cannot say which.
- **No open or close is ever off the tick**, in any month.

### Off tick decisions (your picks, each with its runner up)

1. **Extremes are rounded toward the body.** Noise within `1e-8` of a tick snaps to it (`ROUND_HALF_EVEN`); a high further off is rounded down to the tick and a low up. Because open and close are whole numbers, the rounded high still sits at or above them and the rounded low at or below them. The stored candle never shows a price beyond the range Kraken reported, and for every whole tick price P, "did the minute reach P" has the same answer before and after rounding, so a stop or limit on the tick triggers exactly as it would have. The difference from the real extreme is under one tick (under 1 USD on prices near 25,000 to 45,000). Runner up: round to the nearest tick, simpler but it can store a price one tick past what traded (25920.83 would become 25921, so a stop at 25921 would fire in the backtest but not in reality). Refusing the six months was rejected because no backtest range could then cross 2023-08 to 2024-01, which holds the 2023-08 crash and the 2023-10 rally. Storing the exact fractions was rejected because a Nautilus `Bar` holds prices at the instrument's precision (0).
2. **An open or close off the tick stays a bad row unless it is noise.** The open and close drive the quote path (AC-9) and every fill, so a real surprise there should stop the fetch rather than pass quietly. Runner up: the same rounding as the extremes, which never refuses but would hide a real change in Kraken's data. (Amended 2026-10-28 by spec 0022 AC-2a: ETH has two off tick closes, so an off tick open or close is now rounded to the nearest tick inside the stored low to high, and stays a bad row only past 10 such candles in a month.)
3. **The manifest counts rounded candles in a required field `price_rounded_rows`, and every stored month is fetched again in task 12.** The whole history fetch is due anyway for AC-23's timing, so every manifest is rewritten with the field and the reader can require it, keeping the commit point check strict. Runner up: the reader defaults a missing field to 0, which would be correct for the 49 months already stored (they passed the strict check, so their true count is 0) but accepts manifests missing a field from then on.

### Cross check of the off tick rule (2026-10-18)

A read only pass on another model confirmed the rounding claim: for every whole number P, a raw high reaches P exactly when its floor does (the same for a low and its ceiling), and because open and close are whole numbers the high and low checks give the same result before and after rounding, so a rounded candle can never have a high below its low. It found eight gaps, all settled your way the same day:
- An old manifest without `price_rounded_rows` would crash the reader with a bare `TypeError`, first inside the `--refetch` next month check. It now counts as no manifest everywhere (fetched again, skipped by the next month check, a missing month for `lab backtest`), and the reader raises `CandleInputError`. That makes `--refetch` unneeded in task 12: one plain run fetches every month again and is the timed AC-23 run.
- The check order is written down (round first, then every check on stored values), so a noise high just under the close is not a bad row, page edge copies that differ only in noise are one row, and the quiet decision uses stored prices.
- `price_rounded_rows` counts a candle once and never counts a bad row. `lab_candles_price_rounded` logs up to 50 real roundings per month, so the raw value is not lost; noise snaps are only counted.
- The `1e-8` bound is inclusive, like volume, and the wording about prices beyond Kraken's range now allows for the noise snap.

## Look ahead cut placement (2026-10-19)

Build plan task 17 found a blind spot in AC-19 as first written. A cut fell on a whole hour T, only decisions strictly before T were compared, and the cut run still fed the minute ending at T, so it built the bar closing at T. A strategy that decides about the bar closing at T only after the next bar closes, and stamps that decision at T, is in the full run at T and never compared. Its decision about the earlier bar, made at T from the bar closing at T, is in both runs. So a one bar peek, the classic look ahead bug, always passed. The test only `peek_probe` showed it, and its two tests were strict xfails.

### Cut placement decision (your pick through "handle it all as recommended", with its runner up)

**A cut is a seeded whole hour plus 1 to 59 seeded minutes, never a whole hour.** `random.Random(seed)` first samples three distinct hours whose 59th minute still lies in the middle 80 %, then draws a minute offset for each. The cut run ends at the cut (its last path point 1 ms before it), so it holds the bar closing at T but not the one closing at T + 1 h. A decision about bar T made from bar T + 1 h is stamped at most at T, before the cut, and only the full run has it, so every peek of one bar or more is caught at every cut. A shorter peek past a bar close is caught when the cut lies inside it, about d ÷ 60 per cut for a peek of d minutes. Honest hour level strategies decide on whole hours before the cut with the same data in both runs, and the intrabar events between T and the cut (stop fills, the liquidation guard) come from data both runs fed. The range end close of the cut run acts 1 ms after its last point, at the cut, and is filtered out by reason as before. Only `cut_times` changes; the unit, the closer, and the compare stay as built.

Runner up: keep whole hour cuts, compare decisions up to and including the cut, and move the cut run's range end close and final quote past the cut so the strategy decides at T with the gate still open. It catches any peek past the bar close at T for certain, but it moves the closer and final quote timing that the range end close relies on (AC-8, AC-14) for cut runs only, so cut runs would end differently from real runs. Comparing inclusively with no other change was rejected: the closer acts at T, the same time the bar at T reaches the strategy, so an honest decision at T could differ between the runs and fail a correct strategy.

