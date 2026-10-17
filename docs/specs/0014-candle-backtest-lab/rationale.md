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
