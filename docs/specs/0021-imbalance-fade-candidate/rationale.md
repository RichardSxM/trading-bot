# 0021 rationale: `imbalance_fade`

## Context

Every candidate so far has failed the gate. Seconds scalping (spec 0017) and the 15 minute and 1 hour rules (specs 0018, 0019) earned less per trade than a round trip costs. The trend rules of spec 0020 cleared the cost but failed the concentration checks, which reject a profit carried by a few big trades. The gate is fixed (v1) and is never loosened after a result.

One result stood out. `book_imbalance` (spec 0017 child 0004) was not just too weak: its pre check over 4 days lost 0.95 USD a trade before any fee, every day negative (59 trades, cost 1.15 USD a trade). A signal that is reliably wrong may carry information from the other side. Albers, Cucuringu, Howison and Shestopaloff (2025, "The Market Maker's Dilemma", arXiv 2502.18625) placed about 230,000 live maker orders on Binance's BTC perpetual and found the same mechanism: a maker order on the heavy side fills mostly when that side breaks, so fills are followed by adverse moves, and the strategies that worked traded against the book imbalance.

The forces: maker fees (0.02% a side) are the only cost lever left; the replay lab models the queue pessimistically; the replay holdout is 12 free Tardis days (2025-11-01 to 2026-10-01) and the gate needs 200 holdout trades; a full replay study costs about 40 laptop hours.

## Options considered

### Option 1: exact mirror behind a fresh day pre check (chosen)

Same signal as `book_imbalance`, opposite side, maker in and out, a 4 day pre check on unused days, then the standard study.

**Pros**: the most direct test of the negative gross; reuses tested code unchanged; cheap to kill.
**Cons**: data snooped by construction; posting on the thin side changes the fill pattern, so the mirror of the result is not guaranteed.

### Option 2: wait for the imbalance to reverse

Enter only after a lopsided book swings back past zero, closer to the paper's "Reversals".

**Pros**: closer to the published idea.
**Cons**: new logic with no result of ours behind it, more settings to choose, and so more trials.

### Option 3: a maker entry version of `donchian_4h_r1`

**Pros**: the trend rule already clears the cost.
**Cons**: a resting order on a breakout fills mostly on the false breakouts (the real ones run away from it), and the rule still fails the top trades check, which maker fees do not touch.

### Option 4: fade without the trade flow filter

**Pros**: more trades, easier to reach the 200 trade floor.
**Cons**: a weaker link to the result that motivated it, so a pass would say less.

## Rationale

Option 1 tests exactly the evidence we have, the negative gross, and nothing else, so it adds the fewest trials for the most information. The pre check adds a trade rate bar (25 closed trades a day, about 1.5 × the bare 16.7) because the replay holdout's 12 days and the gate's 200 trade floor would otherwise make an "insufficient" verdict likely at `book_imbalance`'s rate of about 15 a day, after 40 hours of replay; the margin covers the holdout picking the slower `imb_min` 0.5 point. The pre check days are new only against `book_imbalance`'s days; the study reads them again. Option 3 was the runner up, but its failure mode is structural, not a cost problem.

## References

**Project sources**:
- spec 0017 child 0004 and `verify.md` (the `book_imbalance` pre check, task 19)
- spec 0020 (trend verdicts, concentration failures)
- `packages/core/src/trading_core/gate/thresholds.toml` (`[replay] min_holdout_trades` 200, `fee_hurdle_k` 1.5)

**Links**:
- The Market Maker's Dilemma (arXiv 2502.18625): https://arxiv.org/html/2502.18625v2
