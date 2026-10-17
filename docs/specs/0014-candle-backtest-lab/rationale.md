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

Kraken's own charts and funding endpoints are free and cover the exact contract since launch. A different venue's longer history was rejected because its prices and funding differ. Monthly Parquet follows the repo's market data pattern (Parquet plus DuckDB for research, nothing about market data in Postgres) and keeps fetch, commit, and refetch in one unit. The standard library client avoids pinning a dependency for two GET endpoints. One continuous engine run per range matches how hour strategies hold (hours to days). Cutting at midnights or gaps, as the order book lab must, would force fake closes, and positions riding through a data gap is the honest gap risk. The candle versus order book parity proves the shared code numerically without touching the VPS trader, which still runs one hard wired strategy.

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
