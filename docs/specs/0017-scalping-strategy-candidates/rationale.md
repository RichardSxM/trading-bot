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

## Audit result

Filled in by build task 2 in `verify.md` (batch folder, spread p95, median 5 minute move, `reachable` flags, and the resulting order), before any candidate study runs.
