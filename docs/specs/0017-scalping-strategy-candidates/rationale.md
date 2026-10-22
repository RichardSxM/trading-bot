# 0017. Rationale: scalping strategy candidates

## Context

> ⚠️ Premise note: the scope says the bot "scalps on a seconds timescale". The spec 0015 gate makes that very unlikely to pass. A taker round trip costs about 10 bps, and the fee hurdle wants 1.5× that in average gross edge per trade (about 15 bps), while BTC rarely moves that far within seconds. So this feature aims for holds of 1 to 15 minutes, still short and still scalping by the scope's meaning, and every candidate enters post only (about 10.5 bps hurdle). Even so, expect most verdicts to be `fail` or `insufficient`. The value of the feature is honest recorded verdicts, not a guaranteed winner.

Feature 16 needs a small set of scalping ideas, each with a gate verdict. Spec 0015 already fixes how they are judged. A replay study uses only the free Tardis 1st of month days (43 development days from 2022-04-01, a 12 day holdout from 2025-11-01). The holdout needs 200 trades (about 17 a day). Replay grids are capped at 4 points. Each candidate's frozen hash spends its holdout once.

Compute is the binding cost. The engine replays about 19,000 events a second, so one grid point over the 43 development days takes about 10 hours with 2 workers, and a 4 point study about 40 hours, plus about 6 for the holdout. The candle lab, by contrast, runs years of 1 minute candles in hours, but it has no order book, no queue, and no trade aggressor side.

Evidence so far is sobering. `maker_probe` (no edge by design) filled 61% of its post only entries on 2026-09-01 and still lost, so missed fills and adverse selection are real in the simulator. The scope carries about ten ideas from articles with no fee accounting, and a 2026-10-17 note asks for a data audit first, a written hypothesis per candidate, and one candidate at a time.

The workspace is `packages/core` (pure strategy logic in `strategy/`, thin Nautilus classes in `node/`) plus `apps/lab` (registry, run and study files), with the gate on the VPS. The candle lab today refuses any strategy whose kind is not `hour_level`.

## Options considered

### Option 1: Broad sweep of every scope idea

Build all ten or so ideas, run each taker and maker, with and without the regime gate, and let the gate sort them.

**Pros**:
- Most coverage; nothing is left untried.

**Cons**:
- At about 40 hours per replay study, forty studies is months of laptop time.
- Every attempt adds to the trial count, so the deflated Sharpe of any eventual winner sinks. A broad sweep is a recipe for one lucky pass.

### Option 2: An audited, screened, one at a time set of three on one shared framework (chosen)

A data audit fixes the spread cap and the order. Three ideas (two bar based, one order flow) share one strategy base. Bar ideas first pass a cheap candle screen with a rule written in advance, then every survivor gets one maker replay study and a gate verdict, with one written revision allowed.

**Pros**:
- Compute goes to ideas that can clear the fees; the screen and audit cost hours, not days.
- One base means the candidates differ only in their signal, so they compare like for like, and a passing one is already a trader ready class.
- Few trials per candidate keep the deflated Sharpe meaningful.

**Cons**:
- A screen pass is not evidence (the candle lab is pessimistic and optimistic in different ways), and screen runs still count as trials.
- Choosing the entry mode per hypothesis means no taker against maker comparison per candidate.

### Option 3: One candidate only, the rest later

Design and run only the first candidate now; each later one gets its own `/architect` run.

**Pros**:
- Smallest spec and build.

**Cons**:
- The shared framework would be designed around one idea and reworked for the next.
- Leaves the feature's done line (every candidate has a verdict) open for a long time.

### Option 4: Seconds timescale order book scalping

Book imbalance and trade flow bursts, entered taker, held seconds, as the scope first described.

**Pros**:
- Matches the scope's original wording, and it is where the replay's queue and latency modelling matter most.

**Cons**:
- The fee hurdle (about 15 bps per taker round trip) is far beyond a typical few second move, so it is very likely to fail on cost alone.

## Rationale

The forces are the fee hurdle, the 40 hour study, and the trial count. Option 4 loses to the first: the hurdle sets the horizon, so holds of 1 to 15 minutes and maker entries are the only region where a pass is plausible. Option 1 loses to the second and third: it spends months and inflates N until a pass means little. Option 3 saves design time but defers the shared base, which is the part that makes later candidates cheap and comparable.

Option 2 spends cheap evidence first. The audit runs on the development days only (never the holdout), so it can set the spread cap and warn if typical moves cannot reach the hurdle, without leaking holdout information. The candle screen uses years of data to discard bar ideas that fail on fees or trade rate, with the same `fee_hurdle_k` the gate uses and a trade rate floor that the 200 trade holdout implies. Only then does a 40 hour replay run, and only its gate verdict counts.

Each recommendation made while writing, with its runner up:
- **Shared base with pure signal functions**, over one class per candidate with its own order handling: the order lifecycle (maker timeout, partial fills, stop sizing, exits) is where bugs live, so it is written and tested once. Runner up: extending `macd_baseline`'s node, rejected because editing it changes a benchmark's hash.
- **New `scalp_indicators.py`**, over adding to `indicators.py`: editing `indicators.py` would change the frozen hash of `macd_baseline` (a benchmark and a study subject).
- **ADX on 5 minute bars**, over 1 minute: a 1 to 15 minute hold needs the regime over a longer window than its own bars; 1 minute ADX flips too often. Cost: about 140 minutes of warmup per replay segment.
- **Spread cap at the audit's p95**, over a grid point: it blocks only the worst 5% of moments, uses no grid slot, and is measured rather than guessed. The candle screens leave it unset because the candle lab's spread is synthetic (twice `slippage_bps`) and would block everything.
- **Reward floor of 10.5 bps**, over none: a target nearer than the maker hurdle cannot pay for itself even when it hits, so such signals are refused up front, by a fixed rule.
- **All exits taker**, over resting limit targets: simpler and pessimistic; a maker target is listed as a possible revision.
- **Results in `verify.md`**, over a table: they are read by people a handful of times, and `promotions` already holds the machine readable verdicts.
- **Screen exit codes 0 and 3**, matching `gate check-version`, so scripts can tell a fail from an error (1 or 64).

## Amendment 2026-10-22: stop already through at the fill (AC-18)

The first `bollinger_rsi_screen` run showed the gap. In the candle lab a maker entry fills inside a bar, and the next quote can already sit past the stop. The venue rejects a stop market order whose trigger is in the market, so spec 0006's `STOP_NOT_ACCEPTED` block fired. That block clears only on restart, and a lab run never restarts, so one early rejection switched entries off for the rest of a 2.8 year run (grid-1 made 10 trades, grid-2 made 9). The screen would then fail on trade rate for a reason that has nothing to do with the idea.

Options weighed with the engineer:
- **Check the quote and the fill price before placing the stop (chosen)**: if the stop is already through, exit at market with reason `stop_through` and add no block. This is predictable market movement, not a bug, so it should not need a human restart. It also holds in the trader, where a fast move between the fill and the stop gives the same result.
- **Copy spec 0018 AC-3 exactly** (check the average fill price only): runner up. It is consistent with 0018 but misses every case seen here, where the fill was above the stop and the bid had already moved below it.
- **Lab only, no latch**: rejected. The lab would no longer replay the trader's exact code, which is the point of both labs sharing one strategy class.

A real rejection after the check passed still blocks, so fail closed holds for anything unexpected. The edit changes `scalp_base.py` only, so it changes the frozen hash of the three scalping strategies and no other.

## Amendment 2026-10-22 (second): the rejected stop verdict (AC-18)

The rerun `bollinger_rsi_screen_v2` latched the same block 4 times in its first 25 minutes, once per grid point, while the new check fired only once. The cause: Nautilus's backtest engine hands each quote to the simulated venue before the strategy. A quote that fills the maker entry also moves the venue's book past the stop, but the strategy's cached quote is still the one before it, so the fill check sees room and the venue rejects the stop (`bid=22955` against a stop of 22966). A live venue can be ahead of the strategy's quote the same way.

Options weighed (the engineer asked for the work to continue on the recommendation):
- **Let the next quote settle a rejection (chosen)**: the market exit goes out at once as before, entries stay refused, and the first quote after the rejection, the one the venue already used, decides: through means `stop_through` with no block, otherwise the spec 0006 block and critical risk event. No reason text is parsed, and the block is never off while the answer is open.
- **Match the venue's reject reason text** ("in the market"): rejected. The text is the simulator's, Kraken's differs, and a wording change would silently turn the block back on or off.
- **Hold the stop back until the next quote**: rejected. It leaves a live position with no stop for a moment, and with no stop at all on a stale feed.

A denial, a cancel by someone else, and a stop not accepted in time still block at once, so fail closed holds for everything the quote cannot explain.

## Amendment 2026-10-22 (third): the quote already seen at the rejection (AC-18)

Recorded in the index (AC-18, task 12d) and in `verify.md` (the `bollinger_rsi_screen_v3` void record).

## Amendment 2026-10-22 (fourth): cheaper exits, a gate path for slow scalpers, a fourth candidate (AC-19 to AC-24)

What the screens showed (`verify.md`): `bollinger_rsi_screen_v4` passed the walk forward Sharpe (0.068) and the fee hurdle (edge 2.36 against 2.09 USD a trade) and failed only on trade rate (0.31 a day). The ADX version and `vwap_retest_screen` failed on Sharpe too, and `vwap_retest`'s edge (1.08) did not cover even one round trip's cost. `delta_divergence` failed its trade rate pre check (2.75 a day). The engineer asked for the candidates most likely to pass, still scalping, and for the work to go ahead on the recommendations.

Two findings drove the amendment:
- **The trade rate floor belongs to the replay holdout, not to scalping.** 17 a day is the ceiling of 200 ÷ 12: the replay holdout is only 12 free Tardis days. The candle lab's holdout is a full year with a floor of 60 trades (`thresholds.toml` `[candle]`), which `hour_level` studies already use. A scalper that trades a few times a week can be judged there with the same gate.
- **Costs, not signals, are the main wall.** Every exit was a market order, so a round trip cost 7 bps and the reward floor sat at 10.5 bps, almost twice the typical 5 minute move. A resting target costs 2 bps, which brings the round trip to 4 bps and the floor to 6 bps.

Options weighed:
- **Maker target exits as a base option (chosen)**, over a lower fee tier (Kraken's lower tiers need far more volume than this account trades) and over a lower `fee_hurdle_k` (a gate threshold, frozen per version, never changed after a result). Default `taker`, so every recorded study keeps its meaning.
- **The candle gate path for bar scalpers, opt in per study (chosen)**, over lowering the 17 a day floor for replay (it would let a replay study reach a 12 day holdout that cannot give 200 trades, so the gate would say `insufficient` every time) and over leaving the rule alone (it fails every bar idea for a reason the hypothesis does not cover). The candle lab is the harsher judge of fills: a resting maker order fills only when the synthetic opposite side reaches its price (L1 quotes, trade execution off), and within a minute the price path takes the extreme against the open position first. Its known optimism (a whole minute's range, a synthetic spread) is why a `pass` still goes to paper trading before any real money.
- **`bollinger_rsi_r1` as the one revision, changing only the exit (chosen)**, over revising `vwap_retest` (its edge is below one round trip's cost, so a cheaper exit cannot rescue it) and over a new bar idea (a fresh candidate adds trials without any evidence for it). The floor change (6 bps) is not a second dimension: it is the existing 1.5 × round trip rule applied to the new cost.
- **`book_imbalance` as the fourth candidate (chosen)**, over Binance lead lag (it needs a recorded Binance feed and has little free history, so it becomes its own feature) and over a liquidity sweep idea (more free settings, and a swing needs confirming bars). It is the only scalping idea that fires often enough for the replay holdout, and it uses the L2 books the replay already has. The first design rejected it because its edge decays within seconds. That still holds, so it gets a cheap pre check of both trade rate and edge before any 40 hour study.

The honest cost: the candle gate path was opened after the results were seen. What keeps it from being a fishing trip is one revision only, a holdout year no scalping run has read, the family trial count that already includes every void and screen run, and gate thresholds that do not move. Running several strategies together under a regime rule was asked for too; it needs a trader that runs more than one strategy, so it goes to the scope as its own feature, once at least two strategies pass.

## Audit result

Filled in by build task 2 in `verify.md` (batch folder, spread p95, median 5 minute move, `reachable` flags, and the resulting order), before any candidate study runs.
