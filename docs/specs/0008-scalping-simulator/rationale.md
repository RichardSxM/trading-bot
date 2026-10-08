# 0008. Rationale: scalping simulator

## Context

The bot is meant to scalp BTC perpetuals on a seconds timescale, and it only trades real money after a strategy passes backtests and paper trading. Paper trading cannot judge a scalper on its own. Kraken closed its futures demo on 14 July 2026, so paper fills come from NautilusTrader's sandbox. The sandbox fills against live quotes but ignores queue position and latency, and spec 0001 already warned that it may flatter a scalping strategy. At a seconds timescale, those ignored details are most of the result. Kraken's base fees (0.02 % maker, 0.05 % taker) are the same size as the moves a scalper hopes to catch.

Two effects decide whether post only (maker) entries are worth their lower fee. A resting limit order often never fills. And when it does fill, it is often because price is running through it, so the average filled trade is worse than the average signal (adverse selection). A simulator that fills every limit order touched by price hides both, and it would make maker strategies look profitable when they are not. The promotion gate (feature 15) wants to score every scalping candidate twice, once with maker entries and once with taker entries. That only means something if the simulator models both effects and measures them.

The data available is limited by the choice to use free sources only. Our recorder has written BTC order book deltas and trades since early October 2026. After the disk incident it records BTC only, and it converts to hourly Parquet with gaps, holes, and late rows logged (spec 0007). Tardis.dev offers one free day a month (the first) of Kraken Futures L2 and trades, recorded in London, back to 2022. A day of BTC L2 is about 45 million rows. The VPS has 4 GB of RAM and already showed queue pressure in the recorder, so heavy replay work cannot share it with the live trader.

The workspace already has most of the surrounding parts. NautilusTrader 1.231.0 is pinned, and its backtest venue (checked in the trader image on 2026-10-08) offers `queue_position`, `liquidity_consumption`, a `LatencyModel`, and `MakerTakerFeeModel`. It also has `TardisCSVDataLoader` with funding rate loading. Spec 0002's schema has `strategy_runs` with mode `replay`, `run_results`, and an all or nothing `write_run_bundle`. Strategies get their pause gate, blocks, and record function passed in from outside. If nothing is built, the gate has no honest evidence for scalpers, and a strategy could go live on sandbox profits alone.

## Options considered

### Option 1: Sandbox style replay (fill on touch)

Replay the recorded quotes through the same sandbox fill logic the paper loop uses: a limit order fills as soon as the best price reaches it, and a market order fills at the top of the book.

**Pros**:
- Almost nothing new to build; the paper loop's setup carries over.
- Results line up directly with paper results.

**Cons**:
- No queue position: every touched limit order fills, which is exactly the flattering bias the article and spec 0001 warn about.
- No latency, and no walking the book for larger orders.
- It cannot measure missed fills or adverse selection, so it cannot answer feature 15's maker or taker question.

### Option 2: Nautilus `BacktestEngine` per clean segment, with built in queue, latency, and fee models, plus our own fill quality measurement (chosen)

Replay the hourly L2 catalog through a fresh `BacktestEngine` per run of clean hours, with `queue_position`, `liquidity_consumption`, `LatencyModel`, and `MakerTakerFeeModel` switched on. Run the paper strategy classes inside it. Measure fill rate, markouts, funding, and a loser band in pure core code from the replay's own events.

**Pros**:
- The matching logic (queue, latency, partial fills, fees) is the pinned engine's, tested by its maintainers, so we write very little of it ourselves.
- The same strategy classes as paper and live, which is spec 0001's parity rule.
- Measures both maker effects directly, and the optimistic queue bound shows how much the result depends on queue luck.

**Cons**:
- The engine's queue model gives no credit for cancels ahead of you, so it leans pessimistic.
- Bound to the engine's behavior in 1.231.0. A Nautilus upgrade can change fills, so the loser band and the determinism test must be rerun on every bump.
- Python strategy callbacks on a 45 million row day are slow (up to 30 minutes per day).

### Option 3: Our own event driven matching simulator

Write a purpose built L2 matcher in core: our own queue model (crediting a share of cancels ahead), our own latency, and our own fills. Strategies are expressed as pure functions over book events.

**Pros**:
- Full control of the queue model, including cancel credit, which is closer to reality.
- Could be made faster than the Nautilus engine by skipping parts we do not use.

**Cons**:
- A matching engine is subtle (order of events, partial fills, self trade, latency ordering); bugs there invent profit.
- Strategies would no longer be the paper classes, which breaks parity and doubles the strategy code.
- Weeks of work before the first strategy is scored, all of it for one trader.

### Option 4: A vectorized research backtest (DuckDB or pandas)

Compute signals and fills over Parquet columns with SQL or dataframes, using fill rules like "a limit fills if the price trades through it by one tick".

**Pros**:
- Very fast; a month of days in minutes.
- Easy to explore ideas in a notebook.

**Cons**:
- Fill rules over columns cannot model a queue or latency faithfully; it is Option 1 with extra steps.
- Not the paper strategy code, so a result does not transfer.
- Floats creep in through dataframes, against the project's money rule.

## Rationale

The force that decides this is honesty at a seconds timescale. With fees as large as the target move, the result of a scalper is decided by whether its limit orders fill, how late its orders arrive, and what price does next. Options 1 and 4 cannot model those, so they would produce the very flattering numbers the gate exists to reject. Option 3 could model them better in theory, but it means writing a matching engine from scratch for one trader. It also gives up the shared strategy code that spec 0001 chose NautilusTrader for. Option 2 gets queue position, latency, partial fills, and fees from the engine the project already pins and runs. What we write ourselves is the measurement around it (fill rate, markouts, funding, the loser band), which is pure and easy to test.

The pessimistic queue is a deliberate choice. A simulator that errs on the hard side can only reject a strategy that might have worked, while one that errs on the easy side can send a losing strategy to real money. The optimistic variant brackets the truth, so the cost of the pessimism is visible rather than hidden. The same logic set the 25 ms latency: worse than a good London path is likely to be, and replaced by a measurement at the live canary. Segments built only from clean hours follow from spec 0007's gap work. A fill against a book with a known hole could invent profit, and dropping those hours costs a few percent of the data.

Running on the laptop follows from the VPS's 4 GB and its recorder queue pressure (spec 0007 AC-14), and from spec 0001's note that heavy research runs on the laptop. Writing to the laptop Postgres through the existing `write_run_bundle` reuses spec 0002's design. A separate `lab_app` role with INSERT only means a lab bug cannot touch paper rows. The bundle files keep the door open for feature 15 to move results to the VPS later, without opening a database path from the laptop now. The Tardis import ships with the simulator because Tardis days are its main data: own recordings are recent and BTC only, and their main use is the parity check against paper.

### Calls made at write time

- **Random taker size: 1,000 USD notional, not the minimum size.** Kraken's minimum (0.0001 BTC, about 10 USD) pays a taker fee of about half a cent, and commissions are kept at USD cent precision, so rounding could distort the fee by up to 100 %. At 1,000 USD rounding stays under 1 % of the fee. Sizing from notional (not a fixed 0.01 BTC) keeps the order under `max_order_notional` whatever BTC's price was on the replayed day. Milestone 1 task 3 confirms the rounding. Runner up: the minimum size, with the band computed from the charged fees (honest about the check, but unrealistic fees).

### Cross check changes (2026-10-08, a second model's read of the draft)

The draft was read by a different model for missing decisions. These fixes came from it, three of them confirmed in the `kfs/trader:dev` image first:
- `LatencyModel` adds the insert, update, and cancel values to the base (a 25 ms base plus a 25 ms insert stored 50 ms), so AC-5 sets only the base.
- `MakerTakerFeeModel` takes no rates and reads the instrument's fees, so the engine gets an instrument copy with the run's fees, while the `instruments` row keeps Kraken's.
- `BacktestEngine.add_data_iterator` streams sorted chunks from a generator, which is how AC-16 keeps one hour in memory per engine.
- Quotes are synthesized in the data stream (an actor cannot feed the data engine), and the snapshot rows of inner hours are dropped so resting orders keep their queue place.
- Order, fill, and position rows come from a `LabRecorder` over the paper loop's mappers; risk events join the bundle; segments get id tags so ids never repeat in a run.
- The segment end close is submitted by the runner, followed by one synthetic batch so latency does not strand it.
- Tardis ranges require only first of month days; funding is paid at each UTC boundary at the last known rate; fill stats count entries only, by the fill's liquidity side; the parity check skips warm up, block windows, and hours with a different paper config.
- `lab_app` is `NOLOGIN` where its secret is absent, so the VPS migration still passes.
- Fixtures are trimmed 5 minute slices; whole hour and day checks run on real data.
- **No cross product of variants.** Each flag changes one thing against `base`, so three flags give five runs, not twelve, and each difference has one cause. Runner up: the full cross product (it answers interaction questions, at more than twice the runtime).
- **Shared hour writing moves to core.** The Tardis import needs the compactor's book carry and hour writer. Moving them to `trading_core.catalog` gives one writer for both. Runner up: the lab depends on the recorder package (a cross app import, and it would pull recorder code into the lab image).
- **Separate source trees** (`catalog-lab/tardis`, `catalog-lab/own`), because a first of month day can exist in both. Runner up: one tree (it would need to merge or choose between files of the same hour).
- **Quotes from the book.** The recorder stores no quotes, and the paper strategy builds bars from quotes. A quote is emitted when a delta batch changes the top of the book, which is what Kraken's quote feed reflects. Runner up: build bars from trades (it would change the strategy's input from paper, and break parity).
- **Parity tolerance 95 % within 1 s.** Synthesized quotes can differ slightly from the live quote feed near a bar close, so a few decisions can flip on near ties. Anything below 95 % points to a real difference in code or data. Runner up: 100 % (it would fail on legitimate near ties).
- **Loser band of 3 standard errors, at least 0.5 bps.** About 1,400 trades a day make the standard error of the mean small. The floor keeps the check from failing on tiny rounding differences. Runner up: only "total below zero" (it would pass a simulator that undercharges by half).
