# Verify: imbalance fade candidate · spec 0021 · updated 2026-10-27

## AC-1 hash baseline (2026-10-27)

The branch `feat/imbalance-fade-candidate` starts from bbc46b7 (the spec commit, on top of spec 0020's branch). `scripts/spikes/hash_check.py` ran in `kfs/lab:s0020`, which was built at d799ce5; nothing under `packages/` or `apps/` changed between d799ce5 and bbc46b7 (`git diff --stat` is empty), so the image's own `trading_core` is the baseline commit's. It printed 31 `trading_core.node` modules. The sorted `module hash` list has sha256 `4982f569395f3ca0cb2567b6a05a06ff987f0b2bc181628c372d2c96dc1926c2`. At close out (AC-9) the branch's list minus `trading_core.node.imbalance_fade` must give the same sha256, and that must be the only module added.

## Build checks (2026-10-27)

Core tests: 43 passed. Lab suite: 278 passed, 1 failed (`tests/test_pool.py::test_failing_a_variant_drops_and_stops_only_its_units`, under the load of the 29 minute full run). `test_pool.py` run on its own gave 2 passed, and the file is not touched by this change, so it is recorded as a timing flake. mypy strict and pre-commit clean. `kfs/lab:s0021` was built at a0f0315 with `GIT_COMMIT`.

## AC-5 pre check run (2026-10-27)

`lab-reports/scratch/imbalance_fade_precheck.toml` holds the AC-5 keys exactly (`imb_min = "0.3"`, `target_bps = "6"`). `lab replay` ran it once per day on `kfs/lab:s0021`, two days at a time, from 08:38 to 11:41 UTC. Every run exited 0.

| Day | Run id | Approved enters | Closed trades | Net P&L (USD) | Fees (USD) |
|---|---|---|---|---|---|
| 2022-11-01 | `a9a5307f-9df7-43a4-8b3e-390260c6a4e8` | 105 | 37 | −64.20 | 43.74 |
| 2023-11-01 | `3b45aeaa-79ef-4aa9-a653-2ce218451ecf` | 44 | 31 | +10.59 | 35.50 |
| 2024-11-01 | `7bc4b364-ddf4-4b94-8a98-0274d40bfc08` | 55 | 39 | −53.56 | 43.05 |
| 2025-08-01 | `e0d37192-c86f-4767-8a42-a520ea124ff9` | 6 | 3 | +30.71 | 3.78 |
| **Total** | | 210 | 110 | −76.46 | 126.07 |

Net P&L is after fees, funding left out (`run_results.return_after_costs` × 10000). Approved enters are the run's `decisions` rows with `intent` `enter` and `risk_verdict` `approved`.

## AC-6 pre check verdict (2026-10-27): fail

- Closed trades a day: 110 ÷ 4 = **27.5**, bar 25. Holds.
- Gross per trade: (−76.46 + 126.07) ÷ 110 = **+0.45 USD**. Cost per trade: 126.07 ÷ 110 = 1.15 USD, so the bar is 1.5 × 1.15 = **1.72 USD**. Fails.

The pre check fails on the fee hurdle. Gross per trade turned positive (`book_imbalance` had −0.95), but it covers only about 40% of the cost, well short of the 1.5 × bar. Per AC-6: study not run (not run: pre check), no study file written, no revision. The feature goes to close out (AC-9).
