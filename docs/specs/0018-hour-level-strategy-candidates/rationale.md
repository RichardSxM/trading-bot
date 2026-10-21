# 0018 rationale: hour level strategy candidates

## Context

Feature 17 is the fallback set: trend following and mean reversion on 1h to 4h candles. The scope row has collected more than a dozen ideas since 2026-10-04 (Brooks price action, chart patterns, several RSI variants, your layered price action, RSI, and MACD plan, liquidity sweeps, volatility contraction). The candle lab (spec 0014) can backtest any `hour_level` strategy on BTC 1 minute history from 2022-03-24 with fees, slippage, and funding. The gate (spec 0015) judges a study on walk forward Sharpe, a one time holdout year from 2025-10-19, deflated Sharpe, PBO, a fee hurdle, a 10% marked drawdown budget, and concentration checks. Only `buy_and_hold` and `macd_baseline` exist as strategies today, and both are benchmarks.

Forces that shaped the design:

- **Gate constraints.** The candle holdout needs 60 trades in about one year. `concentration_direction` fails any strategy where one side makes more than 60% of the holdout profit, so long only can't pass. The drawdown budget is 10% marked to market.
- **Multiple testing.** The deflated Sharpe trial count N is per strategy name. Testing many named candidates and keeping the one that passes is a selection the gate couldn't see.
- **Frozen hashes.** A strategy's `frozen_hash` covers its node module and every `trading_core` module it imports. Shared code ties hashes together, and editing `indicators.py` would move `macd_baseline`'s hash.
- **Look ahead.** Swings, prior day levels, and pattern detection all need confirmed bars; spec 0014's prefix test catches a peek only through the engine.
- **Paper path.** The trader is hard wired to `PlaceholderTimed`, so a pass can't paper trade without trader work.
- **One trader, one laptop.** A candle study takes a few hours; a sequence of six is days, not weeks.

## Options considered

### Option 1: one node class per candidate (copy the `macd_baseline` pattern)

Each candidate is a full `KfsStrategy` subclass with its own order, stop, exit, and decision code.

**Pros**: each hash is fully isolated; no shared base to get wrong once for all.
**Cons**: about 400 lines of order and stop handling repeated six times, each copy a place for a different bug; the candidates' results then differ by plumbing, not only by idea.

### Option 2: shared `BarRuleStrategy` base plus a pure rule per candidate (chosen)

The base owns bars, ATR floor, sizing, stop, exits, and records. Each candidate is a small pure rule plus a thin node module for its own hash closure.

**Pros**: one tested execution path, so candidates differ only by their rule; pure rules are easy to unit test on bar fixtures; new candidates are cheap.
**Cons**: a base fix re-versions every candidate; the base must fit all six rule shapes (it does, since every one decides on a bar close).

### Option 3: one generic configurable strategy (rules as config)

A single strategy whose entry and exit conditions are composed from config blocks (indicator, comparison, threshold).

**Pros**: no code per candidate; a new idea is a config file.
**Cons**: the config language becomes an untested rule engine; combinations explode the grid and N; a config change and a code change look alike in the hash; hypotheses read as JSON, not rules.

## Rationale

Option 2 because the gate compares candidates against each other and against benchmarks, so the plumbing must be identical and correct once. Pure rules match the Clean Architecture rule in `AGENTS.md` and make the look ahead and rule tests simple. Option 3 is clever in the way that fails at 5pm on a Tuesday.

The sub decisions you made, with the runner up:

| Decision | Pick | Why | Runner up |
|---|---|---|---|
| First set | layered plan (2 layers), mean reversion (Connors, Bollinger), breakouts (NR4, failed breakout) | fully rule based, few settings | Brooks and chart patterns, deferred because their detectors need many invented rules |
| Instrument | BTC only | the candle lab has only PF_XBTUSD | extend the lab to ETH first |
| Timeframe | 1h first; 4h only as the one revision | 1h clears the 60 trade floor | 1h and 4h for all (twice the trials) |
| Direction | both, mirrored | the only way through `concentration_direction` | long only with a gate change |
| Sizing | from the stop, 0.5% risk | keeps the 10% drawdown budget reachable only by a long losing streak; matches live | fixed notional |
| Stop | structural, ATR(14) floor | fits each hypothesis, never inside the noise | ATR multiple for all |
| Exits | shared template: stop, target, time stop | one exit vocabulary keeps results comparable | own exits per candidate |
| Orders | market at bar close | every rule decides on a close; pessimistic lab fills | limit entries at levels |
| Trial count | family by strategy kind (and lab, from the cross check) | the selection across candidates is the main overfitting risk here | per strategy, as today |
| Grid | 4 to 6 points | PBO needs 4, N stays low | up to 16 |
| Hypothesis home | umbrella spec, one child per candidate | committed and reviewed before results | study file comments |
| Order | one at a time, Connors first | learn between candidates; the thinnest rule builds the base | all in parallel |
| Layers | context + RSI first, then MACD | RSI pullback stands alone; MACD alone is already the baseline | context + MACD first |
| Regime | plain first, gated later as its own candidate | evidence that a filter helps needs the ungated result | built in from the start |
| Breakout entry | on the close beyond the level | close confirmation built in, one entry path | resting stop entry |
| Connors trend | close against SMA(200) | the classic, one fixed setting | no filter |
| Risk | 0.5% | room for a 20 trade losing streak | 1% |
| On a pass | record; paper is a follow up | the scope's Done when stops at the verdict | trader strategy selection now |
| Verdict home | results section in each child | failures stay in git next to the hypothesis | promotions table only |
| After a fail | at most one revision by amendment | a second chance without tuning on the holdout | verdict is final |

Decisions I settled while writing (no question asked, since they follow from the above):

- **New indicators go in a new module** (`bar_indicators.py`), not `indicators.py`, so `macd_baseline`'s hash doesn't move. Runner up: extend `indicators.py` and accept a new benchmark version.
- **Every signal is a one bar event, consumed when asked, approved or not.** Most of these signals are crosses or first closes beyond a level, which don't exist a bar later; saving them would enter late at a worse price than the hypothesis describes. Runner up: `buy_and_hold`'s attempt counter, which suits a one time entry, not a recurring signal.
- **The base copies `macd_baseline`'s order lifecycle unchanged** (kill switch `flatten`, stop accept timer, exit retry) instead of a new one, since that code already passed spec 0010's drills. Runner up: a fresh state machine designed for the base, which is cleaner but untested.
- **The base is frozen after the first study** (changes go to a `bar_rule_v2` module), because it is in every candidate's hash closure; editing it would silently change candidates already judged. Runner up: allow edits and run earlier studies again, which spends their holdouts a second time.
- **Close out evaluates every study again with the final family N**, since earlier candidates otherwise face a softer deflated Sharpe bar. Runner up: note the asymmetry only.
- **Revisions are labelled "holdout seen"** and may cite only development evidence, since the holdout year that judged the parent judges the revision too.
- **The stop is the absolute decided price**, not an offset from the fill, because each structural level means something. Runner up: `macd_baseline`'s offset from the fill.
- **Family trial variance from stored ratios** (`risk_adjusted_ratio ÷ √365`), not by loading every run's daily equity: same number to 6 places, one small query. Runner up: recompute from `run_daily_equity`.
- **The family is the strategy kind plus the lab**, so it needs no new column or tag, and the trial Sharpe pool never mixes daily candle Sharpes with monthly sampled replay days. Runner up: a `family` field on studies, which needs a migration and invites new families to escape N.
- **Folds and ranges stay the gate defaults** (12 month first in sample, 6 month steps, holdout from 2025-10-19). Changing them per candidate would be one more thing to tune.
