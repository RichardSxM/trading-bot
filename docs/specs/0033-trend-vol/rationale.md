# 0033. trend_vol: rationale

## Context

> ⚠️ Premise note: the ask was "what can be better for us". The honest answer from the research is that nothing found so far is proven better, and `trend_vol` itself fixes ruin without proving an edge (H1 Sharpe about 1.2, H2 0.2 to 0.45, 2026 negative in every variant). So this spec does not promise an improvement. It makes improvements cheap to test and hard to fool ourselves with, and it routes the strategy to a gate verdict that may well be a fail.

`trend_vol` (feature 34) was built on 2026-11-11 straight after a replay showed the live 5x `trend_daily` rule losing about 98% over 4.6 years, and it replaced `trend_daily` on the VPS demo main trader the same night. No spec recorded it. Its sizing (unit vol, book gross) is only in a Redis snapshot that each run overwrites, and in logs, so the forward test has no history to judge.

The project's gate (spec 0015, thresholds v3) caps holdout max drawdown at 10%, counts trials for the deflated Sharpe check, and needs a paper stage on the exact studied version. At its demo target (50% annual book vol) `trend_vol` drew about 55% in the replay. The gate's holdout (from 2025-10-19) was already inside the 2026-11-11 replay window. The trend book family has had about 78 looks since 2026-11-06.

Forces: a public repo (no secrets in it), NautilusTrader pinned and Python 3.14 (anything else on another Python sits outside the main stack), the rule that strategy closure files never change, and a laptop that runs the lab while the VPS runs only the demo.

## Options considered

### Option 1: record as is, add sizing history, pre check rule changes on a pure replay, then study and gate (chosen)

**Pros**: the running version stays comparable; rule ideas cost a replay; the pass rule is fixed in advance; the study judges the version that would go live.
**Cons**: the most work before any result; the likely result is a fail.

### Option 2: build EWMA and tranches straight into a new demo version

The way `trend_vol` itself shipped: write r2 and switch the demo.
**Pros**: fastest to see something new running.
**Cons**: resets the forward test; no clean comparison; most past ideas failed the replay check, so most of this build would be wasted.

### Option 3: record only, and judge the demo by eye

**Pros**: almost no work.
**Cons**: no path through the gate, so live (spec 0028) would need an exception; no sizing history.

### Option 4: adopt Jesse as the research engine

**Pros**: ready made Monte Carlo, optimisation, metrics, and an MCP.
**Cons**: Python 3.10 to 3.13 only; one strategy per route, so no shared 12 coin book; "Kraken Pro Futures" support unconfirmed for our perps; would duplicate the Nautilus lab and gate, which already have a block bootstrap, deflated Sharpe, and PBO.

## Rationale

Option 1 is the only one that answers both forces at once: the forward test needs a history now (so book_plans ships first, outside every closure, so the version and its demo run stay intact), and live needs a gate verdict on a specific version (so the study runs at a target the bootstrap shows fits the 10% drawdown cap, and on a pass the demo switches to that version for the paper stage). The target is a sizing choice, not the edge (Sharpe was flat from 20% to 65%), so studying at about 8% does not change what is being tested; changing the gate's drawdown limit after seeing a result would break the thresholds file's own rule.

book_plans gets its own write path because `RecordingWriter` fails closed: a dead lettered item blocks entries until restart. That is right for orders and fills, but a missing sizing record must not halt a book with no stops; the owner chose "trading goes on, retry, alert", and the alerter covers a lasting gap.

Jesse is used for what it adds that we lack, not as an engine: its candle based Monte Carlo (moving block bootstrap) becomes our block bootstrap reruns, which rerun the rule on new price paths, which the gate's bootstrap (it resamples the strategy's returns) never does; its benchmark table becomes ours, with plain baselines added to expose beta. A laptop Jesse install is kept as an extra engine on single coin legs, and its MCP is allowed for ad hoc looks at the owner's request, but neither is a pass condition for the book; the book level second engine is our own independent reimplementation.

The cross check (Fable, 2026-11-14) found 19 gaps, all resolved in the spec with the owner's agreement: chiefly the lab's lack of a book mode, the thresholds code only supporting v1 to v3, a tsmom style 2x risk block, the holdout trade count, an unfrozen book_plans row, and an undercounted ledger.

Tranches average the cut hour instead of picking one, so they do not repeat the closed run hour search; still, a 7 day staggered weekly variant already failed on 2026-11-10, so their prior is low, and their node design (hourly closes, seeds) is left until they pass.

### Recommendations made while writing

- book_plans written by its own `write_book_plans` task with `asyncio.to_thread`, the row frozen on first sight and retried by tick (runner up: a second `RecordingWriter` instance with entry blocks off, more code for the same effect).
- A Postgres based staleness alert (newest row older than 24 h 20 min) instead of a snapshot comparison: it survives restarts and snapshot expiry and catches a missed daily run (runner up: the snapshot versus row check, which a restart defeats).
- Parity per day return within 10 bps and cumulative within 1 point, on band 0.3 and band 0 (runner up: 0.5% of equity every day, which compounds fill time differences and one band edge decision over 182 days).
- An independent Decimal reimplementation of the whole book as the main second engine, because a single coin Jesse leg exercises neither book sizing, the band, nor funding, where a replay bug would sit; Jesse stays as the extra single coin check the owner chose.
- Fill at the H:05 one minute close, never the signal's own bar (look ahead otherwise).
- The tranche baseline is the mean of four cut hours, since K = 4 is roughly that mean by construction, and a single 00 baseline would decide the pass by where 00 happens to sit.
- 1000 bootstrap reruns on 120 day blocks with one path set reused across targets, sensitivity at 60 and 180 (runner up: 60 day blocks, which cut through the 30 day lookback on about half the days and dilute the trend).
- The precheck rerun guard keyed on a name in a committed ledger (runner up: a file hash, defeated by a whitespace edit).
- The study's risk block follows the 5x cap (`leverage` 5, `max_leverage` 9, exposure 50000 on 10000), not `tsmom.toml`'s 2x, which would clip the book.
- Study grid `lookback_days = [20, 30, 40, 60]`, the same neighbours tsmom used (runner up: a band grid, less tied to the edge).
- Binance 2018 to 2022 shown for information in the pre check, not a pass condition (it was seen on 2026-11-07 for the 30 day portfolio).
- AC-9a (2026-11-16, your picks of 2026-11-15): DOGE and TRX keep the tick of their time through a `_tick_history` pin key, checked per candle minute (runner up: round old prices to today's tick at fetch, which stores prices that never traded and hides a wrong history date). The QNT glitch is removed by a `_drop_spans` pin key and stored as an ordinary gap (runner up: rounding the 19 minutes like AC-2a, which would keep 19 copies of one stuck print as real closes). Both live in the pin as data, so the one coin they touch is the only coin that changes, and a new case is an amendment, not a looser `OFF_TICK_BODY_MAX`.
- AC-9a edges (2026-11-16, second amendment): the refetch on the first values stopped all three coins, as AC-9a asks. DOGE and TRX each refused 22 candles, 2026-08-20 07:00 to 07:21 UTC, all still on the old tick; the raw candles show the 07:21 candle opening on the old tick and the first trade wholly on the new tick at 07:22, so `until` moves to 07:22. QNT refused the quiet candle at 22:03 (flat at 93.91 against 96.05): the raw candles show a real move to 93.91 at 22:01 inside the glitch and three quiet minutes after it, so one more minute would only move the refusal to 22:04. The span ends at 22:05, the first candle that trades (runner up: let the quiet check compare against the first candle after a span instead of the last one before it, which changes a rule every coin uses for one coin's three minutes).

- AC-12a (2026-11-17, your picks): the owner asked to improve trend_vol with take profits, a stop, a better trend read, and better entries. A scratch replay that day (Kraken hourly closes, 12 coins, 2022-11 to 2026-11, funding, 7 bps; the same bar as this spec) tested each on its own at four cut hours. Take profits (+8, 12, 16 entry vols) and pullback entries lost at every hour. A stop at 5 or 8 entry vols was about neutral, and 3 hurt. A vote of 20 to 120 day trends helped the second half and hurt the first (a regime effect, like aside). Scale in over 2, 3, or 5 days was the one part that raised end value and cut drawdown at 00 UTC for all three. Scale in plus an 8 vol stop, picked after seeing those results, ended higher at all four hours (5.21x vs 3.18x at 00) and drew down less at three. It still missed the scratch bar at 12 UTC, so it enters the lab pre check as a trial instead of a build. Neighbours move both knobs, since a stop width picked from three values is as likely to be luck as the ramp length (runner up: the scale days only, which never tests the stop). The stop is checked on hourly closes with the existing H:05 fill, matching the scratch test and a node that can check hourly (runner up: every one minute close, which differs from the evidence and costs more to run in the trader). Every scratch look of the day goes into the ledger, as this spec's rule says. A Sonnet cross check the same day found 19 gaps, all applied on your pick: chiefly the run time mismatch (the main trial now runs at all four cut hours against the same hour baseline, since 00 was its best scratch hour), a `scale3_nostop` control (the stop was neutral in scratch but brings all the node state), the stop and rebalance order at the cut hour, no stop until the first fill, the ramp following the signal side, and turnover, gross, and stopped share report lines.

### Trial ledger (source of `prior_trials = 191`: tsmom's 13 plus 178 below)

Looks at the 30 day trend book family on real data, from the research notes, plus every run this spec makes on real data:
| Date | Look | Trials |
|---|---|---|
| 2026-11-06 | slow ideas: trend portfolio L30, L60, L90, blend | 4 |
| 2026-11-07 | old data (Binance 2018 to 2022): L30 portfolio | 1 |
| 2026-11-09 | hourly trend variants (6) and weekly rebalance day shifts (7) | 13 |
| 2026-11-10 | slow variants A to E | 5 |
| 2026-11-11 | book sizing replay (vol targets, bands, caps, blends, brake) | 35 |
| 2026-11-12 | trend_vol exits: aside, trailing, take half | 8 |
| 2026-11-14 | aside 5 to 9 at 00 UTC, aside7 at 06, 12, 18, base at 06, 12, 18 | 11 |
| 2026-11-14 | tradfi sleeve | 1 |
| 2026-11-17 | Turtle 55/20, 40/15, 70/25, 20/10 and strength at 00 UTC; Turtle 55/20 and strength at 06, 12, 18; Turtle blend | 12 |
| 2026-11-17 | Binance/Yahoo 2018 to 2022: Turtle, blend, strength | 3 |
| 2026-11-17 | stops, take profits, trend votes, scale in, pullback entries: 15 parts plus the full system at 00 UTC (16), and the 5 main parts plus the full system at each of 06, 12, 18 (18) | 34 |
| 2026-11-17 | scale in plus stop combos (scale3 and scale5 with stop 8) at four cut hours | 8 |
| this spec | parity window on real 2024 candles (AC-10a) | 1 |
| this spec | pre check: EWMA 10, 20, 40; tranches 2, 4, 8 | 6 |
| this spec | tranche start offsets 00 to 05 (AC-12) | 6 |
| this spec | pre check: scale3_stop8 at 4 cut hours, its 4 neighbours, and the scale3_nostop control (AC-12a) | 9 |
| this spec | Binance 2018 to 2022 information runs, one per trial (6 EWMA and tranches, 6 AC-12a) | 12 |
| this spec | study target levels 4, 6, 8, 10, 12% | 5 |
| this spec | block length sensitivity (60, 180 days) | 2 |
| this spec | candidate pick, `trend_vol` versus `trend_vol_r2` | 2 |
| | **total** | **178** |

The four baseline cut hours are inside the 2026-11-14 row (base at 06, 12, 18) plus the 00 base of 2026-11-11. The study's own grid points are counted by the gate itself, on top of these. The candidate pick's 2 count even when r2 is not built, so the total never moves after the study file is written.

### Research evidence

- Volatility targeting raises Sharpe for risk assets such as equities and credit, but adds little for bonds, FX, and commodities (Harvey et al. 2018); the classic mechanism (Moreira and Muir 2017) relies on volatility not being offset by expected returns, and Liu and Tsyvinski find little evidence that realised volatility predicts crypto returns. So the benefit is unproven in our universe, which matches the H2 weakness.
- Rebalance timing luck is large in equities (Newfound), and tranching averages it out, but it can behave like an added momentum overlay rather than pure noise removal.
- EWMA (about a 20 day half life) is the common estimator in the vol targeting literature; no crypto head to head result was found.
- Jesse (docs read 2026-11-14): MIT licensed; Python 3.10 to 3.13; routes are one symbol and strategy each; candle based Monte Carlo with moving block bootstrap and Gaussian noise pipelines; a benchmark that compares configurations with Sharpe, Sortino, Calmar; a rule significance bootstrap on one signal; an MCP served by a running Jesse install.

## References

**Project sources**:
- `AGENTS.md` (Clean Architecture, demo default, secrets as files, no floats) · `packages/core/AGENTS.md` (pure `lab/` and `strategy/` folders, repository writes, migrations)
- Spec 0015 (gate), 0022 (seen holdout label), 0027 (trend kind, prior trials), 0030 (trend portfolio), 0032 (trend daily)
- `packages/core/src/trading_core/gate/thresholds.toml` v3 · `recording/writer.py` (fails closed)

**Practices & standards**:
- Pre registration and counted trials (deflated Sharpe) · strangler, additive migrations · moving block bootstrap · rebalance timing luck

**Links** (web verified):
- Harvey et al., The Impact of Volatility Targeting (summary): https://quantpedia.com/the-impact-of-volatility-targeting-on-equities-bonds-commodities-and-currencies
- Moreira and Muir, Volatility Managed Portfolios: https://www.nber.org/papers/w22208
- Newfound, Tranching, Trend, and Mean Reversion: https://blog.thinknewfound.com/2020/04/tranching-trend-and-mean-reversion/
- Newfound, The Dumb (Timing) Luck of Smart Beta: https://blog.thinknewfound.com/2019/11/the-dumb-timing-luck-of-smart-beta/
- Jesse docs: https://docs.jesse.trade/docs/getting-started/ · https://docs.jesse.trade/docs/monte-carlo/candle-pipelines · https://docs.jesse.trade/docs/backtest/benchmark · https://docs.jesse.trade/docs/optimize/overfitting · https://docs.jesse.trade/docs/routes/ · https://docs.jesse.trade/docs/supported-exchanges/ · https://docs.jesse.trade/docs/mcp/ · https://docs.jesse.trade/docs/mcp/research-runners
- Jesse repository: https://github.com/jesse-ai/jesse
- Liu and Tsyvinski (crypto momentum and volatility) and Baltas and Kosowski (trend estimators): cited by name, links not verified.
