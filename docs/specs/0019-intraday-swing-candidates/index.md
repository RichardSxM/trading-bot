# 0019. Intraday swing candidates: four BTC strategies on 15 minute bars, held 2 to 8 hours, on spec 0018's shared base

**Date**: 2026-10-23
**Status**: In Progress

## Summary

Scalping (spec 0017) lost because each trade aimed for moves about as small as the fees. This feature tests four strategies on 15 minute bars that hold a trade for 2 to 8 hours, so the move each one aims for is several times the cost of a trade. They reuse spec 0018's shared strategy base unchanged (market entries, a protective stop, sizing by risk), run in the same candle lab over BTC history since April 2022, and go through the same promotion gate. They count in the same family as the hour level strategies, so the overfitting check stays strict. Each hypothesis is committed before its result, and every verdict, pass or fail, is written back into its file.

## Structure

The shared contract (bars, family, run protocol, intraday helpers) lives in this file. Each candidate is a child spec with its hypothesis, exact rules, settings, grid, tests, and a results section. Run order is the file order. Candidates 3 and 4 are built only after the checkpoint in AC-10.

| # | File | Strategy (study) | What it is |
|---|---|---|---|
| 1 | [0001-connors-rsi-15m.md](0001-connors-rsi-15m.md) | `connors_rsi` (`connors_rsi_15m`) | The existing `connors_rsi` module at 15 minutes. No new code: the tracer that proves 15 minute bars end to end. Its holdout is labelled "holdout seen". |
| 2 | [0002-ema-pullback.md](0002-ema-pullback.md) | `ema_pullback` | Trend following: 8 EMA above 21 EMA, a pullback to the 8 EMA with RSI(7) at 30 to 50, enter on a close back above it. |
| 3 | [0003-day-vwap-reversion.md](0003-day-vwap-reversion.md) | `vwap_reversion` | A close stretched at least 2 ATR from the UTC day's VWAP, on a turning bar, aims back at the VWAP. |
| 4 | [0004-us-open-range-breakout.md](0004-us-open-range-breakout.md) | `us_open_breakout` | The first 30 or 60 minutes after the 09:30 New York open, then a close beyond that range, at most once per US weekday. |

Left out of round 1: Bollinger RSI and NR4 at 15 minutes (they wait for spec 0018's `bollinger_rsi_1h` and `nr4_breakout` modules), busy hours filters, regime gates, maker entries, 5 and 30 minute bars, and ETH.

## Requirements

**User stories**:
- As the trader, I want strategies that hold minutes to hours tested with every cost, so I learn whether a move per trade well above the fees gives a real edge where scalping did not.
- As the trader, I want each hypothesis committed before its result exists, and every verdict kept in git, fails included.
- As the trader, I want these trials counted with the hour level ones, so trying more ideas on the same data makes passing harder, not easier.

**Acceptance criteria**:

*Shared contract*
- **AC-1** (same base, 15 minute bars): every candidate runs on spec 0018's `BarRuleStrategy` and `BarRule` contract (spec 0018 AC-1 to AC-5) with `bar_spec = "15-MINUTE-LAST"`. `strategy/bar_rule.py`, `strategy/bar_indicators.py`, `strategy/indicators.py`, `node/bar_rule.py`, and every spec 0018 candidate module stay byte identical (checked by `gate versions`: every existing `code_hash` is the same before and after this feature). A candidate whose idea needs a base change is out of scope here and goes back to `/architect`.
- **AC-2** (registry and kind): each new candidate (`ema_pullback`, `vwap_reversion`, `us_open_breakout`) has a pure rule `trading_core/strategy/<name>.py` and a thin node module `trading_core/node/<name>.py` (config class defaulting `bar_spec` to `"15-MINUTE-LAST"`, plus a `BarRuleStrategy` subclass that builds the rule), registered in `apps/lab/src/lab/strategies.py` with kind `hour_level`, so it passes the spec 0014 AC-19 look ahead prefix test and counts in the spec 0018 AC-19 family (kind `hour_level`, lab `candle`). `connors_rsi` keeps its existing registry entry and module; only a new study file is added for it.
  - Look ahead test edits (`apps/lab/tests/test_lookahead.py`): the expected `HOUR_LEVEL` list gains each new name as it is registered. `CONFIGS` gains a loose config per new candidate that makes it decide on the synthetic fixture (for example `ema_pullback` with `rsi_low` 1 and `rsi_high` 50, `vwap_reversion` with `stretch_atr` `"0.5"` and `min_day_bars` 1, `us_open_breakout` with `range_bars` 1 and `entry_window_bars` 16). If one still makes no decision on the fixture, the fixture is extended (more weekdays, a wider swing) until it does; the `assert full` check is never relaxed. A `connors_rsi` case at `bar_spec = "15-MINUTE-LAST"` is added, so the 15 minute path is prefix tested too.
  - Cut times (`trading_core/lab/lookahead.py`): `cut_times` takes the strategy's bar interval and draws each cut strictly between two bar closes (for 15 minute bars: 1 to 14 minutes past a quarter hour), so the existing promise "a cut never lands on a bar close" holds. With the 1 hour interval it draws exactly what it draws today. `lookahead.py` is not in any strategy's code closure, so no `code_hash` moves.
- **AC-3** (intraday helpers): the new pure module `trading_core/strategy/intraday.py` provides `DayVwap` and `ny_open_utc_ns(day: date) -> int` exactly as *Intraday helpers* below, with unit tests on hand worked values (a VWAP across a UTC midnight reset, a zero volume bar, the March and November US daylight saving switch weeks of 2022, 2023, 2024, and 2025). It is imported only by `vwap_reversion` and `us_open_breakout`, and it is frozen after the first study that has it in its closure (spec 0018 AC-7 rule: a later change goes into a new module).
- **AC-4** (config validation): each candidate config refuses bad values in its constructor with `ValueError` (its child spec *Settings* checks, plus the base's), so `lab study` exits 64 before any run. A `bar_spec` other than `15-MINUTE-LAST` is refused by the three new candidates, because their bar counts (warmup, windows, holds) are written for 15 minutes.
- **AC-5** to **AC-8** (candidate rules): `connors_rsi` at 15 minutes (AC-5), `ema_pullback` (AC-6), `vwap_reversion` (AC-7), and `us_open_breakout` (AC-8) behave exactly as the *Rules* and *Setup state* sections of their child spec, proven by the unit tests each child lists, on hand built bar fixtures.

*Studies and verdicts*
- **AC-9** (study files): each candidate has `apps/lab/studies/<study>.toml`: spec 0018's *Shared study settings* unchanged (range 2022-04-01 to 2025-10-18, fees, `slippage_bps` 2, `[risk]`, `lab = "candle"`, seed 42), plus the candidate's `[strategy.config]` and `[strategy.grid]` from its child spec. Every grid has 4 to 6 points. The study file is in a commit before `lab study` first runs it (verify: the commit time is earlier than `studies.created_at`). A candidate is never run on real candles outside its study. Task 1's cost check reads candle data only and never builds or runs a strategy, so it is not a run of any candidate.
- **AC-10** (run protocol): candidates run in the order of *Structure*, one at a time: `lab study`, `lab holdout`, `scripts/push-bundles.sh`, then `gate evaluate <study>` on the VPS (each VPS step confirmed with the trader first). The next candidate's `lab study` starts only after the previous candidate's results commit. **Checkpoint after candidate 2:** before any slice 3 code is written, the trader sees both verdicts with their gross and cost per trade (AC-11) and chooses to continue with candidates 3 and 4 or stop. The recommendation is to continue when either candidate's holdout gross per trade is at least 1.5 × its cost per trade (the timescale clears the fee wall, the signal is what is missing), and to stop and go back to `/architect` when both are below their cost (15 minutes alone does not help). The choice and its reason go into `verify.md`. A failed or interrupted study is resumed, never renamed to start over. Studies of this spec and spec 0018 may run at different times in any interleaving; each `gate evaluate` simply sees the family as it stands then.
- **AC-11** (results record): after `gate evaluate`, the candidate's child spec gets its *Results* filled in, in its own commit (`docs(specs): <study> verdict (0019)`), with the same fields as spec 0018 AC-17 (study name, promotion id, evaluation date, verdict, every reason with code, value, and threshold, walk forward Sharpe, holdout Sharpe against the buy and hold and MACD baseline Sharpes, holdout return after costs, holdout marked max drawdown, deflated Sharpe probability, PBO, holdout trades, trial count N, trial pool), plus `avg_hold_minutes` (mean minutes from entry fill to exit fill over the holdout's closed trades, from the holdout run's `positions` rows) and the holdout's gross and cost per trade (gross = (net P&L + `fees_total` − `funding_total`) ÷ `trade_count`, cost = (`fees_total` − `funding_total`) ÷ `trade_count`; `funding_total` is positive when funding was earned, spec 0008), so the fee question this feature exists for is answered directly. Fails and insufficient verdicts are recorded the same way and never removed.
- **AC-12** (holdout seen): `connors_rsi_15m`'s results carry the line "holdout seen: the same idea's 1 hour study (spec 0018.1) already judged this holdout year", and a `pass` from it adds a paper trading follow up only with that label. No other candidate carries the label.
- **AC-13** (one revision): after a `fail` or `insufficient` verdict, a candidate may get at most one revision, under spec 0018 AC-18's rules (written into its child spec as *Revision 1* before it runs, development and walk forward evidence only, new name and study `<name>_r1` with its own node module, labelled "holdout seen", final). A switch to `30-MINUTE-LAST` bars is a valid revision. `connors_rsi_15m` gets no revision (it is already a variation of a judged idea).
- **AC-14** (close out): when every candidate run (all four, or the first two if the AC-10 checkpoint stopped), plus any revision started, has a verdict, `gate evaluate` runs once more for each of their studies, so each is also judged with the family N as it then stands (new promotion rows; the first rows stay). Each child's results add a "final family N" line with that verdict. A `pass` doesn't start paper trading here; it adds the follow up in *Follow-up*.

## Decision

**Chosen option**: Option 2: new 15 minute candidates as small pure rules on spec 0018's frozen base, in the hour level family, run through the existing candle study and gate.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

```
strategy/intraday.py (DayVwap, ny_open_utc_ns)      strategy/bar_indicators.py, strategy/indicators.py (unchanged)
            └──────────────┬───────────────────────────────┘
strategy/<name>.py  (pure BarRule)  ─►  node/<name>.py  ─►  node/bar_rule.py BarRuleStrategy (unchanged)
            ▼
lab registry (hour_level) ─► lab study ─► lab holdout ─► push-bundles ─► gate evaluate (family N) ─► child spec Results
```

### Bars and time

- Every bar is a 15 minute `LAST` bar built from trade ticks (spec 0014 AC-11), so one study replays the same 1 minute candle stream as spec 0018 and only the bar aggregation differs. `bar_interval_ns` = 900 × 10⁹.
- A bar's close time is `ts_ns`; its open time is `ts_ns − bar_interval_ns`. Every date or clock rule in this spec uses the bar's **open** time, so the bar from 23:45 to 00:00 belongs to the old UTC day and the bar from 00:00 to 00:15 to the new one.
- A zero volume bar (spec 0014 AC-11) is a normal bar for every rule; only `DayVwap` treats it specially (it adds nothing).

### Intraday helpers (`trading_core/strategy/intraday.py`)

- `DayVwap()`: `update(bar_open_ns, high, low, close, volume) -> Decimal | None`. The day key is the UTC date of `bar_open_ns`. On a new day key it resets `cum_pv` and `cum_v` to 0 and `bars_in_day` to 0. Each bar adds `typical × volume` to `cum_pv` (`typical = (high + low + close) ÷ 3`) and `volume` to `cum_v`, and adds 1 to `bars_in_day`. It returns `cum_pv ÷ cum_v`, or `None` while `cum_v` is 0. It exposes `bars_in_day` and `day` (the current key). All `Decimal`. The volume is `BarInput.volume` of the 15 minute bar the base receives (in the candle lab, the sum of that bar's snapped 1 minute volumes, spec 0014 AC-9; live, real trade volume).
- `ny_open_utc_ns(day)`: the UTC nanosecond time of 09:30 New York time on `day`, where `day` is a calendar date (callers pass the UTC date of the bar's open time; at 13:30 and 14:30 UTC the UTC date and the New York date are the same). It is 13:30 UTC when US daylight saving time is in force on `day`, else 14:30 UTC. Daylight saving is in force from the second Sunday of March through the Saturday before the first Sunday of November (the US rule since 2007; at 09:30 the 02:00 switch has already happened on both switch days). The second Sunday of March is the first Sunday on or after March 8; the first Sunday of November is the first Sunday on or after November 1. Pure, no time zone database.
- `is_us_weekday(day)`: `day.weekday() < 5`. US market holidays are not excluded (about nine a year); the rule trades them as normal days.

### Family, settings, and run protocol

- Kind `hour_level`, lab `candle`, so the spec 0018 AC-19 family N and trial variance include every study of both specs, failed runs included.
- The study settings are spec 0018's *Shared study settings* with nothing changed. The candidate sets `bar_spec`, `atr_period` 14, `atr_floor_mult` `"1"`, and its own fields.
- Folds, holdout range, gate thresholds (including the 60 trade floor and the fee hurdle), benchmarks: all unchanged from spec 0015 and spec 0018.

### Cost arithmetic (why 15 minutes)

A round trip pays the taker fee twice (0.05% each side) plus 2 bps of slippage each side: about 14 bps of notional. Each new candidate's target or exit sits at least about 1.5 ATR(14) of 15 minute bars from the entry (child specs *Rules*). `connors_rsi` is the exception: it exits at the first close above SMA(5), which is often under 1 ATR away, and that is its likely fail (child 1).

Task 1 is a one off script, `scripts/spikes/cost_check_15m.py`, run in the lab image. It reads the development range's stored 1 minute candles through the candle lab's own candle reader, aggregates them into UTC aligned 15 minute bars (a 15 minute window with no candle is skipped, as the candle lab skips it), and records in `verify.md`: the median ATR(14) in bps (ATR ÷ close × 10 000), each new candidate's implied minimum target distance in bps, and for `connors_rsi` the median distance in bps between a close and its SMA(5). These are properties of the data, not of any strategy (no strategy is built or run), so it is not a trial. It gates nothing; it tells you early if a candidate's target is near the cost.

### Data model sketch

No migration. New rows only: one `strategies` row per new candidate (kind `hour_level`), one study per candidate (`studies.name` is unique; `connors_rsi_15m` is a second study of the existing `connors_rsi` strategy), and the usual study run, run, and promotion rows.

### State transitions

The base's phases are spec 0018's *Base lifecycle*, unchanged. Each candidate's own setup state is inside its pure rule and listed in its child spec's *Setup state*. The spec 0018 shared rule holds: while a position is open, setups keep updating, but a trigger that would fire is dropped (consumed, not saved).

### API surface (no new commands)

| Command or route | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `lab study apps/lab/studies/<study>.toml` | CLI | study file | development runs, bundles | `lab_app` | 64 bad file, grid, or config, 2 missing candles or funding, 1 a grid point failed |
| `lab holdout <study>` | CLI | study name | holdout plus benchmark runs, bundles | `lab_app` | 64 not ready or spent, 1 a run failed |
| `lab gate <study> --dry-run` | CLI | study name | verdict preview (laptop family only) | `lab_app` SELECT | 64 unknown study |
| `scripts/push-bundles.sh <dir>...` | shell | batch dirs | import summary on the VPS | Tailscale SSH as `kfs` | nonzero on rsync or import failure |
| `gate evaluate <study>` | CLI (VPS) | study name | promotion id, verdict, reasons, metrics | `gate_app` | 64 unknown study |

### Value sourcing

| Action | Value | Source |
|---|---|---|
| every bar | OHLCV, close time | the 15 minute internal bar from trade ticks (spec 0014 AC-11) |
| every bar | bar open time, UTC day | `ts_ns − bar_interval_ns`, its UTC date |
| every bar | ATR | the base's `Atr(14)`, passed in `update` |
| `ema_pullback` | EMAs, RSI | `indicators.Ema(8)`, `indicators.Ema(21)`, `bar_indicators.Rsi(7)` on closes |
| `vwap_reversion` | day VWAP, bars in day | `DayVwap` fed every bar |
| `us_open_breakout` | 09:30 New York in UTC | `ny_open_utc_ns(UTC date of the bar's open time)` |
| `us_open_breakout` | trading day or not | `is_us_weekday` of that date |
| entry | mid, stop used, quantity, funding periods | the base (spec 0018 *Value sourcing*), unchanged |
| exit | `entry_px`, `stop_px`, `bars_held`, R target | the base's `TradeView` and `r_target` |
| task 1 | median 15 minute ATR (bps) | `Atr(14)` over 15 minute bars aggregated from the development range's stored 1 minute candles (candle lab reader), ÷ close × 10 000, median |
| task 1 | `connors_rsi` exit distance (bps) | median of \|close − `Sma(5)`\| ÷ close × 10 000 over the same bars |
| results | verdict, reasons, metrics | the promotion row on the VPS (`gate evaluate` output) |
| results | `avg_hold_minutes` | holdout run `positions` (laptop Postgres, `run_id` of the holdout run): mean of (`closed_at − opened_at`) over rows with `closed_at` set |
| results | gross and cost per trade | holdout run `run_results`: `fees_total`, `funding_total`, `trade_count`, and net P&L (`return_after_costs × starting_balance_usd`) |

### Key invariants

- No float for money: prices, stops, targets, VWAP, and indicators are `Decimal` end to end.
- One position at a time, one action per bar close, no decision before warmup.
- Every open position has a resting protective stop at least 1 ATR from the decision mid, or is being closed.
- Every signal is used at most once.
- No existing strategy module, and nothing in any strategy's code closure, changes, so no existing `code_hash` or `frozen_hash` moves, and no deploy is needed for this feature. The only existing files edited are the look ahead test and `lab/lookahead.py` (AC-2), which are in no closure.
- A candidate's rules, grid, and modules never change after its first `lab study` run. Any change is a revision (AC-13).
- Real candle data is seen by a candidate only through its study.

### Security model

Nothing new. The lab runs on the laptop as `lab_app` in demo, the gate on the VPS as `gate_app`. No Kraken key, no regulated data. The repo is public, so every hypothesis and verdict is world readable.

### Configuration required

None. No new settings or secrets; three registry entries and four study files.

### Critical test scenarios

- Happy path: `connors_rsi_15m` runs `lab study`, `lab holdout`, push, and `gate evaluate` end to end on 15 minute bars, and its verdict lands in its child spec with the "holdout seen" line, verifies **AC-1**, **AC-5**, **AC-9**, **AC-10**, **AC-11**, **AC-12**
- Hash stability: every `code_hash` from `gate versions` is the same before and after the feature, verifies **AC-1**
- VWAP reset: bars from 23:30 to 00:30 UTC give the old day's VWAP for the 23:45 bar and a fresh VWAP from the 00:00 bar's typical price alone, verifies **AC-3**
- VWAP zero volume: a day whose first two bars have volume 0 returns `None` until the first traded bar, and counts all three in `bars_in_day`, verifies **AC-3**
- New York clock: `ny_open_utc_ns` gives 14:30 UTC on 2024-03-08, 13:30 on 2024-03-11, 13:30 on 2024-11-01, and 14:30 on 2024-11-04 (and the same pattern for 2022, with switches on 2022-03-13 and 2022-11-06, for 2023, and for 2025), verifies **AC-3**
- Cut times: with a 15 minute interval no drawn cut is a multiple of 15 minutes; with a 1 hour interval the draws equal today's, verifies **AC-2**
- Config: `bar_spec = "1-HOUR-LAST"` in an `ema_pullback` study file exits 64 before any run, verifies **AC-4**
- Look ahead: every new candidate passes the spec 0014 prefix test, verifies **AC-2**
- Family: a new candidate's study adds its distinct hashes to the same N as the spec 0018 studies, verifies **AC-2**
- Each child spec's *Tests*, verifies **AC-5** to **AC-8**

## Build plan

Tracer Bullet: `connors_rsi_15m` goes end to end first with no code (a study file only), which proves 15 minute bars, the study, the holdout, the push, the gate, and the results record. Each later slice adds one pure rule and runs it to a verdict before the next.

**Slice 1: thin thread (`connors_rsi_15m` to a recorded verdict)**
1. Task 1 cost check (`scripts/spikes/cost_check_15m.py`): the median 15 minute ATR in bps, each candidate's implied minimum target, and the `connors_rsi` SMA(5) distance, recorded in `verify.md`. Satisfies **AC-9** (informs it, gates nothing)
2. Look ahead tooling: the bar interval aware `cut_times` and the 15 minute `connors_rsi` look ahead case. Then `apps/lab/studies/connors_rsi_15m.toml` from child 1, committed; record every `code_hash` from `gate versions`; run study, holdout, push, evaluate; fill in *Results* with the "holdout seen" line. Satisfies **AC-1**, **AC-2**, **AC-5**, **AC-9**, **AC-10**, **AC-11**, **AC-12**

**Slice 2: trend following**
3. `ema_pullback` rule, node, registry, child tests, look ahead test entry, study file; run to a verdict. Then the AC-10 checkpoint with the trader. Satisfies **AC-2**, **AC-4**, **AC-6**, **AC-9** to **AC-11**

**Slice 3: session ideas (only if the checkpoint says continue)**
4. `strategy/intraday.py` (`DayVwap`, `ny_open_utc_ns`, `is_us_weekday`) with its tests. Satisfies **AC-3**
5. `vwap_reversion` rule, node, registry, tests, study file; run to a verdict. Satisfies **AC-2**, **AC-4**, **AC-7**, **AC-9** to **AC-11**
6. `us_open_breakout` rule, node, registry, tests, study file; run to a verdict. Satisfies **AC-2**, **AC-4**, **AC-8**, **AC-9** to **AC-11**

**Slice 4: revisions and close out**
7. For each `fail` or `insufficient` new candidate you choose to revise: *Revision 1*, commit, `<name>_r1`, run to a verdict. Satisfies **AC-13**
8. Close out: `gate evaluate` again for every study, "final family N" lines, check `gate versions` hashes again, tick the scope row, add the paper follow up if any passed. Satisfies **AC-1**, **AC-14**

## Consequences

**Positive**:
- Tests the one lever the scalping results point at: a move per trade several times the cost, with no new infrastructure.
- Small code: at most three pure rules, one helper module, three thin node modules, four study files, and a small look ahead tooling change. The checkpoint after candidate 2 can skip slice 3 entirely. No base change, no migration, no deploy.
- Many trades over four and a half years of 15 minute bars, so most candidates should clear the 60 trade holdout floor and get a real verdict instead of `insufficient`.

**Negative / tradeoffs**:
- Expect mostly `fail` verdicts. The gate is strict on purpose, and the holdout year has to beat buy and hold.
- Joining the hour level family raises N for every hour level study, spec 0018's included, so each added trial makes every later pass harder.
- `connors_rsi_15m` uses a holdout year its own idea already saw at 1 hour. Even a pass is weak evidence.
- All candidates share the 2025-10-19 holdout year with spec 0018's six, which is more selection on one year than N fully captures.
- Market entries pay the taker fee both ways (about 14 bps round trip with slippage). Maker entries would roughly halve that, but the candle lab cannot model queue position, so that saving is left untested.
- Inside one minute the candle lab orders the high and low by position side (spec 0014 AC-9). With 15 minute stops of about 1 to 3 ATR, a stop and target both inside one minute are rarer than at 1 minute, but not impossible.
- Candle lab bar volume can exceed real volume by step size rounding (spec 0014 AC-9), which nudges `DayVwap` toward low volume minutes. The effect is small, and live bars use real volume.
- `us_open_breakout` trades US holidays as normal days and is limited to one trade a day, so it has the fewest trades of the four (likely 150 to 220 a year, still well above the 60 trade floor).
- The US daylight saving rule is hard coded. If the US changes it, the helper needs a new module.

**Neutral**:
- No migration, no new setting.
- The scope header still describes a seconds scale scalper; a `/scope` pass should reword it once spec 0017's verdict is in.

## Follow-up

- [x] `/scope`: enrolled as feature 22 in Slice 7 (between features 16 and 17), linking spec 0019.
- [ ] `/scope`: reword the product header (it still says seconds timescale scalping) once spec 0017 closes.
- [ ] On the first `pass` (without the "holdout seen" label): `/scope` the feature that runs a chosen registered strategy instead of `PlaceholderTimed` for paper evidence (same follow up as spec 0018).
- [ ] Round 2 (`/architect intraday swing candidates, round 2`): Bollinger RSI and NR4 at 15 minutes once spec 0018 builds those modules, a maker entry base (`bar_rule_v2`) with an honest fill model, busy hours and regime filters, and ETH.
- [ ] `/sync`: `packages/core/AGENTS.md` should list `strategy/intraday.py` (only if candidates 3 and 4 are ever built; the 2026-10-26 checkpoint skipped them).
- [ ] `/architect`: the AC-10 checkpoint stopped this feature on 2026-10-26 (both candidates below cost per trade). Choose the next approach before any round 2.
