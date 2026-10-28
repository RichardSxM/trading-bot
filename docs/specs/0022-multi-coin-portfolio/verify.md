# Verify: multi coin portfolio · spec 0022 · updated 2026-10-28

## AC-1 hash baseline (2026-10-28)

`code_hash(code_closure(m, root))` for every `trading_core.node` module (32) on origin/main bed1415, the commit `feat/multi-coin-portfolio` starts from, run through `scripts/spikes/hash_check.py` in `kfs/lab:dev` against a `git archive` of main's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `9c8c2a41d51b414ce8f21c140972c3ae3abc217a7fb2f8df88a0b724becd76b3`. The close out (AC-18) runs the same list on the branch: it must match line for line, with no module added or edited.
