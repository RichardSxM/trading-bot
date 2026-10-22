# 0017. Test three scalping candidates, chosen and ordered by a data audit, through a cheap candle screen and the replay gate

**Date**: 2026-10-21 (amended 2026-10-22: AC-18, stop already through at the fill)
**Status**: In Progress

## Summary

This feature builds three scalping strategies and gives each one a recorded verdict from the spec 0015 gate. First a quick audit of the free Tardis days measures spreads, price moves, and order flow, so the run order and the spread limit come from data. The two bar based ideas (Bollinger and RSI, VWAP retest) first run a fast screen in the candle lab on years of 1 minute candles, and only survivors spend the roughly 40 hours a replay study takes. All three hold 1 to 15 minutes, enter with post only (maker) orders, and share one entry and exit framework, so they differ only in their signal.

## Structure

- [0001-bollinger-rsi.md](0001-bollinger-rsi.md): Bollinger and RSI mean reversion on 1 minute bars, gated by ADX in replay (supports the candidate set and the regime gate decision).
- [0002-vwap-retest.md](0002-vwap-retest.md): EMA trend plus a pullback to the day's VWAP, both directions, ungated (supports the candidate set).
- [0003-delta-divergence.md](0003-delta-divergence.md): price moves without matching aggressive volume, faded; replay only, gated by ADX (supports the order flow candidate).

The cross child contract (the shared framework, the audit, the screen, the order rule, and how verdicts are recorded) lives here. Each child holds its written hypothesis, exact rules, grid, and study files.

## Requirements

**User stories**:
- As the trader, I want each scalping idea written down as a hypothesis with exact rules before it runs, so a result can't be explained away afterwards.
- As the trader, I want cheap evidence (an audit, a candle screen) to filter ideas before a 40 hour replay, so compute goes to ideas that can clear the fees.
- As the trader, I want every candidate to end with a recorded gate verdict, pass, fail, or insufficient, so I know where scalping stands before going live.

**Acceptance criteria**:

*Data audit*
- **AC-1** (audit command): `lab audit [--from D] [--to D]` reads the Tardis catalog (`catalog-lab/tardis`) over every 1st of the month day in the range (default `from` 2022-04-01, default `to` the day before `thresholds.toml` `holdout_start`). A `--to` on or after `holdout_start` exits 64 ("the audit never reads holdout days"). A planned day with no manifest exits 2, listing every missing day and the `lab import-tardis` command, before anything is read. It plans clean hours with the spec 0008 segment planner (`trading_core.lab.segments`) and reads each segment through the existing `lab.stream.SegmentStream`, consuming its `QuoteTick` and `TradeTick` items only, with no `BacktestEngine`. It writes `lab-reports/audit-<YYYYMMDD>/audit.md` and `audit.json` (the date is the run's UTC date), connects to no database, and exits 0. It runs under the lab env guard (exit 78 with `TRADING_ENV=live`).
- **AC-2** (audit content): the summary math is pure, in `trading_core.lab.audit`, fed plain values (no Nautilus types). At each whole UTC second inside a clean segment, the last quote at or before it gives one sample of quoted spread in bps (`(ask − bid) ÷ mid × 10,000`) and touch depth (bid size + ask size, BTC). The JSON holds, pooled over all days and per day: spread p50, p90, p95, p99; touch depth p50; the median absolute mid move in bps over 1, 5, and 15 minute horizons (mid at t + h against mid at t, both inside one segment, sampled each whole second) and the share of samples at or above 10.5 bps and 15 bps; trades per minute p50; aggressive buy share of traded size; the per minute absolute delta (buy size − sell size, from the trade aggressor side) p50 and p90; per UTC hour, spread p50 and median absolute 5 minute move; per day, clean hours, dropped hours with reasons, and mean absolute funding rate; and `reachable`, for each entry cost (`maker_entry`: maker + taker fee = 7 bps; `taker_entry`: 2 × taker fee = 10 bps, both in bps), whether the pooled median absolute 5 minute move in bps is at least `fee_hurdle_k` (1.5, from `thresholds.toml`) × that cost. The day's mean absolute funding rate is time weighted over its clean segments, each segment starting from the last `funding_rate_update` at or before its start (read through `lab.stream.read_context`); a segment with no rate before it counts from its first rate. Two runs on the same data give byte identical `audit.json` apart from its `generated_at` field. Percentiles use the nearest rank method.
- **AC-3** (audit record and order): before the first candidate study (screen or replay) runs, `verify.md` of this spec records the audit's batch folder, its pooled spread p95, its median absolute 5 minute move, its `reachable` flags, and the resulting run order under the *Order rule* below. Each child's replay run file sets `max_spread_bps` to that pooled spread p95 as a `Decimal`, rounded to 2 decimal places with `ROUND_CEILING`, before its first replay study, and it is never changed afterwards.

*Shared framework*
- **AC-4** (indicators): `trading_core/strategy/scalp_indicators.py` (new, pure, `Decimal` only) holds `Sma`, `Bollinger` (middle = SMA(n), bands = middle ± k × population standard deviation of the last n closes, square root via `Decimal.sqrt` in the default context), `Rsi` (Wilder smoothing, seeded with the simple mean of the first n gains and losses; 100 when the average loss is 0), `Atr` (Wilder, true range), `Adx` (Wilder +DI, −DI, DX, ADX; first value after 2n bars), `SessionVwap` (Σ typical price × volume ÷ Σ volume since its anchor, typical = (high + low + close) ÷ 3, reset by `anchor(ts)`; no value while Σ volume is 0), and `DeltaWindow` (the signed traded size of the last n closed 1 minute buckets: a buyer aggressor trade adds its size, a seller one subtracts, `NO_AGGRESSOR` adds 0). Each returns `None` until warm. Unit tests check each one against hand worked values. `trading_core/strategy/indicators.py` is not edited.
- **AC-5** (shared strategy base): `trading_core/node/scalp_base.py` holds `ScalpStrategy(KfsStrategy)` and `ScalpConfig(KfsStrategyConfig)`, built with the trader's arguments `(config, gate, blocks, record)` like `MakerProbe`. It subscribes quotes and `<instrument_id>-<bar_spec>-INTERNAL` bars (`bar_spec` defaults to `1-MINUTE-LAST`, the only value the children use; the candle lab reads the same field) plus the regime bars of AC-6 when set. On each 1 minute bar close, while flat with no entry working and not blocked, it asks the child's pure signal function (`trading_core/strategy/<name>.py`) for a `ScalpSignal(side, stop_px, target_px, inputs)` or `None`. It never decides before every indicator the child needs is warm. A signal then passes, in order: the entry blocks (spec 0006, as `KfsStrategy` does today); the spread cap (when `max_spread_bps` is set and the last quote's spread in bps exceeds it, it records an `enter` decision rejected with `risk_reason` `spread_cap` and places nothing); the reward floor (when `min_target_bps` is set and `|target_px − mid| ÷ mid × 10,000` is below it, it records an `enter` decision rejected with `risk_reason` `target_too_close`); the stop side check (a long whose `stop_px` is not below the mid, or a short whose `stop_px` is not above it, records an `enter` decision rejected with `risk_reason` `invalid_stop`, so `check_entry` never sees it); and `check_entry` with `stop_px` and no `fixed_quantity`, so spec 0011 sizes it from `risk_per_trade` under the run's caps. An approved entry is a post only limit at the last quote's best bid (buy) or best ask (sell) with `entry_mode = "maker"`, or a market order with `entry_mode = "taker"`. A maker entry still unfilled `entry_timeout_s` after it was submitted has its open remainder cancelled. Any filled part is kept (no taker chase). On every entry fill, the strategy makes its one reduce only stop market order at `stop_px` match the position's quantity (submitted on the first fill, its quantity modified on later fills). While a position is open: the target exit fires on the first quote whose bid (long) is at or above `target_px`, or whose ask (short) is at or below it; the time stop fires `time_stop_min` minutes after the first entry fill. Each exit is a reduce only market order with an `exit` decision whose reason is `target` or `time_stop`. A stop fill records an `exit` decision with reason `stop`. Before any exit is submitted, and on any stop fill or `flatten`, the strategy first cancels the entry's open remainder. An entry fill that still arrives after that (a race with the cancel) is closed at once by a reduce only market exit with reason `late_entry_fill`. Once one exit path fills, the other resting exit orders are cancelled. A stop modify rejected because the stop already triggered is logged, not retried. When the position is flat, the stop is cancelled. One position at a time. `flatten(halt_id)` closes through the same exit path with the spec 0010 tags. `spread_cap`, `target_too_close`, and `invalid_stop` refusals are logged at info severity (beside the existing info refusals in the strategy's refusal severity map). Every lab report (replay and candle) adds a table of `enter` decisions per reason (approved, and each rejection reason) and per day, so the signal rate is visible apart from the fill rate. Every `enter` decision's `inputs` carry `mid`, `spread_bps`, `stop_px`, `target_px`, the child's `inputs`, and `adx` when the regime gate is set.
- **AC-6** (regime block): `BlockReason` gains `REGIME = "regime"`, self clearing, with plain words "The market is trending (ADX above the limit), entries wait". With `regime_adx_max` set, the strategy also subscribes `<instrument_id>-<regime_bar_spec>-INTERNAL` bars (default `5-MINUTE-LAST`) and feeds an `Adx(regime_adx_period)` (default 14). From start until the ADX is warm, and whenever an ADX value is above `regime_adx_max`, `regime` is active in `EntryBlocks`. A value at or below the limit clears it. On stop the strategy clears it. With `regime_adx_max` unset, no regime bars are subscribed and the reason is never added. A signal while `regime` is active records the usual `blocked:regime` rejection.
- **AC-7** (registry): `apps/lab/src/lab/strategies.py` registers `bollinger_rsi`, `vwap_retest`, and `delta_divergence`, each with kind `scalping`, `default_entry_mode` `"maker"`, and its `trading_core.node` module. `StrategyEntry` gains `candle_screen: bool = False`, true for `bollinger_rsi` and `vwap_retest` only. Their run fields depend on the lab: in a candle run they are the candle entries' fields (`stale_quote_s` 300 and `stale_check_s` 60, as `_candle_fields` gives today); in a replay run they are the replay fields (as `_random_taker_fields`, no stale block). `ScalpConfig` carries `stale_quote_s` and `stale_check_s` with those candle values as defaults.
- **AC-18** (stop already through at the fill, amended 2026-10-22): on the entry's first fill, before the stop is submitted, the base checks whether the stop is already through. For a long that means the last quote's bid is at or below `stop_px`, or the average fill price is at or below it. For a short, the ask is at or above `stop_px`, or the average fill price is at or above it. When it is through, the base places no stop. It cancels the entry's open remainder and sends a reduce only market exit with an `exit` decision whose reason is `stop_through`, logged at info severity. It adds no entry block and records no risk event, so the next signal can trade. Any later entry fill is closed by the existing `late_entry_fill` rule. Only the first fill is checked; later fills modify the stop that is already resting, as AC-5 says. A stop that is submitted and then rejected, denied, cancelled by someone else, or not accepted in time is still handled as spec 0006 says (`STOP_NOT_ACCEPTED` block, critical risk event, market exit), because once the base saw a quote that allowed the stop, a rejection means something it did not expect. This covers spec 0018 AC-3's fill price case too.

*Candle screen*
- **AC-8** (screen admission): `lab backtest` and a `lab study` with `lab = "candle"` accept a `scalping` strategy only when its entry has `candle_screen`; any other non `hour_level` strategy still exits 64. `lab holdout` on a candle study whose strategy kind is `scalping` exits 64 ("a candle screen never holds out") before taking its lock or reading data. `gate evaluate` on such a study exits 64 ("a candle screen is not a gate study") and writes nothing. The gate has no registry, so it reads both facts from Postgres: `studies.lab = 'candle'` and the study's `strategies.kind = 'scalping'`. In `lab holdout` the check comes right after the study is loaded, before the advisory lock. In practice screens are never pushed to the VPS; the gate check is the second layer. Its development runs still count toward the strategy's trial count N (spec 0015 AC-11).
- **AC-9** (screen rule): `lab screen <study>` (laptop, `lab_app` SELECT only) exits 64 unless the study is a candle study of a `scalping` strategy whose every development grid point has a `completed` run. It picks the final grid point with the rule `lab holdout` uses (best development `risk_adjusted_ratio`, ties to the lowest index, the same shared function). It computes the walk forward Sharpe with the pure `trading_core.gate` folds over the study's development `run_daily_equity` and the study's frozen thresholds. It reads the final point's `run_measures.avg_gross_edge` and `avg_round_trip_cost`, and its trades per day = `run_results.trade_count` ÷ the count of its `run_daily_equity` rows. The pure `trading_core.lab.screen.screen_verdict` passes when walk forward Sharpe > 0, edge ≥ `fee_hurdle_k` × cost, and trades per day ≥ `SCREEN_MIN_TRADES_PER_DAY` (17, the ceiling of 200 ÷ 12, a constant in that module); otherwise it fails with every failing reason. It prints the verdict, reasons, and figures, writes `lab-reports/screen-<study>/screen.json`, and exits 0 on pass and 3 on fail. A missing figure (no folds, NULL edge or cost, no daily rows) fails the screen with reason `missing:<figure>`. Edge and cost are compared in USD per trade, as the gate's `fee_hurdle` does; the candle lab's slippage sits in the gross edge, not the cost. The final grid point (best whole development Sharpe) and the walk forward series (best in sample point per fold) may differ; that is accepted, as in the gate. A screen that fails only on `trades_per_day` is recorded as "trade rate", which says the candle lab's trade through fills or the signal is too rare, not that the hypothesis is wrong.

*Studies and verdicts*
- **AC-10** (candidate studies): each child's study files exist under `apps/lab/studies/` exactly as the child lists them. Replay studies use `source = "tardis"`, `from = 2022-04-01`, `to = 2025-10-18`, maker and taker fees 0.0002 and 0.0005, `latency_ms = 25`, `starting_balance_usd = "10000"`, `leverage = "2"`, `[risk] max_order_notional = "2000"`, `max_leverage = "2"`, `risk_per_trade` left at its default, `[study] entry_mode = "maker"`, and a grid of 4 points. Candle screens follow `apps/lab/studies/macd_baseline.toml`: `from = 2023-01-01`, `to = 2025-10-18`, `seed = 7`, the same fees, balance, leverage, and `[risk]` as the replay studies, `slippage_bps = "2"`, `[study] lab = "candle"`, `seed = 42`. They use the replay study's strategy config and grid, with `entry_mode = "maker"` in `[strategy.config]` and `max_spread_bps` unset (the candle lab's spread is synthetic).
- **AC-17** (delta trade rate pre check): before `delta_divergence`'s replay study, `lab replay` runs its grid point `lookback_bars = 5`, `move_atr = "1.5"` (the most permissive) from a scratch run file over 2022-10-01, 2023-10-01, 2024-10-01, and 2025-10-01. If its approved `enter` decisions average under 17 a day (from the AC-5 report table), `verify.md` records delta as "not run: trade rate" with the counts, and its study does not run. These days are development days, so nothing touches the holdout, and the runs are not part of any study.
- **AC-11** (run order and verdicts): candidates run in the order the *Order rule* gives, one at a time, each to a recorded result before the next starts. A bar idea goes to replay only when a screen of it passes AC-9. Each replay study runs `lab study`, `lab holdout`, `push-bundles.sh`, `gate import`, and `gate evaluate` on the VPS. `verify.md` records, per candidate: each screen's verdict, reasons, and figures; the replay study's `promotions` id, verdict, and reason codes; or "not run" with the screen reason. The feature is done when all three candidates have one of these recorded.
- **AC-12** (one revision): after a replay verdict of `fail` or `insufficient`, a candidate may get at most one revision, changing one design dimension. It is written into its child spec as a `## Revision` section by an `/architect` amendment before its study runs, under a new study name `<name>_r1`, and its verdict is recorded like the first. Then the candidate is closed.

*Per candidate*
- **AC-13** (`bollinger_rsi`): the strategy implements exactly the rules of [0001-bollinger-rsi.md](0001-bollinger-rsi.md). Its pure signal function's unit tests cover a long, a short, a signal refused by non flat bands, and warmup.
- **AC-14** (`vwap_retest`): the strategy implements exactly the rules of [0002-vwap-retest.md](0002-vwap-retest.md). Unit tests cover a long retest, a short retest, a touch that closes through VWAP (no signal), the anchor reset at 00:00 UTC, and the warmup after an anchor.
- **AC-15** (`delta_divergence`): the strategy implements exactly the rules of [0003-delta-divergence.md](0003-delta-divergence.md). Unit tests cover a bearish divergence (short), a bullish one (long), a move with matching delta (no signal), `NO_AGGRESSOR` trades counting 0, a trade at a minute boundary landing in the bucket of its `ts_event` minute whatever its arrival order, and a late trade counted in `late_trades`.
- **AC-16** (both labs, same code): a `lab replay` of each strategy over 2025-09-01, from a scratch run file under `lab-reports/scratch/` with `min_target_bps` and `regime_adx_max` unset (scratch only, so a trade is likely), completes with at least one closed trade, and a `lab backtest` of `bollinger_rsi` and `vwap_retest` over 2025-06 (same scratch rules) completes with at least one closed trade. `delta_divergence` in `lab backtest` or a candle study exits 64 (no `candle_screen`). A determinism rerun of one replay day gives the identical report JSON (spec 0008 AC-12).

### Order rule (written before any result)

1. Run `lab audit` and record it (AC-3).
2. A candidate whose entry cost has `reachable: false` runs after every candidate whose entry cost is reachable. All three candidates enter maker, so with the current set this flag either moves none of them or warns about all of them. When it warns about all, `verify.md` says so and the order below stands.
3. Within that, both bar ideas' candle screens run first (gated and ungated, four studies, hours each). Then replay studies run one at a time: `bollinger_rsi`, then `vwap_retest`, for each whose screen passed, then `delta_divergence` once its AC-17 pre check passes.
4. For a bar idea, replay runs the version its child names (`bollinger_rsi` gated, `vwap_retest` ungated). If only the other version's screen passed, that version runs instead, and `verify.md` records the switch.

## Decision

**Chosen option**: Option 2: An audited, screened, one at a time set of three maker entry candidates on one shared framework.

Three candidates share one strategy base and differ only in a pure signal function; a data audit fixes the spread cap and run order, a candle screen filters the bar ideas, and only the spec 0015 replay gate gives the verdict.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

```
Tardis dev days ─► lab audit ─► audit.json ─► spread cap, order (verify.md)
                                                   │
bar ideas:  lab study (candle screen, ungated + ADX) ─► lab screen ─► pass? ─┐
                                                                             ▼
all ideas:  lab study (replay, maker) ─► lab holdout ─► push-bundles.sh ─► gate import ─► gate evaluate ─► verdict (verify.md)
```

**Code layout**:
- `trading_core/strategy/scalp_indicators.py` (pure): the AC-4 indicators.
- `trading_core/strategy/scalp.py` (pure): `ScalpSignal`, the spread in bps, and the target and time stop checks used by the base.
- `trading_core/strategy/bollinger_rsi.py`, `vwap_retest.py`, `delta_divergence.py` (pure): each child's signal function and state.
- `trading_core/node/scalp_base.py`: `ScalpStrategy`, `ScalpConfig` (AC-5, AC-6).
- `trading_core/node/bollinger_rsi.py`, `vwap_retest.py`, `delta_divergence.py`: thin subclasses that feed bars (and trades, for delta) to the pure state.
- `trading_core/lab/audit.py`, `trading_core/lab/screen.py` (pure).
- `trading_core/strategy/blocks.py`: `REGIME` and its plain words.
- `apps/lab`: `audit` and `screen` subcommands, the registry entries and `candle_screen`, the kind checks of AC-8, run and study files.
- `apps/gate`: the AC-8 refusal in `gate evaluate`.

**`ScalpConfig` fields** (beyond `KfsStrategyConfig`): `bar_spec` (default `"1-MINUTE-LAST"`), `stale_quote_s` (default 300), `stale_check_s` (default 60), `entry_mode` (`"maker"` | `"taker"`, default `"maker"`), `entry_timeout_s` (default 30), `time_stop_min` (required), `max_spread_bps` (`Decimal | None`, default None), `min_target_bps` (`Decimal | None`, default None; every child sets `"10.5"`, 1.5 × the 7 bps maker entry round trip), `regime_adx_max` (`Decimal | None`, default None), `regime_adx_period` (default 14), `regime_bar_spec` (default `"5-MINUTE-LAST"`). Each child adds its own fields.

**Data model sketch**: no migration. Confirmed in design.

| What | Table or file | Change |
|---|---|---|
| The three strategies | `strategies` (kind `scalping`), `strategy_versions` | rows, through `ensure_strategy_version` |
| Screens, studies, holdouts | `studies`, `study_runs`, `run_daily_equity`, `run_measures`, run tables | rows (spec 0015) |
| Verdicts | `promotions`, `promotion_folds`, `promotion_evidence` | rows, through `gate evaluate` |
| `regime` block | `BlockReason` (code), `decisions.risk_reason` (text) | no schema change |
| Audit, screen results | `lab-reports/audit-<date>/`, `lab-reports/screen-<study>/` | files |
| Recorded results | this spec's `verify.md` | text |

**State transitions** (per candidate, recorded in `verify.md`): `written` → (bar ideas) `screened` (pass or fail) → `studied` (replay development done) → `held out` → `evaluated` (verdict) → optionally one `revised` → `evaluated` → `closed`. A bar idea whose screens both fail goes from `screened` to `closed` with "not run".

**API surface**:

| Command | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `lab audit [--from D] [--to D]` | CLI | UTC days (opt) | `audit.md`, `audit.json` | none (files only) | 64 `--to` in holdout or bad day; 2 missing manifest; 78 live env |
| `lab screen <study>` | CLI | study name (req) | verdict, reasons, figures, `screen.json` | `lab_app` SELECT | 64 not a candle scalping study or incomplete; 3 screen fail |
| `lab study <studyfile>` | CLI | study TOML | as spec 0015 | `lab_app` | as spec 0015; 64 a non screen scalping strategy in a candle study |
| `lab holdout <study>` | CLI | study name | as spec 0015 | `lab_app` | as spec 0015; 64 a candle screen |
| `gate evaluate <study>` | CLI (VPS) | study name | as spec 0015 | `gate_app` | as spec 0015; 64 a candle screen |

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| `lab audit` | day list | every 1st of the month in `from`..`to`; `to` default = `holdout_start − 1` from `trading_core/gate/thresholds.toml` |
| `lab audit` | clean hours, segments | `trading_core.lab.segments` over the Tardis manifests and gap files |
| `lab audit` | spread, depth samples | the last `QuoteTick` from `SegmentStream` at or before each whole second |
| `lab audit` | delta, trades per minute | `TradeTick.aggressor_side` and `size` from `SegmentStream` |
| `lab audit` | funding per day | the day's `funding_rate_update` rows in the catalog |
| `lab audit` | entry costs, `fee_hurdle_k` | constants 0.0002 and 0.0005 (the pinned instrument's fees, spec 0008 AC-11); `fee_hurdle_k` from `thresholds.toml` |
| replay run file | `max_spread_bps` | the recorded audit's pooled spread p95, rounded up to 2 places (AC-3) |
| `ScalpStrategy` | spread at signal | the cache's last quote: `(ask − bid) ÷ mid × 10,000` |
| `ScalpStrategy` | entry price (maker) | the last quote's best bid (buy) or ask (sell) |
| `ScalpStrategy` | quantity | `check_entry` with `stop_px` (spec 0011 AC-1), capped by `max_order_notional` |
| `ScalpStrategy` | stop and target prices | the child's signal function, from the closed bar and its indicators |
| `ScalpStrategy` | stop through at the fill (AC-18) | the cache's last quote (bid for a long, ask for a short) and the entry's average fill price, against `stop_px` |
| `ScalpStrategy` | time stop moment | the first entry fill's `ts_event` + `time_stop_min` |
| `ScalpStrategy` | ADX | `Adx(regime_adx_period)` over `regime_bar_spec` bars |
| `lab screen` | final grid point | the shared final point function `lab holdout` uses |
| `lab screen` | walk forward Sharpe | `trading_core.gate` folds over the study's development `run_daily_equity`, the study's frozen thresholds |
| `lab screen` | edge, cost | the final point's `run_measures.avg_gross_edge`, `avg_round_trip_cost` |
| `lab screen` | trades per day | final point `run_results.trade_count` ÷ its count of `run_daily_equity` rows |
| `lab screen` | `fee_hurdle_k` | the study's frozen `thresholds` |
| `verify.md` | verdicts | `promotions.verdict`, `reasons` of the VPS `gate evaluate` |

**Key invariants**:
- No candidate study, screen, or audit reads a holdout day before its own `lab holdout`.
- The spread cap, every threshold, and every grid are written before the run they apply to, and never tuned afterwards.
- No file already in another strategy's import closure is edited, except `strategy/blocks.py` (the `REGIME` reason).
- A candle screen never holds out and is never a gate study.
- Every exit and stop is reduce only. One position at a time.
- Win rate is never a selection rule.

**Security model**: no new role, secret, or grant. `lab audit` touches files only; `lab screen` uses `lab_app` SELECT. Nothing here places a live order (the lab exits 78 on `TRADING_ENV=live`), and no strategy is wired into the trader. The repo is public, so study files and audit numbers are world readable, which is fine.

**Configuration required**: none new. All settings live in run and study files.

**Critical test scenarios**:
- Happy path: the audit runs over the development days, its spread p95 goes into `bollinger_rsi`'s run file, its screens run, `lab screen` passes one, and the replay study goes through holdout, push, import, and evaluate to a recorded verdict. Verifies **AC-1**, **AC-3**, **AC-9**, **AC-10**, **AC-11**.
- Holdout safety: `lab audit --to 2025-10-19` exits 64; `lab holdout bollinger_rsi_screen` exits 64; `gate evaluate bollinger_rsi_screen` exits 64. Verifies **AC-1**, **AC-8**.
- Audit determinism and math: two runs give identical JSON apart from `generated_at`; a hand built quote series gives the expected percentiles and moves; a day missing its manifest exits 2. Verifies **AC-1**, **AC-2**.
- Maker entry: an unfilled entry is cancelled at the timeout and the strategy returns to flat; a partial fill keeps its part, and the stop quantity follows each fill; a target exit while the remainder rests cancels the remainder first; a remainder fill after the stop filled is closed with `late_entry_fill`; a long signal with its stop above the mid records `invalid_stop`. Verifies **AC-5**.
- Stop through at the fill: a long entry fills while the bid is already at or below `stop_px`, so no stop is submitted, a `stop_through` exit closes the position, no block is added, and the next signal enters; the same holds for a short against the ask, and for an average fill price beyond the stop; a stop the venue rejects after the check passed still sets `STOP_NOT_ACCEPTED`. Verifies **AC-18**.
- Delta pre check: under 17 approved entries a day records "not run: trade rate" and no study runs. Verifies **AC-17**.
- Exits: a target touch by the bid exits long; the time stop exits at `time_stop_min`; a stop fill leaves no exit order behind. Verifies **AC-5**.
- Spread cap, reward floor, and regime: a quote wider than the cap records `spread_cap` and places nothing; a target 10 bps away with `min_target_bps = 10.5` records `target_too_close`; ADX above the limit adds `regime`, at or below clears it, unwarm ADX blocks. Verifies **AC-5**, **AC-6**.
- Screen rule: each failing reason alone fails the screen; a NULL edge fails with `missing:avg_gross_edge`; an incomplete grid point exits 64. Verifies **AC-9**.
- Admission: `delta_divergence` in `lab backtest` exits 64; `macd_baseline` in a replay study still behaves as before. Verifies **AC-7**, **AC-8**, **AC-16**.
- Per candidate rules: the AC-13, AC-14, AC-15 signal tests.
- Both labs: AC-16's one day replays, one month backtests, and the determinism rerun.

## Build plan

Tracer Bullet: the audit first (the scope asks for it before any candidate), then one candidate end to end through both labs, then the others thicken the same thread, then the studies run.

**Prerequisite** (met: shipped in PR #86, 2026-10-21): spec 0015 tasks 20 and 21 (AC-25, entry mode in the hash; AC-26) and spec 0016's 5% off tick guard are built, so a maker study gets its own `frozen_hash` and every Tardis day is guarded.

**Ordering rule**: task 9's `BlockReason.REGIME` edit to `strategy/blocks.py` lands, and is deployed with the placeholder's manual promotion, before any candidate study starts (task 12 onward). That edit changes the `frozen_hash` of every strategy importing `blocks.py` (every existing one: `placeholder_timed`, `random_taker`, `maker_probe`, `macd_baseline`, `buy_and_hold`), so a study already in development when it lands cannot hold out ("start a new study").

**Slice 1: audit**
1. `trading_core/lab/audit.py` (pure summaries, nearest rank percentiles, `reachable`) with hand worked tests; `lab audit` over `SegmentStream` with the holdout refusal, missing day exit, and reports. Satisfies **AC-1**, **AC-2**.
2. Run `lab audit` over the development days (import any missing ones first); record the result and the order in `verify.md`. Satisfies **AC-3**.

**Slice 2: thin thread, `bollinger_rsi` through both labs**
3. `scalp_indicators.py`: `Sma`, `Bollinger`, `Rsi`, `Atr`, with tests. Satisfies **AC-4** (partial).
4. `strategy/scalp.py` and `node/scalp_base.py`: signal hook, spread cap, maker and taker entry, timeout and partial fills, stop sizing, target and time stop exits, `flatten`, decision inputs, with node tests in a `BacktestEngine` fixture. Satisfies **AC-5**.
5. `strategy/bollinger_rsi.py` and `node/bollinger_rsi.py`; registry entry with `candle_screen`. Satisfies **AC-7** (partial), **AC-13**.
6. AC-8 admission in `lab backtest`, `lab study`, `lab holdout`, and `gate evaluate`. Satisfies **AC-8**.
7. One day `lab replay` and one month `lab backtest` of `bollinger_rsi`, plus the determinism rerun. Satisfies **AC-16** (partial).
8. `trading_core/lab/screen.py` and `lab screen`, with tests. Satisfies **AC-9**.

**Slice 3: regime gate**
9. `Adx` in `scalp_indicators.py`; `BlockReason.REGIME` with its plain words; regime bars and block in the base. Satisfies **AC-4** (partial), **AC-6**.

**Slice 4: the other two candidates**
10. `SessionVwap`; `vwap_retest` pure and node; registry entry. Satisfies **AC-4** (partial), **AC-7** (partial), **AC-14**, **AC-16** (partial).
11. `DeltaWindow`; `delta_divergence` pure and node (subscribes trade ticks); registry entry without `candle_screen`. Satisfies **AC-4**, **AC-7**, **AC-15**, **AC-16**.

**Slice 5: studies and verdicts**
12. Write every study file the children list, with the audit's spread cap. Satisfies **AC-10**, **AC-3**.
12b. (amendment) The AC-18 check in `node/scalp_base.py`, with node tests. Rebuild the lab image before any screen. The first `bollinger_rsi_screen` study (run 2026-10-22 before AC-18, stopped partway) is void: `verify.md` records it, its rows stay (they still count toward N), and the screen reruns under the new study name `bollinger_rsi_screen_v2` (file `bollinger_rsi_screen_v2.toml`, otherwise identical), because `lab study` would resume that study and mix old and new code. Satisfies **AC-18**.
13. Run the four candle screens and `lab screen` on each; record them. Satisfies **AC-9**, **AC-11** (partial).
13b. Before `delta_divergence`'s study, run its AC-17 pre check and record it. Satisfies **AC-17**.
14. Run the replay studies in the *Order rule* order, each through holdout, push, import, and `gate evaluate`; record each verdict, plus any single revision through `/architect`. Satisfies **AC-11**, **AC-12**.

## Consequences

**Positive**:
- Every idea is judged by rules written before its result, and the cheap screen and audit keep the 40 hour replays for ideas that can clear the fees.
- One shared base means the candidates differ only in their signal, so their verdicts compare like for like.
- The strategies are trader ready classes, so a passing one needs wiring, not a rewrite.

**Negative / tradeoffs**:
- Expect `fail` or `insufficient` verdicts. Maker round trips need about 10.5 bps gross edge per trade at about 17 trades a day, and `maker_probe` already showed adverse selection.
- Compute: each replay study is about 40 hours of laptop time and each holdout about 6 more. Three candidates plus revisions can be over a week of machine time.
- Screen studies count as trials, so a bar idea reaches replay with a higher N and a lower deflated Sharpe than one with no screen.
- The candle screen is optimistic for maker entries in one way (a whole minute's range) and pessimistic in another (fills only on trade through), so a screen pass is a filter, never evidence of an edge.
- Editing `strategy/blocks.py` changes the `frozen_hash` of every strategy that imports it, including the running placeholder, so the deploy that carries `REGIME` needs a manual paper promotion of the placeholder first (AC-26 refuses it cleanly until then).
- Each replay segment starts cold, so warmup (up to about 2 hours 20 minutes for a 5 minute ADX) takes trading time from every replay day.
- All exits are taker, so every round trip pays the taker fee once.
- A `stop_through` exit takes the loss at market, at or past the stop price, so in a fast move it can lose more than the planned risk. That is the same loss a stop would have taken, without blocking the strategy.
- BTC only: Tardis free days cover `PF_XBTUSD` only.

**Neutral**:
- No migration, no new service. Two lab subcommands, one registry field, one block reason.
- Results live in `verify.md` and on the `/strategies` page, not in a new table.

## Follow-up

- [x] Enrolled on the scope's Deferred list: "Paper trade a passing scalping candidate" (it replaces the placeholder; one strategy at a time).
- [ ] Possible later revision dimensions, not decided here: maker target exits (a resting reduce only limit), the scaled exit, a session filter from the audit's per hour numbers, a taker version of `delta_divergence`.
- [ ] Order book imbalance, liquidity sweep, EMA pullback, and short period RSI stay in the scope as unchosen ideas.
- [ ] `/sync`: `apps/lab/AGENTS.md` should list `lab audit`, `lab screen`, and the `candle_screen` rule.
