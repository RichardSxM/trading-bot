# 0019. Rationale

## Context

> ⚠️ Premise note: a longer hold raises the move per trade, but it does not by itself create an edge. Scalping failed because gross edge per trade was a fraction of the cost (the book_imbalance replay proof on 2025-09-01 made 0.23 USD gross per trade against 1.04 USD in fees). At 15 minutes the cost stays about 14 bps per round trip while the moves get several times larger, which removes the arithmetic wall, not the need for a real signal. The gate stays strict, so the honest expectation is that most of these four fail. The right framing is "test whether any simple intraday idea survives costs," not "find the winner."

Spec 0017 tested scalping (holds of seconds to minutes) on recorded order books and candles. Every candidate failed on the same point: the move each trade could catch was close to, or below, the round trip cost of Kraken's fees plus slippage. The trader asked for strategies that hold from minutes to hours instead, so each trade aims at a move well above the cost.

Spec 0018 already covers 1 hour and 4 hour candles with six candidates, a shared `BarRuleStrategy` base (market entries, a protective stop, sizing from the stop at 0.5% risk, time stops), and a family trial count that spans every `hour_level` candle study. That base is frozen after its first study and is on main. Its first candidate, `connors_rsi`, failed with only 4 holdout trades. Another session is building spec 0018's remaining candidates.

The forces: the candle lab has Kraken's free 1 minute BTC candles from March 2022 and accepts minute bar specs, so shorter bars need no new data. The gate (spec 0015) needs at least 60 holdout trades, a positive walk forward Sharpe, a fee hurdle, and a holdout that beats buy and hold. Every trial counts toward the deflated Sharpe and PBO overfitting checks. The repo is public. The cost of not deciding is staying stuck between a failed scalping spec and a 1 hour spec whose candidates trade rarely.

## Options considered

### Option 1: Amend spec 0018 with 15 minute variants

Add a 15 minute variant of each spec 0018 candidate as a child of 0018.

**Pros**:
- One spec, one family, one place for all bar based results.
- Reuses rules that are already written or planned.

**Cons**:
- Another session is building against spec 0018 right now; editing it mid build risks conflicts and confuses its run order.
- 0018 AC-18 allows one revision per candidate; 15 minute variants of the same rules blur the line between a new idea and a revision.
- The 1 hour rules were written for 1 hour behaviour (for example the prior day level sweep); several do not transfer to 15 minutes unchanged.

### Option 2: New spec with 15 minute candidates on 0018's frozen base, in the hour level family (chosen)

Four candidates on `15-MINUTE-LAST` bars: the existing `connors_rsi` module as a zero code tracer, plus three new pure rules (EMA pullback, day VWAP reversion, US open range breakout). Kind `hour_level`, so they share 0018's family N.

**Pros**:
- No base change, no migration, no deploy; each candidate is a pure rule, a thin node, and a study file.
- Many more trades per year than 1 hour bars, so real verdicts instead of `insufficient`.
- Targets of 1.5 to 3 ATR on 15 minute bars sit several times above the 14 bps round trip.
- One family keeps the overfitting check honest across both specs.

**Cons**:
- Raises N for every hour level study, spec 0018's included.
- Market entries keep the full taker cost.
- Shares the one holdout year with spec 0018.

### Option 3: Same as Option 2 with a maker entry base (`bar_rule_v2`)

Post only limit entries at the bar close price, cutting the per side fee from 5 to 2 bps.

**Pros**:
- Roughly halves the cost per trade.

**Cons**:
- The candle lab has no order book and cannot model queue position or adverse selection, so maker fills would look better than live. That optimism is what hurt the scalping replays.
- A new base is a large piece of code to build, test, and freeze before any candidate runs.

### Option 4: Swing trading on 4 hour or daily bars

Hold days to weeks.

**Pros**:
- Largest moves per trade, cost becomes nearly irrelevant.

**Cons**:
- Few trades over four and a half years; most candidates would fail the 60 trade holdout floor (`insufficient`).
- Funding becomes a large cost on multi day holds.
- 4 hour bars are already a valid revision in spec 0018.

## Rationale

The scalping results point at one lever: the move per trade against a fixed cost. Option 2 pulls that lever with the least new code and no new risk to what runs on the VPS, because spec 0018's base already does everything an intraday candidate needs (market entry, stop, risk sizing, time stop, decision rows) and accepts any minute bar spec. At 15 minutes, BTC's typical bar range is several times the 14 bps round trip, while the bar count over four and a half years is four times the 1 hour count, which answers the trade floor problem that sank `connors_rsi` at 1 hour.

Option 1 would be cheaper on paper, but editing a spec another session is building from is the kind of shared state change that causes silent conflicts, and its rules were designed around 1 hour behaviour. Option 3 attacks cost from the other side, but the candle lab cannot tell you whether a maker entry would really fill, and the scalping work already showed that this optimism costs more than the fee it saves. Option 4 would mostly produce `insufficient` verdicts and overlaps 0018's allowed 4 hour revision.

Joining the hour level family was the trader's choice, and it is the honest one: these are bar based ideas on the same data and the same holdout year, so counting them separately would let a lucky pass hide behind a smaller N. The `connors_rsi` rerun is kept because it is free and proves the pipeline, but it is labelled "holdout seen", because the 1 hour version already judged that year and choosing to rerun an idea after seeing its result is selection.

Recommendations made without a separate question, each with its runner up:
- **Run order:** `connors_rsi_15m` first (zero code, proves 15 minute bars), then `ema_pullback`, then a checkpoint (index AC-10) before the two session ideas that need the new helper. If both early candidates make less before costs than they pay in costs, 15 minutes alone is not enough and the session ideas would add trials to the shared holdout year for little chance; the checkpoint saves that code and that N. Runner up: the EMA pullback first, giving up the free tracer.
- **VWAP late cutoff:** no new entries in the last 2 hours of the UTC day, because `day_end` would close them within minutes at full cost. Runner up: accept those trades as noise.
- **New York clock:** a pure function with the US daylight saving rule rather than a time zone database, because core is pure and the container may not ship tz data. Runner up: `zoneinfo` with the `tzdata` package.
- **Bar time rule:** every date and clock rule uses the bar's open time, so a bar belongs to the day it started in. Runner up: close time, which puts the 23:45 to 00:00 bar in the new day and makes VWAP resets off by one bar.
- **VWAP `day_end` exit:** close at the first bar of a new UTC day, because the target level resets then. Runner up: hold through the reset to the new day's VWAP, which changes the trade's meaning mid hold.
- **Range breakout stop at the range middle:** a common, tighter choice than the far side of the range; the 1 ATR floor bounds it. Runner up: the far side, which halves the R multiple reachable within the session.
- **Cost check before studies (task 1):** a median 15 minute ATR in bps from the stored candles, recorded and not gating. Runner up: no check, which risks a wasted study if a target sits near the cost.
- **No revision for `connors_rsi_15m`:** it is already a variation of a judged idea.
