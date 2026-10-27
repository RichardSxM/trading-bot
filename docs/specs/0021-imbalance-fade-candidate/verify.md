# Verify: imbalance fade candidate · spec 0021 · updated 2026-10-27

## AC-1 hash baseline (2026-10-27)

The branch `feat/imbalance-fade-candidate` starts from bbc46b7 (the spec commit, on top of spec 0020's branch). `scripts/spikes/hash_check.py` ran in `kfs/lab:s0020`, which was built at d799ce5; nothing under `packages/` or `apps/` changed between d799ce5 and bbc46b7 (`git diff --stat` is empty), so the image's own `trading_core` is the baseline commit's. It printed 31 `trading_core.node` modules. The sorted `module hash` list has sha256 `4982f569395f3ca0cb2567b6a05a06ff987f0b2bc181628c372d2c96dc1926c2`. At close out (AC-9) the branch's list minus `trading_core.node.imbalance_fade` must give the same sha256, and that must be the only module added.
