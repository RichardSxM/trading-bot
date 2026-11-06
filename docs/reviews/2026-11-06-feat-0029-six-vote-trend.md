# Review, feat/0029-tests (feature 30 six_vote_trend, merged), 2026-11-06

**Reviewed by**: Claude Sonnet 5.5 (author on the same family; fresh context)
**Scope**: 14 files, branch vs main (merge base 1dd44bd)
**Verdict**: Approve with nits

## Summary
Adds `SixVoteRule` (pure six vote rule on 1 hour bars) and `SixVoteTrend` on the frozen maker base: BTC vote with a hold and wait, a market fallback for the maker entry, a per tick trailing stop, a 1 hour seed, and trader wiring. The code follows the spec closely, fails closed on the order paths I traced, and leaves existing modules untouched. No blocker or major found; a handful of minors around sizing in the part fill fallback, a trail pending flag with no timeout, and a no seed edge.

## Minor
### 🟡 Part fill fallback can exceed the per trade risk the spec promises, `six_vote_trend.py:433`
**Problem**: The market quantity is `min(limit.leaves_qty, approved)` where `approved` is a full size verdict at the current mid. When the limit part filled and price moved toward the stop, `approved` shrinks, but the filled part (sized at the old price) is not subtracted, so filled plus fallback can exceed what the risk check approves now (`has_position=False` is also passed although a position exists).
**Why it matters**: Spec says risk is sized again so realised risk never exceeds the limit; in the part fill case that holds only against the original sizing. Demo only, bounded by the original size.
**Suggested fix**: Pass `has_position=True` when held and cap the market quantity at `approved - filled`, or document the exception in Consequences.

### 🟡 `_trail_pending` has no timeout, `six_vote_trend.py:590`
**Problem**: The flag clears only on `OrderUpdated` or `OrderModifyRejected` for the current stop. If the venue never answers (dropped event, adapter quirk), the trail stops for the rest of the trade with no log.
**Why it matters**: Silent loss of the trail; the resting stop still protects, so this is degraded behavior, not an exposure.
**Suggested fix**: Clear the flag and count a refusal after a few seconds without an answer, or log when pending longer than N seconds.

### 🟡 A coin without a seed feeds its first (partial) live bar, `six_vote_trend.py:235`
**Problem**: With `warmup_seed_failed` for a coin, `_seed_pending` is False, so the first live bar (covering only part of the hour) goes into the rule, EMAs, ATR, and the 50 bar volume window. The same coin also holds that bar 3 s waiting for a BTC bar that the BTC instance's seed join discards, logging `btc_bar_late`.
**Why it matters**: Slightly skewed windows after a failed seed; r2 behaves the same, and ready needs 200 bars anyway, so impact is low.
**Suggested fix**: Ignore the first live bar of any instance started without a seed, or accept and note it.

### 🟡 Per tick cache lookups for 12 coins, `six_vote_trend.py:561`
**Problem**: `_trail` calls `_position()` (cache scan) on every quote tick of every instance, before any cheap early exit such as `trail_atr is None` or phase check.
**Why it matters**: Hot path; small cost today, grows with tick rate.
**Suggested fix**: Return early when `self.phase` is not `PROTECTED`/`EXITING` before touching the cache.

## Nits
- ⚪ `six_vote_trend.py:552`, `except Exception` around the trail logs and continues; acceptable as fail safe, but name the narrower errors if known.
- ⚪ `six_vote_trend.py:330`, the seed feed `except Exception` leaves the rule half fed; logged, fine, but the log says nothing about how many bars were fed.
- ⚪ `six_vote_trend.py:397`, `_refused` docstring says "refused part fill fallback is reported"; the code path also resets `_fallback_id`, worth one word.

## Strengths
- The fallback re-runs the base's gate, block, quote age, stop-through and risk checks, never calls `super()` for a case it handles, and has one test per skip reason plus kill switch, rejected, and lost market.
- The pure rule matches the pinned backtest definitions (previous bar windows, cross from below to ±6, RSI 55/45 edges, zero range), and the trail is ratchet only with a tick-or-step floor, a single in flight modify, and a refusal cut-off.
- The BTC vote is keyed by bar close time with a bounded wait and a logged late path, so a silent V2 is visible via `six_vote_values`.

## Test coverage
Rule: strong (crossings, edges, volume, breakout, readiness). Node: fallback paths, trail steps/pending/refusals/off, time stop, resume past and before the ATR, BTC ordering, seed join and stale, values log. Trader wiring: version config, builder refusal outside demo, seed fetch. Gaps: part fill fallback under a price move toward the stop (sizing, finding 1), a trail modify that never gets an answer, and a coin starting with no seed while BTC has one.
