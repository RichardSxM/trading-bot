# 0027.1. The `trend` kind's gate rules and `thresholds.toml` version 3

## Summary

This child teaches the gate how to judge a trend strategy. For the new `trend` kind, it adds the 13 trials run before the study to the trial count, skips the long versus short check when a strategy is long only by design, and drops the top 5 % trades check in favour of the luck tests the gate already runs. It also gives trend runs a warmup pre roll, so a daily rule can trade from the first day of a segment. Scalping and hour level studies are judged exactly as before. The paper floor (60 days, 8 trades) is not built here; it moves to the paper follow up (index Follow-up).

## Requirements

- **AC-4** (thresholds v3): `trading_core/gate/thresholds.toml` becomes `version = 3`. It keeps every v2 value and adds one table, `[trend] prior_trials = 13`.
  - The loader gains `TrendThresholds(prior_trials: int)` as the top level field `Thresholds.trend: TrendThresholds | None`.
  - `trend` goes into `_NESTED` (so it is built as a table, not coerced as a `Decimal`) and into `_ADDED_IN` with version 3, so it is `None` in versions 1 and 2. Every existing study's frozen thresholds then parse, and `to_json` gives byte identical JSON for them (no `trend` key is emitted for v1 or v2).
  - `SUPPORTED_VERSIONS` becomes `(1, 2, 3)`. Tests that pin version 2 as the current version are updated to 3.
  - `prior_trials` gets its own int coercion that accepts 0 or more (the shared int rule rejects values below 1). A negative or non int value raises `ThresholdsError`.
  - A study planned under v2 and resumed by name after this change is refused as today ("planned with other thresholds", `lab/study.py`). That is intended.
- **AC-5** (trial count): for a study of kind `trend`, `trial_count = max(1, prior_trials + distinct frozen hashes)`.
  - `family_trials(session, kind, lab)` is unchanged and still returns the database count and Sharpes. `study_evidence` adds `prior_trials` from the study's frozen `thresholds.trend` after that call.
  - `trial_sharpes` stay database only (the 4 grid points). The DSR's variance term therefore ignores the 13 outside trials while its N counts them. That is accepted: N is the stricter half of the test.
  - The trial query already filters by kind and lab and excludes benchmarks by role, so `trend` trials never mix with hour level ones.
  - `promotions.metrics` gains `prior_trials`. For scalping and hour level, nothing changes, and `prior_trials` is recorded as `0`.
- **AC-6** (direction check): for kind `trend`, when the final strategy version's frozen `config["long_only"] is True`, `concentration_direction` is not applied.
  - Metrics: `direction_applicable` false, `direction_share` still computed and recorded, and no reason is emitted for it (the builder adds a path that records the share without calling the reason emitting check).
  - In that case a holdout whose run measures show `short_trades > 0` fails with reason `long_only_violated`, with that count. When the holdout run has no measures, it fails closed with `missing_metric`, as `check_fee_hurdle` does.
  - For kind `trend` without `long_only` true, and for every other kind, the check runs as today, with `direction_applicable` true.
- **AC-7** (top trades check): for kind `trend`, `concentration_top_trades` is not applied. Metrics: `top_trades_applicable` false, `top_trades_share` still computed and recorded, no reason. `concentration_fold`, `concentration_instrument`, the bootstrap, DSR, PBO, drawdown, the fee hurdle, holdout Sharpe, and the holdout trade floor (`candle.min_holdout_trades`, 60) all apply to `trend` unchanged. For other kinds, `top_trades_applicable` is true and nothing changes.
- **AC-8** (benchmarks): `BENCHMARKS["trend"] = (buy_and_hold,)`. `beats_buy_and_hold` is a hard check for `trend`, as for every kind. `lab holdout` therefore runs only `buy_and_hold` for a trend study, on every coin in one shared account (spec 0022 AC-11). The `buy_and_hold` registry entry keeps kind `hour_level`, and benchmark runs use their own entry's kind, so no change is needed there.
- **AC-9** (removed by the cross check, 2026-11-01): the paper floor for `trend` (60 days, 8 trades) is not built in this spec. No paper step can run for `trend` until the paper follow up, so the fields would have no reader. The follow up carries the agreed values.
- **AC-10** (lab accepts the kind): the run file check in `apps/lab/src/lab/runfile.py` (the `entry.kind != StrategyKind.HOUR_LEVEL and not entry.candle_screen` test and its message) also admits kind `trend`, so `lab backtest`, `lab study`, and `lab holdout` accept it. The message names all three admitted shapes. `lab screen` already refuses anything but scalping (`screen.py`), which covers `trend` with no change. `lab replay` gets no new check in this spec. The `lab study` plan records the thresholds version it froze.
- **AC-10a** (warmup pre roll for trend runs): for a strategy of kind `trend`, every candle segment (each development fold and the holdout) is preceded by a pre roll of `warmup_days` days of the same candles, read from `max(segment start − warmup_days, first candle)`.
  - `warmup_days` is the strategy's `warmup_bars()` (for `tsmom`: `lookback_days + atr_period + 1`), so it follows the grid point.
  - Pre roll bars feed the strategy's bar aggregation and indicators only. No order is sent, and no fill, funding charge, position, or measure is recorded before the segment start. The run's measures and equity start at the segment start, as today.
  - The holdout's pre roll reads development data (before `holdout_start`), so it adds no holdout look. The `buy_and_hold` benchmark gets the same pre roll and ignores it.
  - Other kinds keep today's cold start, so their runs and verdicts don't move.
- **AC-11** (version check): `gate check-version` parses kind `trend` from `trader.version` JSON and writes the version, as for the other kinds.

## Design

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| `gate evaluate` (trend) | N | `thresholds.trend.prior_trials` (frozen in `studies.thresholds`, added in `study_evidence`) + `family_trials`' count of distinct `study_runs.frozen_hash` of kind `trend` in the study's lab |
| `gate evaluate` (trend) | long only | `strategy_versions.config["long_only"]` of the final version (`study_evidence` already loads that version), a new `CandidateInputs.long_only: bool` defaulting to False. The lab writes a version config as `{**[strategy.config], **grid point}`, so a config default never lands there: the study file must write `long_only = true` explicitly (child 2 AC-18) |
| `gate evaluate` (trend) | short trade count | `run_measures.short_trades` of the holdout run (already in `StudyRunInput.measures`; `positions.side` is `flat` once closed, so it can't be used) |
| `gate evaluate` (trend) | `top_trades_share` | as today, `top_trades_quantile` (0.05) of the sorted holdout net P&L |
| `lab holdout` | benchmarks | `benchmark_names("trend", studied)` |
| lab trend segment | pre roll length | the strategy's `warmup_bars()` in days (AC-10a) |

**Key invariants**:
- Each new branch tests `strategy_kind == StrategyKind.TREND`. The metrics added for every kind (`prior_trials`, `direction_applicable`, `top_trades_applicable`) report today's behaviour for scalping and hour level.
- Re-serialising a v1 or v2 thresholds JSON gives byte identical JSON (the `_ADDED_IN` rule).
- The gate never reads the lab registry. Long only is known only from the frozen version config.
- Old promotion rows lack `prior_trials`, `direction_applicable`, and `top_trades_applicable`. Every reader (`gate` output, dashboard) treats a missing key as absent, never as an error.
- No trend run sends an order or records a measure before its segment start (AC-10a).

**Critical test scenarios**:
- v2 JSON in a `Thresholds.from_json` → `to_json` round trip is identical, and v3 loads with `trend.prior_trials` 13. `prior_trials` 0 loads, and a negative one raises. Verifies **AC-4**.
- Trend family with 4 distinct hashes and `prior_trials` 13 gives N 17; an hour level family with 27 hashes still gives 27. Verifies **AC-5**.
- Trend, long only, all long trades with long share 1.0: no direction reason, `direction_applicable` false. The same with one short trade: `long_only_violated` 1. The same with no holdout measures: `missing_metric`. Hour level with long share 0.9: `concentration_direction` fails as today. Verifies **AC-6**.
- Trend with top 5 % share 1.4: no top trades reason, metric 1.4 recorded. Scalping with the same numbers fails `concentration_top_trades`. Verifies **AC-7**.
- Trend with holdout Sharpe at or below buy and hold's fails `beats_buy_and_hold`; `benchmark_names("trend", "tsmom")` is `("buy_and_hold",)`. Verifies **AC-8**.
- A trend run file is accepted by `lab study`, and refused by `lab screen` with exit 64. Verifies **AC-10**.
- A fixture trend segment with a 21 day pre roll: the rule's first entry can fall on the segment's first close, no order or measure carries a timestamp before the segment start, and an hour level run's first bar is unchanged. Verifies **AC-10a**.

## Rationale (short)

The direction check measures whether profit leans on one side. For a strategy that can only be long, the share is 1.0 by construction, so the check carries no information about luck. The top 5 % check fails every one year development window of `tsmom` (0.92 to 24) and of the long and short variants, because trend following earns its money in the tail. The bootstrap lower bound and the DSR already ask whether the profit is luck, and they keep running. N gets the 13 pre check trials so the search done outside the database is paid for. The pre roll exists because a daily rule needs up to 81 days of history: a cold start would spend up to a fifth of a one year holdout unable to trade, which fails the trade floor for reasons that have nothing to do with the edge. Runner up: a top 1 % check. It has the same arbitrary limit on a smaller tail and still fails some windows. Full options in [rationale.md](rationale.md).
