# Verify: daily trend candidate (tsmom) · spec 0027

## AC-1 hash baseline (2026-11-01)

The branch `feat/0027-daily-trend-tsmom` starts from origin/main 0be15e1 (its only commit on top, 753ec82, adds this spec and no code).

- **Node modules**: `code_hash(code_closure(m, root))` for every `trading_core.node` module (40) on 0be15e1, run through `scripts/spikes/hash_check.py` in `kfs/lab:s0026` against a `git archive` of 0be15e1's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `8a8f462227ec429275e91e7ee726f7f1a4d1cceb94defdd54e77413948513ead`. The close out (AC-21) runs the same list on the merged code and names every changed line with its reason.
- **`donchian_scalp_r2` on demo**: `python -m trader.version` on the VPS (git commit 0be15e1, `PAPER_STRATEGY=donchian_scalp_r2`, instruments PF_PEPEUSD and PF_XBTUSD, kind `scalping`) gives `code_hash` `dc96df87a2bab7c152e7d7d1184e1d72aa88784c954d757154598e90c08eced2` and `frozen_hash` `a1ad9ba1948e050e4c6a3d668e577e21b1f37cd927915f95750be9b5b7cfce10`. AC-3 changes `recording/enums.py`, so both are expected to change, and the deploy needs your manual promote.

## AC-2 to AC-8 and AC-11, kind and gate (2026-11-01)

- **AC-2, AC-3**: migration `0014_trend_kind` (`20261101_0014_trend_kind.py`) and `StrategyKind.TREND`. `tests/integration/test_trend_kind_migration.py`: the upgrade keeps scalping and hour level rows, then accepts `trend` and refuses `other`; the downgrade with no trend row restores the old check text exactly; with a trend row it refuses (`RuntimeError`) and leaves 0014 in place. The round trip and autogenerate drift tests pass unchanged (the model's check follows the enum through `_one_of`).
- **AC-4**: `thresholds.toml` version 3 with `[trend] prior_trials = 13`. A stored v1 and v2 JSON round trips with `json.dumps` byte identical (no `trend` key), 0 loads, −1 and 1.5 are refused.
- **AC-5**: a trend study of 4 hashes gets N = 13 + the family count, its trial Sharpes stay database only, and an hour level study's N and `prior_trials` (0) are unchanged (`test_family_trials.py`).
- **AC-6 to AC-8**: `test_gate_verdict.py` trend cases: long only all long passes with `direction_applicable` false and `direction_share` 1.000000; one short trade gives `long_only_violated` 1; no measures gives `missing_metric`; trend without `long_only` and hour level (either flag) keep `concentration_direction`; top 5 % share 1.4 is recorded with no reason for trend and fails scalping; trend is judged against buy and hold only.
- **AC-11**: `gate check-version` parses and writes a hand made `trend` payload (`apps/gate/tests/test_check_version.py`).
- Suites: core unit 2267 passed, core integration 185 passed, gate 38 passed; ruff and mypy strict clean.
- Not changed, by AC-3: `PaperThresholds.trade_floor` gives a non hour level kind the scalping floor (100). No paper step runs for `trend` before the paper follow up, which owns the trend paper floor.
