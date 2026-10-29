# Review, feat/multi-coin-portfolio (spec 0022, PRs #106 and #107 plus test commit f147f96), 2026-10-29

**Reviewed by**: Sonnet 5.5 (author on Opus 5.5 and Sonnet)
**Scope**: 50 files, branch range bed1415..bb9b806 plus f147f96, read statically (nothing run on the Mac host)
**Verdict**: Approve with nits

## Summary
The change lets the candle lab run several coins in one Nautilus engine and one margin account, adds ETH data and the off tick rounding rule, carries every coin in the run bundle, and adds the `concentration_instrument` gate check with thresholds version 2. The work follows the spec closely, single coin paths keep their old shape, and the pure logic sits in core with docstrings. I found no blockers and no majors. The minors are about edge cases the spec chose or did not think of.

## Minor
### 🟡 The coin share check is skipped when only one coin traded, `packages/core/src/trading_core/gate/checks.py:261`
**Problem**: `concentration_instrument` runs only when the holdout trades touch at least 2 coins. A two coin study whose ETH side made no holdout trades at all gets the check skipped, so the whole profit comes from BTC and the check says nothing.
**Why it matters**: The stated aim of the check is to stop a "portfolio" passing on one coin's luck. The case where one coin has zero trades is the most extreme version of that and it passes the check silently. The spec (AC-13) wrote this skip on purpose, so this is a design gap, not a coding slip. Other checks (trade count, folds) may still catch it, but not by design.
**Suggested fix**: Consider giving the gate the study's coin count (from the plan or the bundle) and failing, or at least recording a reason, when the study names 2 or more coins and the holdout trades land on fewer. Record the choice in the spec rationale either way.

### 🟡 Noise bounds are absolute, so they do not guard a tiny tick, `packages/core/src/trading_core/lab/candles.py:36`
**Problem**: `PRICE_NOISE` is `1e-8`. The pinned PEPE tick is `1e-10` (`PF_PEPEUSD.KRAKEN.json`), so every price is within `1e-8` of some tick and `is_off_tick` never returns true for PEPE. Highs and lows are rounded to the nearest tick instead of toward the body, and off tick bodies are never counted or rejected.
**Why it matters**: Only the exploratory PEPE study is affected, and rounding to the nearest tick is harmless at that scale. But the bad row guard from spec 0014 AC-3 is silently off for any coin whose tick is near or below the noise bound.
**Suggested fix**: Either scale the bound to the tick (for example the smaller of `1e-8` and a fraction of the tick), or add a line in the PEPE study comment and the instrument note that off tick detection does not apply there.

### 🟡 Only the first coin's entry blocks hold a recording failure, `apps/lab/src/lab/candle_units.py:271`
**Problem**: `LabRecorder` gets `blocks=coins[0].blocks`, so `RECORDING_FAILED` is set only on the first coin's `EntryBlocks`. The second coin keeps entering trades after a recording failure until the run ends and the post run check raises.
**Why it matters**: The variant still fails at the end, so no wrong result is stored. But the lab is meant to fail closed on order paths, and the run does wasted work and writes more rows after the fault. It also depends on the post run `any(...)` check never being dropped.
**Suggested fix**: Let the recorder take the list of every coin's blocks and set the reason on all of them.

### 🟡 `SharedRuin` is not idempotent, `apps/lab/src/lab/actors.py:372`
**Problem**: Each coin's guard calls `SharedRuin` when equity is at or below zero. If the other coin's guard also sees a tick before the engine stops, `ruin.at_ns` is overwritten with a later time and the closes run again (guarded by `_closing`, so no second order).
**Why it matters**: `ruined_ns` in the report could differ from the first ruin time. It is deterministic, so it does not break byte identity, but it is not the moment ruin was first seen.
**Suggested fix**: Keep the first `at_ns` (set it only when it is `None`) and return early on later calls.

## Nits
- ⚪ `apps/lab/src/lab/candle_stream.py:289`, the docs say equal timestamps follow sorted ids "by construction", but a normal minute's last point (+59.999 s) and another coin's quiet minute point (also +59.999 s) are emitted in different chunks, so the earlier chunk wins, not the sorted id. Still deterministic. Soften the wording or note this case.
- ⚪ `packages/core/src/trading_core/gate/thresholds.py:153`, the `_ADDED_IN` branch coerces with `Decimal` directly while others use `f.type`; a short comment on why would help the next reader.
- ⚪ `apps/lab/studies/donchian_4h_pepe.toml:1`, an exploratory study outside any spec sits beside the gated ones; the header explains it well, but a pointer to where its result is recorded would keep it findable.

## Strengths
- Single coin behaviour is protected on purpose: the serializer drops the unused coin key, the version config only adds `instrument_ids` for multi runs, and a test pins the plan hashes and version configs of the committed single coin studies.
- Thresholds version 1 and 2 handling is careful: version 1 JSON must lack the new field and loads as `None`, version 2 requires it, and a parity test shows the same verdict under both.
- The merged stream builds each coin's minute only when due, so the side flag is read at the right time, and tests cover tie order, funding order, per coin gaps, shared exposure cap, ruin on one coin, funding isolation, and byte identical reruns.
- Off tick rounding is bounded (the per month cap of 10, clamp inside the stored range, flat repeat rule) and each branch has a named test.
- Good guard against path tricks in the pinned instrument loader, and the single coin commands refuse multi coin run files with exit 64, each with a test.
- Combined equity and union exposure are small pure functions in core with docstrings and tests.

## Test coverage
Good. New logic in core (`combined_equity`, union `exposure`, `mean_benchmark`, rounding, thresholds loader, gate check, bundle codec, instrument import) and in the lab (merged stream, multi coin unit, report, refusals, fetch and import per coin) each has tests, and the dashboard coin share has a present and an absent case. Not covered that I could find: a recording failure with two coins (the minor above), a repeated ruin call from both guards, and a two coin study where one coin has no holdout trades (the first minor). I did not run any tests, since NautilusTrader cannot import on this host.
