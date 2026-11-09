# 0030. trend_portfolio: rationale

## Context

> ⚠️ Premise note: you asked for the portfolio to run **beside** six vote, and most of the work that asks for is not the strategy. The trader was built as a single process (one consumer group, one halt key, one set of snapshot keys, one loss tally per environment), so a second trader touches the control stream, a table, the loss limits, the alerter, the deploy script, and the dashboard. The cheaper path is to let the portfolio replace six vote on demo, as every earlier switch did. That was offered and you chose side by side, so this spec does it properly and keeps the `main` trader byte for byte unchanged. Part A (the strategy) is built first and smoke tested alone, so you can still stop after it if the plumbing turns out to cost too much.

From 2026-10 to 2026-11-06 about 900 backtest trials of scalpers, hour level rules, fades, and event trades all failed on fresh data after fees (see memory notes and specs 0016 to 0029). On 2026-11-06 a slow idea was tested: hold every coin long or short by the sign of its 30 day return, scale each by its volatility, and rebalance weekly. On the Kraken data (2022-03 to 2025-10) it narrowly missed its pre registered bar (Sharpe 0.69 against 0.8 needed), then made Sharpe 1.04 on the fresh year. On 2026-11-07 it was tested, unchanged, on data it had never seen: free Binance daily candles from 2018 to 2022-03, with Binance funding where it exists. It passed its pre registered rule (Sharpe ≥ 0.5 and at least 3 of 4 years positive) with room to spare.

The demo trader today runs one strategy (`PAPER_STRATEGY`) on one sandbox account in netting mode (Nautilus keeps one net position per coin). Six vote trades the same 12 coins on 1 hour bars, so the two would fight over the same net position if they shared an account. Kraken has had no futures demo since 2026-07 (spec 0001 amendment), so "an account" on demo is a Nautilus sandbox inside each trader process: a second process gets a second account for free.

Forces: keep six vote's forward test undisturbed; keep every safety property (one stop button, alerts, the promotion guard, loss limits per strategy); stay demo only until a gate verdict exists; no edits to frozen strategy modules (spec 0029 AC-11); free data only.

### Evidence (scratchpad of session 25e035c4: `slow_ideas.py`, `old_data.py`, `port12.py`, their logs)

Costs 6 bps a side (taker 5 + 1 slip). Funding: Binance from late 2019, Kraken from 2025-10-30; none before. Yearly figures are simple sums of daily returns.

| Data | Coins | Sharpe | Years | Max drop | vs holding coins |
|---|---|---|---|---|---|
| Binance 2018-01 to 2022-03 | 19 | 1.17 | 2018 +42%, 2019 +11%, 2020 +25%, 2021 +58% | −26% | corr −0.10 |
| Binance 2018-01 to 2022-03 | the 12 demo coins (11, no HYPE yet) | 1.36 | 2018 +43%, 2019 +19%, 2020 +29%, 2021 +69% | −23% | corr −0.05 |
| Kraken 2022-07 to 2025-10 | 12 demo coins | 0.70 | 2022 −20%, 2023 +19%, 2024 +41%, 2025 +16% | −36% | corr +0.04 |
| Kraken fresh 2025-10 to 2026-11 | 12 demo coins | 1.08 | +28% | | |

Holding all coins long, for comparison: 2018 −79% to −100%, Sharpe 0.79 to 0.98 overall, fresh year 0.32. The 60 day, 90 day, and blended versions were weak (dev Sharpe 0.08 to 0.39) and are not used. The trial count for this family: 4 portfolio versions on the Kraken data, then one rule on the Binance data.

## Options considered

### Option 1: a second trader service with its own sandbox account (chosen)

A `trader-portfolio` compose service runs the same image with `TRADER_SLOT=portfolio`. The slot separates its snapshot keys, consumer group, halt key, and loss tally. Both traders read every command.

**Pros**:
- No clash: each strategy has its own net positions, equity, and P&L.
- Six vote's trader keeps every key and behavior as it is, and a slot test pins that.
- Restart safety, reconciliation, and the kill switch keep working per process, as built.
- Live later needs only a second Kraken account per slot, not a redesign.

**Cons**:
- Touches shared parts: the control consumer, a table migration, the loss seed, the alerter, the deploy script, and the dashboard.
- Two processes use about 1 GB more memory on the VPS.
- The top bar's command state still follows `main` only.

### Option 2: both strategies in one trader, one account

Add the portfolio as a second Nautilus strategy in the same node.

**Pros**:
- No new service, no slot, no control changes.

**Cons**:
- The account nets the two: six vote long BTC and the portfolio short BTC become one smaller position, and each strategy's exits act on the other's size.
- Reconciliation (spec 0023) cannot tell whose position is whose after a restart.
- P&L cannot be split cleanly. Unsafe, so rejected.

### Option 3: one trader, one combined target per coin

Sum the portfolio's target and six vote's wanted position into one order per coin.

**Pros**:
- One account and one process, and it is how a real fund would net its books.

**Cons**:
- A large rewrite of six vote's order logic (its stop and trail act on the whole net position), which breaks spec 0029's frozen modules.
- The P&L of the two can no longer be compared, which is the point of running both.

### Option 4: the portfolio replaces six vote on demo

**Pros**:
- The least work by far: Part A only, and a one line `PAPER_STRATEGY` switch, as every earlier switch did.

**Cons**:
- Ends six vote's forward test, which you are watching.
- You asked for both, side by side.

### Option 5: a second trader in its own key namespace (raised by the cross check)

Run `trader-portfolio` with its own Redis database or a key prefix, so every key, stream, and group is separate without threading a slot through the code.

**Pros**:
- Far less threading: no slot module, no consumer group, no result field, no migration.

**Cons**:
- The stop button no longer reaches both traders: the dashboard writes to one control stream. You chose one button for both.
- The dashboard, alerter, and deploy script still need to read the second namespace, so much of Part B stays.

## Rationale

Option 1 is the only one that keeps both forward tests clean, which is the whole point of running them side by side: separate money, separate positions, separate P&L. Options 2 and 3 share one account, and Kraken netting (simulated the same way in the sandbox) makes the two strategies interfere. Option 3 would also break spec 0029's frozen modules. Option 4 is cheaper, but it drops what you asked for.

The cross check on 2026-11-07 found that the first draft missed the shared pause key, the result readers, the dashboard's P&L reads, the exchange snapshot, a deploy pause that would leave the book empty for a week, and risk caps that would shrink the book to a quarter. The revised spec closes each one. It also found that several control files sit inside every strategy's version closure, so the slot names live in a new module, and those files stay unedited.

Most of Option 1's cost is in places built for one trader. The design keeps that cost safe by making `main` the default slot, unchanged byte for byte, so six vote's run cannot regress, and by putting every shared change behind the `portfolio` slot, the compose profile, or a setting. One stop button still stops both, because each trader reads the stream in its own consumer group and halts through its own key. Loss limits stay per strategy by scoping the seed to the trader's own runs.

The strategy follows the backtest exactly where it matters: the signal, volatility, weights, gross cap, Monday rebalance after Sunday's close, market orders, and no stops. It departs only where live trading forces it, and each departure is listed in Consequences: the start rebalance after a restart, the risk caps, and the retries. Going straight to the demo matches your choice for six vote. The gate stays owed before live, and the `trend` gate kind from spec 0027 already exists for it.

Small calls made here (runner up in brackets):
- Daily bars built from trades plus a seed, as six vote does [fetching candles at each rebalance: simpler, but a Kraken outage at 00:05 blocks the week].
- Flips as a reduce only close, then an open [one netting order: fewer fees, but it breaks the rule that every exit is reduce only].
- The failed coins alert through the alerter reading the plan snapshot [a new risk event kind: the alerter does not page on risk events today].
- `TRADER_SLOT` as a named enum [a free prefix setting: more flexible, but easy to mistype into a key clash].
