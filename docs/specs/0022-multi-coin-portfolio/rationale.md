# 0022. Multi coin portfolio: rationale

## Context

> ⚠️ Premise note: `donchian_4h` was chosen because it did best on BTC's holdout (spec 0020), and that holdout period is reused here. A pass therefore rests partly on data that already shaped the choice, and only ETH's half is a fresh test. The right framing is "does the near miss hold up when ETH is added", not "a new, clean candidate". The spec keeps the gate unchanged (stricter, in fact), counts every grid point in the family N, and labels the result "BTC holdout seen". A truly fresh test needs coins never studied (the "More instruments" follow up).

Specs 0017 to 0021 tested eleven single BTC rules, and all failed the gate. Fast rules failed on cost per trade. Slower trend rules covered their costs but failed on statistics and concentration. `donchian_4h` (holdout +2.78%, gross 6.11 against cost 1.77 a trade) failed on `concentration_fold`, `concentration_direction`, `concentration_top_trades`, the bootstrap lower bound, and the deflated Sharpe. All of these get worse with few trades: 64 holdout trades on one coin is a thin sample. Spec 0021's last follow up asked for a multi coin version of the best candle rule before any more single BTC rules.

The candle lab (spec 0014) and gate (spec 0015) assume one coin per run everywhere. That covers the run file's `instrument_id`, `CandleUnitPlan`, one stream, one recorder, one guard, the benchmark merge, and the hard coded `PF_XBTUSD` in candle fetch and funding import. The risk check already sums open notional over every coin (spec 0011 AC-4), and the trade tables already carry `instrument_id`. The VPS `instruments` table gets rows only from the trader, so a pushed ETH run would break its foreign key.

The constraints: never loosen the gate, keep every past frozen hash and verdict valid, keep the lab laptop only and demo only, use free data only (Kraken's public candles and the funding export), and leave the trader alone until something passes.

## Options considered

### Option 1: One rule, both coins, one shared account (chosen)
One Nautilus engine, one margin account, one strategy instance per coin, one combined run judged by the gate as any other run.

**Pros**: a real shared account (sizing, exposure cap, and ruin interact as live would); the gate's daily series, folds, and statistics work unchanged on one curve; no migration.
**Cons**: lab plumbing across stream, actors, report, and benchmarks; the gate needs a per coin check to see inside the curve.

### Option 2: Per coin runs, combined afterwards
Run BTC and ETH as two single coin runs, each on half the capital, and sum their daily equity into a portfolio series in the gate.

**Pros**: almost no engine change; each coin's run reuses today's code.
**Cons**: no shared margin, so exposure and sizing interplay is never tested; the gate must learn composite evidence (two runs per role), touching folds, trade lists, and the importer; half capital sizing differs from a real account.

### Option 3: Separate studies per coin
Add ETH and study `donchian_4h` on it alone.

**Pros**: the least code; a clean ETH only answer.
**Cons**: no portfolio effect, so the thin sample problem stays; another single coin trial that would likely fail the same checks.

### Option 4: Cross sectional rotation
A new rule that ranks coins by momentum and goes long the strongest and short the weakest.

**Pros**: the classic crypto portfolio result in the literature.
**Cons**: a new rule plus multi coin plumbing at once; with two coins, ranking is just a spread trade; it needs many coins to mean anything.

## Rationale

The failed checks are sample size and concentration problems, so the lever is more independent trades under the same rule, not a new rule. Option 1 gets them while the gate's statistics stay untouched: the gate still sees one daily equity curve, so folds, bootstrap, PBO, and deflated Sharpe need no new logic. Only a per coin check is added so diversification has to be real. Option 2 would avoid engine work but push the complexity into the gate, the one place the rules must stay simple and strict, and it would test a half capital setup that no live account uses. Options 3 and 4 do not address the thin sample.

Decisions settled while writing (pick, why, runner up):
- **One strategy instance per coin** with today's unchanged `Donchian4h` node, rather than one strategy that trades a list of coins. It needs no node edit, so every frozen node hash stays valid (AC-1). Runner up: a multi coin `BarRuleStrategy`, which would change the frozen base.
- **One merged stream, ties in sorted id order.** The coins' streams are merged into one `chunks()` iterator ordered by timestamp, then by id, so the order holds by construction and does not depend on how Nautilus breaks ties between iterators (not verified). Runner up: one data iterator per coin, which is less code but bets on that unverified tie behaviour. (Changed after the cross check.)
- **One shared recorder and side flag with per coin maps**, not one per coin. Both actors subscribe to every order and position event, so one per coin would write rows twice and mix slippage sides. Runner up: one per coin with an instrument filter, which keeps two copies of the funding and bar bookkeeping.
- **A shared ruin handler that closes every coin before stopping.** Stopping on one guard's ruin would leave the other coin open and fail the variant. Runner up: exempt ruin from the open position check, which hides the other coin's loss.
- **Thresholds loader accepts versions 1 and 2.** A plain bump would stop every stored v1 study from loading (the loader demands the exact field set and one version). Runner up: rewrite stored thresholds JSON, which edits evidence.
- **Liquidation stays per coin, documented as an approximation.** A cross margin guard means editing frozen lab and risk code before anything passes. Runner up: build it now.
- **`instrument_ids` as a new key, `instrument_id` untouched.** Old run files and hashes stay byte identical. Runner up: always a list, which would change every past plan hash.
- **Strategy name `donchian_4h`, study name `donchian_4h_pair`.** The rule is the same code; the `instrument_ids` in the version config make the trial distinct, and the study page shows both names. Runner up: a new registry name, which adds a registry entry for no new behaviour.
- **Bundles carry instrument rows.** This keeps the VPS import self contained and checks for conflicts. Runner up: a hand run SQL upsert on the VPS, which is easy to forget and has no check.
- **`--instrument` flags with BTC as the default**, so every existing script and cron keeps working. Runner up: a required flag, which would break them.
- **Thresholds version 2 with an explicit `instrument_max_share`**, so the new bar is a written, versioned value and not a reused constant. Runner up: reuse `concentration_max_share` silently, which hides that the gate's rules changed.
