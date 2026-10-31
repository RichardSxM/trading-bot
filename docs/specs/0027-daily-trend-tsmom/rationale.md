# 0027. Rationale

## Context

> ⚠️ Premise note: the gate rules for `trend` were chosen on 2026-10-31, after the development pre check showed which rule worked and why today's gate fails it. That is exactly what `thresholds.toml` warns against ("never after looking at a result"). The holdout is still unseen for this rule, so its verdict keeps meaning. But the rules were fitted to a known winner's shape, so a pass is weaker evidence than one under a gate fixed in advance. The right framing is a pass that earns a paper run on demo, not proof. Every check that tests luck stays, and N pays for the search.

Every candidate so far failed on cost or on a negative gross: seconds scalping (spec 0017), hour level (0018), 15 minute swing (0019), intraday trend (0020), the BTC plus ETH portfolio (0022), and donchian_scalp r1 and r2 (0025, 0026). On 2026-10-31 four more scalping pre checks failed the same way: the breakout fade, 1 minute mean reversion, and the analysis of donchian_scalp's live streak with trend filters.

The one horizon left untested was daily bars across several coins, where costs of 14 bps a round trip are small next to the moves. A pre registered pre check of 10 rules on BTC, ETH, SOL and XRP (development data only, 2022-04-01 to 2025-10-18) found one rule that cleared every bar.

Today's gate (spec 0015, v2) has two checks that a long only trend follower cannot pass:
- `concentration_direction` caps the long (or short) share of holdout profit at 60 %, and a long only rule is at 100 % by construction.
- `concentration_top_trades` caps the top 5 % of trades at 60 % of profit, and trend following earns its profit in that tail.

Constraints: the gate's thresholds are frozen per study, so existing verdicts must not move. `recording/enums.py` sits inside demo strategies' code hash closure. The holdout for BTC and ETH was already looked at by specs 0020 and 0022. SOL and XRP have no lab data yet.

## Options considered

### Gate: how a trend strategy is judged

**Option 1: a new `trend` kind with fitting rules (chosen).** Add a kind and key every new rule on it.
- Pros: scalping and hour level verdicts and N stay exactly as they are. The trend family gets its own N.
- Cons: a migration and an enum change (r2's code hash moves, so a manual promote). The rules come after the evidence.

**Option 2: reuse `hour_level` and relax its checks.**
- Pros: no migration, no new kind.
- Cons: it changes the rules for a family with 27 recorded trials and verdicts, and it mixes daily trend trials into the hour level N.

**Option 3: a per strategy flag naming which checks apply.**
- Pros: the most flexible.
- Cons: easy to game, because each strategy picks its own bar. The gate would have to trust strategy code it does not read.

### Top 5 % trades check for `trend`

**Drop it and rely on the bootstrap and DSR (chosen).** Pros: it tests luck directly instead of a shape that is normal for trend following. Cons: one huge trade can carry a year, guarded only by the bootstrap lower bound and the 60 trade floor.
**Top 1 % at 60 %.** Pros: keeps a tail check. Cons: the same arbitrary limit, and it still fails some development windows.
**Keep it.** Pros: no change. Cons: `tsmom` fails by construction.

### Candidate

**`tsmom` long only, grid 20 / 30 / 40 / 60 days (chosen).** The only long only rule that passed. Its neighbours behaved the same.
**`tsmom` long and short.** Pros: more trades (124 a year). Cons: drawdown 12.2 %, and the top trades share stays above 1.
**Donchian 20/10 or 55/20 long only.** Pros: high gross per trade. Cons: 12 to 24 trades a year, far under the 60 holdout floor.

## Rationale

The direction check measures whether profit leans on one side. For a strategy that can only be long, it can't tell luck from design, so skipping it only for a declared long only strategy removes no information. The `long_only_violated` check makes sure the declaration can't hide a short. The top 5 % check fails every one year window of `tsmom` (0.92 to 24) and of the long and short variants. That's the signature of trend following, not of luck. The bootstrap lower bound and the DSR test luck directly, so they take its place. N gets the 13 trials run outside the database (10 pre registered rules, 3 robustness lookbacks), so the search is paid for.

Keeping `beats_buy_and_hold` hard is the natural bar for a long only coin strategy: if it can't beat holding the same coins on Sharpe, it adds nothing. The 60 day, 8 trade paper floor gives enough closed trades to compare live fills and slippage with the lab at about 5 trades a month, without waiting a quarter. It is built with the paper follow up, not here, because nothing in this spec reads it. A new kind (Option 1) is chosen over reusing `hour_level` (Option 2), because 27 recorded hour level trials and their verdicts must not move.

## Cross check (2026-11-01)

A read only pass on another model checked the draft against the code. You applied every recommended fix:
- The time stop went from 365 to 30 bars. The risk check budgets funding for each hourly boundary in the planned hold (`risk/funding.py`), so 365 daily bars meant 8,760 periods and positions about half the size.
- The trend warmup pre roll was added (child 1 AC-10a). `lab holdout` started cold, which would have cost 21 to 61 days of a one year holdout.
- The paper floor fields moved to the paper follow up. They sat in a nested table `_ADDED_IN` doesn't cover and had no reader yet.
- The pre check is rerun as built (AC-18a) before the study file.
- The grid winner rule is stated: no second pick.

Smaller precision fixes went in with them (the `lookback_days` default, the run file line, `missing_metric`, the daily bar timing, the pin field mapping, the data end date, crashed holdouts).

## Pre check evidence (2026-10-31, development data only)

Script: session scratchpad `daily_trend_precheck.py`, with the data from Kraken's public charts and funding APIs.
- Costs: 7 bps a side.
- Funding: longs pay 3 bps a day, about three times the last year's mean.
- Sizing: 0.5 % risk on 2 ATR(20), with no stop for the momentum rules.

| Rule | Trades a year | Gross a trade by half (bps) | Max drawdown | Sharpe | Pre check |
|---|---|---|---|---|---|
| tsmom 30 long only | 61.8 | +492 / +417 | 7.7 % | 1.29 | pass |
| tsmom 30 | 123.6 | +167 / +130 | 12.2 % | 0.61 | fail |
| donchian 20/10 long only | 24.2 | +488 / +1082 | 6.9 % | 0.95 | fail (trades) |
| donchian 55/20 long only | 12.4 | +1318 / +984 | 6.9 % | 0.95 | fail (trades) |
| tsmom 90 long only | 34.2 | +909 / +315 | 12.3 % | 0.86 | fail |
| ema 20/100 long only | 9.1 | +3304 / +623 | 11.4 % | 0.80 | fail |
| the other four long and short rules | 19 to 69 | | 10 to 22 % | | fail |

Robustness (long only, read after the pass, counted in N): tsmom 20 passed (83 trades a year, drawdown 10.0 %). tsmom 40 and 60 were as profitable but missed on trade count (53 and 50) or drawdown (11.6 %).

Per coin net USD (tsmom 30 long only): BTC +1,123, ETH +645, SOL +1,248, XRP +809. In the first half, buy and hold lost 7 to 30 % per coin.

Trade shape: the median trade lost. Without the top 10 trades (of 204), the mean is −53 bps.

Gate concentration on one year windows of the development data:

| Window from | Trades | Top 5 % share | Direction share |
|---|---|---|---|
| 2022-04-01 | 57 | 1.45 | 1.00 |
| 2022-09-30 | 66 | 24.26 | 1.00 |
| 2023-04-01 | 50 | 0.92 | 1.00 |
| 2023-09-30 | 65 | 0.93 | 1.00 |
| 2024-03-31 | 61 | 1.44 | 1.00 |
| 2024-09-29 | 60 | 1.08 | 1.00 |
