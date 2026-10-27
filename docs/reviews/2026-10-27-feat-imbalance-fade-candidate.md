# Review, feat/imbalance-fade-candidate, 2026-10-27

**Reviewed by**: Sonnet 5.5 (author on Sonnet 5.5)
**Scope**: 10 files, branch vs bbc46b7 (spec 0021 commits only)
**Verdict**: Approve with nits

## Summary
This change adds `imbalance_fade`, a small pure rule that mirrors `book_imbalance`'s signal, a thin node subclass, a registry entry and tests. The code is small, correct against AC-2 to AC-4, and leaves both `book_imbalance` files untouched, so the frozen hash holds. The pre check failure is expected and not a finding. I found no blockers or majors, only a few small things you can take or leave.

## Minor
### 🟡 The node builds the parent rule and then throws it away, `packages/core/src/trading_core/node/imbalance_fade.py:28`
**Problem**: `super().__init__` already builds a `BookImbalanceState`, and you then overwrite `self.rule` with an `ImbalanceFadeState`. Anyone reading this has to know the parent's rule is dead.
**Why it matters**: It works, but a later change to the parent's constructor (for example a side effect) would run twice, and the type of `self.rule` stays the parent's.
**Suggested fix**: Fine to keep to honour AC-3 and the byte unchanged parent. Add one comment line saying the parent rule is replaced on purpose, or add a rule factory hook in a future change to the parent when its hash may move.

### 🟡 Node test does not check the mirrored stop and target, `packages/core/tests/test_node_imbalance_fade.py:35`
**Problem**: The node test checks side, reason, imbalance and timing, but not that the stop sits above and the target below the mid on the engine path.
**Why it matters**: The pure tests cover the arithmetic, so the risk is low, but the wiring of the signal's prices into the order is only proven for the parent's direction elsewhere.
**Suggested fix**: Assert the enter decision's stop and target (if recorded in the decision inputs or the order) against the mirrored values.

### 🟡 The `TypeError` guard in `on_firing` is untested, `packages/core/src/trading_core/strategy/imbalance_fade.py:25`
**Problem**: The branch that raises when `inputs["mid"]` is not a `Decimal` has no test.
**Why it matters**: It is a defensive, fail closed branch on the order path. It is unreachable today, but an untested branch can rot.
**Suggested fix**: Add one test with a stub parent that returns a signal with a non Decimal mid, or drop the guard and use a typed accessor.

## Nits
- ⚪ `packages/core/src/trading_core/strategy/imbalance_fade.py:28`, reads the parent's private `_stop_bps` and `_target_bps`; fine in a subclass, but a protected accessor on the parent would be cleaner when the parent may change.
- ⚪ `docs/specs/0021-imbalance-fade-candidate/index.md:4`, Status is still "In Progress" although Results and AC-9 are done; set it to Accepted at merge.
- ⚪ `docs/specs/0021-imbalance-fade-candidate/verify.md:5`, the AC-1 baseline comes from an image built at d799ce5 plus a "nothing changed" diff rather than the bbc46b7 build itself; sound, but a rebuild at bbc46b7 would be direct proof.
- ⚪ `docs/specs/0021-imbalance-fade-candidate/verify.md:11`, the test counts (43 core) differ from the 52 you now have; refresh when you next touch it.

## Strengths
- The rule is a clean wrapper: it reuses the parent's whole signal, passes `inputs` through unchanged, and mirrors only side, stop and target around the parent's own mid, exactly as AC-2 says.
- The code closure test and the hash comparison in `verify.md` give real proof that `book_imbalance` stayed frozen and only two files were added.
- Registry factory refuses the wrong config type, the candle lab refuses it, and the sorted list test is updated.
- Pure tests cover both directions, configured bps, non matching flow, no persistence, empty sides and no mid.
- Reusing the `_run` harness through `strategy_cls` and `config_cls` params avoids copying the engine setup.

## Test coverage
Good. AC-2 is covered by nine pure tests, AC-3 by config, wiring and an engine run (short entry on a bid heavy book, nothing on an empty ask side), AC-4 by four registry tests, AC-1 and AC-9 by the closure test. Gaps are only the minors above. I did not run the tests (Nautilus cannot run on this host); the containers reported them passing.
