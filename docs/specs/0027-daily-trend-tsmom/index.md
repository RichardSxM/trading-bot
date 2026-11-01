# 0027. A trend kind in the promotion gate, and the daily momentum candidate `tsmom` on BTC, ETH, SOL and XRP

**Date**: 2026-10-31
**Status**: In Progress

## Summary

On daily bars, momentum (be long a coin while its close is above the close some days ago, else flat) earned far more than its costs in a pre registered check on the development data. Today's gate fails any such strategy by design, though: a long only strategy earns all its profit from longs, and a trend follower earns most of it on a few big trades. This spec adds a `trend` strategy kind to the gate whose rules fit trend following, with every other check unchanged. It then runs `tsmom` through the gate once, on all four coins in one shared account. The gate change was decided after the development check (see Consequences), so a pass is weaker evidence than one under a gate fixed in advance.

## Structure

The shared contract (the new kind, its migration, the hash baseline, the run order) lives in this file. Each child spec is complete enough to build from.

| # | File | What it is | Decision it supports |
|---|---|---|---|
| 1 | [0001-trend-gate-kind.md](0001-trend-gate-kind.md) | `thresholds.toml` v3 and the `trend` kind's gate rules | how a trend strategy is judged |
| 2 | [0002-tsmom.md](0002-tsmom.md) | the `tsmom` rule, node, SOL and XRP data, the four coin study, its verdict | whether daily momentum is promoted to paper |

Run order is the file order: no `tsmom` study runs before child 1 is merged, because the study freezes the thresholds version it was created with.

## Requirements

**User stories**:
- As the trader, I want a trend following strategy judged by rules that fit how it earns money, so that a real edge is not failed by design and a lucky one still fails.
- As the trader, I want daily momentum tested once on BTC, ETH, SOL and XRP together, so that I learn whether it earns a paper run on demo.

**Acceptance criteria** (shared; the children hold AC-4 to AC-22):
- **AC-1** (hash baseline): before any code, `verify.md` records the sorted module hash list sha256 of every `trading_core.node` module at the commit this feature's branch starts from, from `scripts/spikes/hash_check.py` (spec 0020 AC-1 method), plus the `code_hash` and `frozen_hash` of the strategy demo runs (`donchian_scalp_r2`, from `python -m trader.version`). At close out the same lists are recorded again, and every changed module is named with the reason. `recording/enums.py` is expected to change (AC-3), so `donchian_scalp_r2`'s `code_hash` changes and its deploy needs the manual promote.
- **AC-2** (migration): Alembic migration `0014_trend_kind` (file `2026MMDD_0014_trend_kind.py`, after `0013_alerter_fx_rates`) replaces the `ck_strategies_kind_valid` check (created by name in migration 0002; `models.py` has no matching `CheckConstraint`, so the model is unchanged and the autogenerate drift test must still pass) so `strategies.kind` accepts `'scalping'`, `'hour_level'`, and `'trend'`. Its downgrade restores the two value check, and refuses with a clear error while any `trend` row exists. No other schema change.
- **AC-3** (the kind): `StrategyKind` gains `TREND = "trend"`. Everything that branches on kind and does not name `trend` in this spec keeps its scalping or hour level behaviour, unchanged.

## Decision

**Chosen option**: a new `trend` kind with its own concentration, trial count, and paper rules (child 1), then one gate study of `tsmom` long only on four coins in one shared account (child 2).

Add `StrategyKind.TREND` and `thresholds.toml` version 3. For `trend`: count the 13 pre check trials in N, skip the direction check for a strategy declared long only, replace the top 5 % trades check with the luck tests already in the gate, keep `beats_buy_and_hold` hard, and warm each segment up on the bars before it. The 60 day, 8 trade paper floor is agreed but built in the paper follow up. Then build `tsmom` on the spec 0018 bar base and run it as spec 0022 ran `donchian_4h_pair`: study, holdout once, evaluate once.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`)

## Rationale

Reasoning, options, and the pre check evidence: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no new table or column. `strategies.kind` gains the value `trend` (AC-2). A trend study's `studies.thresholds` JSON carries the v3 `trend` table (child 1 AC-4). `promotions.metrics` gains `prior_trials`, `direction_applicable`, `top_trades_applicable` (child 1). `strategy_versions.config` of a `tsmom` version holds `long_only: true` (child 2 AC-13), which the gate reads.

**Cross child contract**:
- The gate reads `long_only` only from the final strategy version's frozen `config` (`strategy_versions.config["long_only"] is True`). A strategy that does not write the key is not long only.
- `tsmom`'s study is created only on code where `thresholds.toml` is version 3, so its frozen thresholds always hold the `trend` table.
- `tsmom` exposes `warmup_bars()`, and the lab's trend pre roll (child 1 AC-10a) reads it. No other strategy needs it.
- Every look at real candles for `tsmom` is a counted trial: `lab study`, then `lab holdout` once. No `lab backtest` on real candles.

**Value sourcing** (shared):
| Action | Value | Source |
|---|---|---|
| migration | allowed kinds | the three values of `StrategyKind` (AC-3) |
| baseline | module hash list | `scripts/spikes/hash_check.py` at the branch start commit (AC-1) |
| baseline | r2 hashes | `python -m trader.version` with `PAPER_STRATEGY=donchian_scalp_r2` (AC-1) |

**Key invariants**:
- Scalping and hour level studies, their verdicts, and their N never change because of this spec. Their thresholds are frozen per study, and every new rule is keyed on `strategy_kind == "trend"`.
- A promotion is never written by a run that saw holdout data before the study file was committed.

**Security model**: no new secret, endpoint, or role. The gate and lab run as today (`gate` on the VPS with `--no-deps`, `lab` on the laptop only). Kraken public endpoints need no keys.

**Configuration required**: none (no new env var or `kfs.env` line).

**Critical test scenarios** (shared):
- The migration upgrades a database with scalping and hour level rows, accepts a `trend` insert, and refuses `'other'`. The downgrade with no trend row restores the old check, and with a trend row refuses. Verifies **AC-2**.
- `StrategyKind("trend")` round trips, and `check_version` accepts a `trader.version` payload with kind `trend`. Verifies **AC-3**.

## Build plan

Tracer Bullet: first a thin thread from the new kind through a fixture study to a gate verdict, then real data, then the one real study.

1. [x] Record the AC-1 baseline in `verify.md`, satisfies **AC-1**
2. [x] `StrategyKind.TREND`, migration `0014_trend_kind` with its integration test, `check_version` test, satisfies **AC-2**, **AC-3**, **AC-11**
3. [x] `thresholds.toml` v3 and the `trend` gate rules in `trading_core.gate` and `recording.gate_queries`, with unit tests, satisfies **AC-4** to **AC-8**
4. [x] Thin thread: the `tsmom` rule, node, and registry entry, the lab accepting kind `trend` with its warmup pre roll, one fixture (synthetic candles) four coin study run end to end through `lab study`, `lab holdout`, and `gate evaluate` in the integration tests, satisfies **AC-10**, **AC-10a**, **AC-12** to **AC-14**, **AC-17**
5. [x] SOL and XRP pinned instruments, candles, and funding on the laptop, with data quality in `verify.md`, satisfies **AC-15**, **AC-16**
6. [x] Rerun the pre check as built on development data, results in `verify.md`; stop here if lookback 30 no longer passes, satisfies **AC-18a**
7. [x] PR, merge, deploy with `--no-deps`; you run the manual promote of `donchian_scalp_r2` (its code hash changed, AC-1), satisfies **AC-1**, **AC-2**
8. [x] Commit `apps/lab/studies/tsmom.toml`, rebuild the lab image with `GIT_COMMIT`, satisfies **AC-18**, **AC-22**
9. [x] Run `lab study`, `lab holdout tsmom`, push bundles, `gate import`, `gate evaluate` on the VPS, satisfies **AC-19**
10. [x] Results and the run log in its own commit, the AC-1 comparison again, the scope ticks, and a paper follow up enrolled on a pass, satisfies **AC-20**, **AC-21**, **AC-1**

## Consequences

**Positive**:
- Trend following can be judged at all; it no longer fails on two checks that only restate its design.
- Every luck test stays: DSR with a stricter N, bootstrap lower bound, PBO, fold share, coin share, drawdown, fee hurdle, holdout Sharpe, and beating buy and hold.
- SOL and XRP join the lab for any later study.

**Negative / tradeoffs**:
- The trend rules were chosen after seeing the development data. The holdout is still unseen for this rule, but a pass is weaker evidence than one under a gate fixed in advance. The Results section says so.
- BTC and ETH holdout years were already seen by specs 0020 and 0022 (other rules), and recent live BTC prices were seen during the 2026-10-31 session. Only SOL and XRP are fully fresh, so the label is "BTC and ETH holdout seen".
- Dropping the top trades check for `trend` is a deliberate loosening. In the pre check, 10 of 204 trades carried the whole mean (without them, −53 bps a trade), and with about 60 holdout trades and fat tails the bootstrap lower bound is a weak guard. One huge trade can carry a year, and only the bootstrap and the 60 trade floor stand against that.
- Trend runs warm up on the bars before each segment and other kinds don't, so the lab now treats kinds differently at segment start.
- The enum change alters `donchian_scalp_r2`'s code hash, so the deploy needs your manual promote (a TTY).
- The trade count is near the 60 holdout floor, so a quiet year fails on count alone.
- The 3 ATR stop and the 30 bar time stop were never in the first pre check. They change sizing and add exits, so AC-18a reruns the pre check as built, and a miss stops the spec before any study.

**Neutral**:
- Migration 0014 widens a check constraint and is reversible while no trend row exists.
- Paper trading is a follow up feature, architected only after a pass.

## Follow-up

- [ ] On a pass: enrol a paper feature (multi coin daily trader on demo replacing `donchian_scalp_r2`, a daily warmup seed from Kraken's public 1d candles, quotes for SOL and XRP) and run `/architect` for it. The trader uses the venue's live XRP instrument (tick 0.0001), not the lab pin's historical 0.00001 (child 2 AC-15). It carries the paper floor agreed on 2026-10-31: 60 days and 8 trades for `trend` (as `thresholds.toml` fields keyed on the kind, with a refusal for a trend paper step on thresholds that lack them).
- [x] On a fail: no revision in this spec. Any new trend idea starts from N = 17 or more.
- [ ] `apps/lab/AGENTS.md` and `packages/core/AGENTS.md` lines for the `trend` kind and the new coins via `/sync`.
