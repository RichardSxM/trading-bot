# 0020. Intraday trend candidates: a noise area breakout on 30 minute bars, a time of day variant, and a 4 hour Donchian trend, on spec 0018's frozen base

**Date**: 2026-10-26
**Status**: In Progress

## Summary

Every candidate so far (seconds scalping, 1 hour and 15 minute rules) earned less per trade than the 14 bps a round trip costs. This feature tests trend following instead, the idea with the best recent published evidence: trade only when BTC leaves its usual daily range (the "noise area") and ride the move, plus a slower 4 hour channel breakout that holds for days. All three reuse spec 0018's shared strategy base unchanged, run in the candle lab over the same history, and go through the same promotion gate in the same family, so the overfitting check stays strict. Expect fails; the gate decides, and every verdict is kept.

## Structure

The shared contract (bars, family, run protocol, helpers) lives in this file. Each candidate is a child spec with its hypothesis, exact rules, settings, grid, tests, and a results section. Run order is the file order, with a checkpoint after candidate 1 (AC-9).

| # | File | Strategy (study) | What it is |
|---|---|---|---|
| 1 | [0001-noise-breakout.md](0001-noise-breakout.md) | `noise_breakout` | 30 minute bars. Bands around the UTC day's open, as wide as the average move from the open at that time of day over the last 14 days. Enter when a close is beyond the band and the day's VWAP, exit when a close comes back inside that line, re-entry allowed, 8 hour time stop. |
| 2 | [0002-noise-breakout-tod.md](0002-noise-breakout-tod.md) | `noise_breakout_tod` | Candidate 1 plus one time of day filter: no new entries on Sunday 12:00 to 22:00 UTC. |
| 3 | [0003-donchian-4h.md](0003-donchian-4h.md) | `donchian_4h` | 4 hour bars. Enter on a close beyond the 20 or 55 bar channel, exit on a close beyond the opposite half length channel, 2 or 3 ATR stop, 10 day time stop. |

Left out: daily bars (too few trades for the gate's 60 trade floor), funding and open interest signals (Kraken keeps funding history only from 2025-10-15, so there is no development data), maker entries (the candle lab cannot model fill quality), volatility target sizing (a new risk path), ETH, and a multi lookback Donchian ensemble (the base holds one fixed size position).

## Requirements

**User stories**:
- As the trader, I want trend following tested with every cost, because it aims for moves many times the round trip cost, where the earlier rules aimed for moves about the size of it.
- As the trader, I want each hypothesis committed before its result exists, and every verdict kept in git, fails included.
- As the trader, I want these trials counted with all the earlier hour level ones, so trying more ideas on the same data makes passing harder, not easier.

**Acceptance criteria**:

*Shared contract*
- **AC-1** (same base): every candidate runs on spec 0018's `BarRuleStrategy` and `BarRule` contract (spec 0018 AC-1 to AC-5). `strategy/bar_rule.py`, `strategy/bar_indicators.py`, `strategy/indicators.py`, `node/bar_rule.py`, and every existing candidate module stay byte identical (checked by comparing `code_hash(code_closure(m, root))` for every registered module on main before and after this feature, as spec 0019's close out did, since `gate versions` does not print `code_hash`). A candidate whose idea needs a base change is out of scope here and goes back to `/architect`.
- **AC-2** (registry, kind, look ahead): each candidate has a pure rule `trading_core/strategy/<name>.py` and a thin node module `trading_core/node/<name>.py` (a config class with its `bar_spec` default, plus a `BarRuleStrategy` subclass that builds the rule), registered in `apps/lab/src/lab/strategies.py` with kind `hour_level`, so it passes the spec 0014 AC-19 look ahead prefix test and counts in the spec 0018 AC-19 family (kind `hour_level`, lab `candle`). In `apps/lab/tests/test_lookahead.py` the expected `HOUR_LEVEL` list gains each name as it is registered, and `CONFIGS` gains a loose config per candidate that makes it decide on the synthetic fixture (for example `noise_breakout` with `lookback_days` 1 and `band_mult` `"0.1"`, `donchian_4h` with `entry_bars` 2). Each entry also sets `"bar_spec"` (`"30-MINUTE-LAST"` or `"4-HOUR-LAST"`) and `notional_usd` (`NOTIONAL`), as the existing entries do, because the test reads the cut interval from `strategy_config["bar_spec"]` and would otherwise fall back to 1 hour. `time_stop_bars` stays at its default. `test_the_registry_has_the_candle_strategies` gains the new names in sorted order. The same task adds a test on the synthetic fixture that pins how Nautilus builds the bars: every 4 hour bar opens at a multiple of 4 hours from UTC midnight, every 30 minute bar at :00 or :30, and a window with no trades gives either no bar or a zero volume bar (the test records which). If it is a zero volume bar, the rule tests in AC-3, AC-5, and AC-7 add a case for it. If one still makes no decision, the fixture is extended until it does; the `assert full` check is never relaxed. `lab/lookahead.py`'s bar interval aware `cut_times` (spec 0019 AC-2) already handles 30 minute and 4 hour intervals; if a test shows otherwise, the fix stays inside `lookahead.py`, which is in no closure.
- **AC-3** (noise helpers): the new pure module `trading_core/strategy/noise_area.py` provides `DayVwap` and `NoiseBands` exactly as *Noise helpers* below, with unit tests on hand worked values: a VWAP and band reset across a UTC midnight, a zero volume bar, a day whose 00:00 bar is missing (no open, no bands, no history sample that day), a slot missing on one earlier day (its history skips that day), and the band not ready until `lookback_days` samples exist for that slot. It is imported only by `noise_breakout` (and through it `noise_breakout_tod`), and it is frozen after the first study that has it in its closure (spec 0018 AC-7 rule: a later change goes into a new module).
- **AC-4** (config validation): each candidate config refuses bad values in its constructor with `ValueError` (its child spec *Settings* checks, plus the base's), so `lab study` exits 64 before any run. `noise_breakout` and `noise_breakout_tod` refuse any `bar_spec` other than `30-MINUTE-LAST`, and `donchian_4h` any other than `4-HOUR-LAST`, because their slot math and bar counts are written for those intervals.
- **AC-5** to **AC-7** (candidate rules): `noise_breakout` (AC-5), `noise_breakout_tod` (AC-6), and `donchian_4h` (AC-7) behave exactly as the *Rules* and *Setup state* sections of their child spec, proven by the unit tests each child lists, on hand built bar fixtures.

*Studies and verdicts*
- **AC-8** (study files): each candidate has `apps/lab/studies/<name>.toml`: spec 0018's *Shared study settings* unchanged (range 2022-04-01 to 2025-10-18, fees, `slippage_bps` 2, `[risk]`, `lab = "candle"`), `[study] name = "<name>"`, `seed = 42`, plus the candidate's `[strategy.config]` and `[strategy.grid]` from its child spec (3 points for the two noise candidates, as the trader chose, 4 for `donchian_4h`). The study file is in a commit before `lab study` first runs it (verify: the commit time is earlier than `studies.created_at`). A candidate is never run on real candles outside its study. Task 1's cost check reads candle data only and never builds or runs a strategy, so it is not a trial.
- **AC-9** (run protocol and checkpoint): candidates run in the order of *Structure*, one at a time: `lab study`, `lab holdout`, `scripts/push-bundles.sh` (every batch folder the holdout wrote, plus the study batch), then `gate evaluate <name>` on the VPS, each VPS step confirmed with the trader first. The next candidate's `lab study` starts only after the previous candidate's results commit. **Checkpoint after candidate 1:** before any code for candidate 2 is written, the trader sees `noise_breakout`'s verdict with its gross and cost per trade (AC-10) and chooses what runs next. The recommendation: when its holdout gross per trade is at least 1.5 × its cost per trade, run candidates 2 and 3 (the signal clears the cost, the filter may lift it); when it is below, skip candidate 2 (a filter only removes trades, it cannot lift a below cost signal past the hurdle) and still run candidate 3 (a different timescale, not a variant of candidate 1). The choice and its reason go into `verify.md`. A failed or interrupted study is resumed, never renamed to start over.
- **AC-10** (results record): after `gate evaluate`, the candidate's child spec gets its *Results* filled in, in its own commit (`docs(specs): <study> verdict (0020)`), with spec 0019 AC-11's fields: study name, promotion id, evaluation date, verdict, every reason with code, value, and threshold, walk forward Sharpe, holdout Sharpe against the buy and hold and MACD baseline Sharpes, holdout return after costs, holdout marked max drawdown, deflated Sharpe probability, PBO, holdout trades, trial count N, trial pool, `avg_hold_minutes`, and the holdout's gross and cost per trade (formulas as spec 0019 AC-11). It also records, from the holdout run's `decisions`: the share of exits by reason (`noise_trail` or `channel_exit`, `time_stop`, stop fill), the share of rule exits within 2 bars of entry, the count of `BAD_SIGNAL` refusals, and the warmup bars skipped at the start of the run. Fails and insufficient verdicts are recorded the same way and never removed.
- **AC-11** (source caveat): `noise_breakout_tod`'s results carry the line "filter from published results (Concretum, 2018 to 2025) that may overlap part of the holdout year", and a `pass` from it adds a paper trading follow up only with that label.
- **AC-12** (one revision): after a `fail` or `insufficient` verdict, a candidate may get at most one revision, under spec 0018 AC-18's rules (written into its child spec as *Revision 1* before it runs, development and walk forward evidence only, new name and study `<name>_r1` with its own node module, labelled "holdout seen", final). `noise_breakout_tod` gets no revision (it is already a variation of candidate 1).
- **AC-13** (close out): when every candidate run (all three, or those the checkpoint allowed), plus any revision started, has a verdict, `gate evaluate` runs once more for each of their studies, so each is also judged with the family N as it then stands (new promotion rows; the first rows stay). Each child's results add a "final family N" line with that verdict. The AC-1 hash comparison runs again. A `pass` does not start paper trading here; it adds the follow up in *Follow-up*.

## Decision

**Chosen option**: Option 2: two trend following ideas (an intraday noise area breakout and a 4 hour Donchian channel) as small pure rules on spec 0018's frozen base, in the hour level family, through the existing candle study and gate.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning, options, and the research behind this choice: see [rationale.md](rationale.md).

## Feature design

```
strategy/noise_area.py (DayVwap, NoiseBands)        strategy/bar_rule.py, bar_indicators.py (unchanged)
            └──────────────┬──────────────────────────────┘
strategy/noise_breakout.py ─► strategy/noise_breakout_tod.py      strategy/donchian.py
            ▼                                                        ▼
node/<name>.py ─► node/bar_rule.py BarRuleStrategy (unchanged: sizing, ATR floor, stop, time stop)
            ▼
lab registry (hour_level) ─► lab study ─► lab holdout ─► push-bundles ─► gate evaluate (family N) ─► child spec Results
```

### Bars and time

- Bars are `LAST` bars built from trade ticks (spec 0014 AC-11): `30-MINUTE-LAST` (`bar_interval_ns` 1800 × 10⁹) for the noise candidates, `4-HOUR-LAST` (14 400 × 10⁹) for `donchian_4h`. Nautilus time bars are expected to align to UTC, so 30 minute bars open at :00 and :30 and 4 hour bars at 00:00, 04:00, and so on. The AC-2 bar test pins this, because Nautilus builds the bars (`INTERNAL` aggregation), not the lab.
- A bar's close time is `ts_ns`; its open time is `ts_ns − bar_interval_ns`. Every date, slot, or clock rule uses the bar's **open** time, so the bar from 23:30 to 00:00 belongs to the old UTC day (slot 47) and the bar from 00:00 to 00:30 to the new one (slot 0).
- The rule gets `bar_interval_ns` from its config (`config.bar_interval_ns`, passed to the rule by the node, as `connors_rsi`'s node passes its settings).
- A window with no candle is expected to produce no bar, so a slot can be missing on a day. The AC-2 bar test confirms this or shows a zero volume bar instead.
- Rules count processed bars, not time (as the base's `bars_held` does), so a missing bar never shifts a rule's bookkeeping.

### Noise helpers (`trading_core/strategy/noise_area.py`)

- `DayVwap()`: `update(bar_open_ns, high, low, close, volume) -> Decimal | None`, exactly spec 0019's *Intraday helpers* definition: day key = UTC date of `bar_open_ns`; on a new key reset `cum_pv` and `cum_v` to 0; each bar adds `typical × volume` (`typical = (high + low + close) ÷ 3`) to `cum_pv` and `volume` to `cum_v`; return `cum_pv ÷ cum_v`, or `None` while `cum_v` is 0. Exposes `day`. All `Decimal`, default context, no rounding.
- `NoiseBands(lookback_days: int, interval_ns: int)`: `update(bar_open_ns, open, close) -> Decimal | None` returns the slot's noise `sigma` for this bar, or `None`.
  - Day key = UTC date of `bar_open_ns`; slot = `(bar_open_ns − UTC midnight of that date) ÷ interval_ns` (0 to 47 for 30 minutes).
  - On the first bar of a new day key: `day_open` = that bar's `open` if its slot is 0, else `None` for the whole day (fail closed: no bands that day).
  - `sigma` for this bar = the mean of the slot's history when it holds exactly `lookback_days` values, else `None`. It is read **before** this bar's own move is added, so it uses earlier days only.
  - Then, when `day_open` is not `None`, the bar's move `|close ÷ day_open − 1|` is appended to the slot's history (a deque with `maxlen = lookback_days`). A day missing that slot, or with no `day_open`, adds no value, so the history is the slot's most recent `lookback_days` days that had it.
  - Exposes `day`, `slot`, `day_open`. All `Decimal`.

### Family, settings, and run protocol

- Kind `hour_level`, lab `candle`, so the spec 0018 AC-19 family N and trial variance include every study of specs 0018, 0019, and this one, failed runs included. The family stood at N 16 at spec 0019's close out.
- Study settings are spec 0018's *Shared study settings* with nothing changed. Each candidate sets `bar_spec`, `atr_period` 14, `atr_floor_mult` `"1"`, `time_stop_bars`, and its own fields.
- Folds, holdout range (2025-10-19 to the last complete UTC day), gate thresholds (the 60 trade candle floor, the fee hurdle at 1.5 × cost), and benchmarks are unchanged from specs 0015 and 0018.
- Warmup is paid in every run. Each walk forward fold, each grid point, and the holdout start cold (spec 0018 AC-1, warmup inside the range). So the noise candidates make no decision for about the first 15 days of each run, and `donchian_4h` for its first `entry_bars` bars (about 4 or 9 days). This is fine against the holdout's roughly 365 days, but it cuts each fold's trades, and the results record it (AC-10).
- Sizing is the base's, unchanged: risk per trade from the stop actually used (spec 0011 caps apply). A wide band or channel means a far stop and a small position, which works like volatility sizing without a new risk path.

### Cost arithmetic and task 1

A round trip pays the taker fee twice (0.05% each side) plus 2 bps slippage each side: about 14 bps of notional. Trend rules have no fixed target: they win by holding the few large moves, so the check that matters is the holdout gross per trade against cost (AC-10), not a target distance.

Task 1 is a one off script, `scripts/spikes/cost_check_trend.py`, run in the lab image. It reads the development range's stored 1 minute candles through the candle lab's candle reader, aggregates them into UTC aligned 30 minute and 4 hour bars (a window with no candle is skipped), and records in `verify.md`: the median 30 minute ATR(14) in bps, the median noise band half width in bps (`band_mult × sigma × 10 000`, over all slots with a ready `sigma`, for `band_mult` 1, 1.5, and 2), the share of 30 minute bars whose close is beyond `day_open × (1 ± band_mult × sigma)` for each `band_mult` (a rough guide to how often candidate 1 could signal), and the median 4 hour ATR(14) in bps (and 2 × and 3 × that, the Donchian stop distances). These are properties of the data, not of any strategy, so it is not a trial. It gates nothing; it shows early whether the band width is near the cost.

### Data model sketch

No migration. New rows only: one `strategies` row per candidate (kind `hour_level`), one study per candidate (`studies.name` unique), and the usual study run, run, decision, position, and promotion rows.

### State transitions

The base's phases are spec 0018's *Base lifecycle*, unchanged (flat, entering, protected, exiting). Each candidate's own setup state is inside its pure rule and listed in its child spec's *Setup state*. The spec 0018 shared rule holds: while a position is open, a trigger that would fire is dropped (consumed, not saved). On the bar where the base exits, it does not enter (its `on_bar` runs the entry step only when no exit fired), so the earliest re-entry is the next bar close.

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
| every bar | OHLCV, close time | the internal bar from trade ticks (spec 0014 AC-11) |
| every bar | bar open time, UTC day, slot | `ts_ns − bar_interval_ns`; its UTC date; `(open time − midnight) ÷ bar_interval_ns` |
| every bar | ATR | the base's `Atr(14)`, passed in `update` |
| noise | day open | the `open` of the day's slot 0 bar (`NoiseBands`), else none that day |
| noise | `sigma` per slot | mean of the slot's last `lookback_days` moves from earlier days (`NoiseBands`) |
| noise | upper and lower band | `day_open × (1 ± band_mult × sigma)` |
| noise | day VWAP | `DayVwap` fed every bar |
| noise | the entry day of an open trade | `days[-1 - bars_held]`: the rule's deque of recent bar open dates, indexed by `TradeView.bars_held` (child 1 *Rules*) |
| noise | frozen lines after midnight | `day_lines[entry_day]`: the entry day's last bar's `long_line` and `short_line`, kept by the rule, `None` included |
| `noise_breakout_tod` | rule and config | subclasses of `NoiseBreakoutRule` and `NoiseBreakoutConfig` (child 2 *Rules*) |
| `noise_breakout_tod` | blocked window | the bar's open time: UTC weekday Sunday and 12:00 ≤ time < 22:00 |
| donchian | channels | highest high and lowest low of the previous `entry_bars` (entry) and `entry_bars ÷ 2` rounded down (exit) bars, the current bar excluded |
| entry | mid, stop used, quantity, funding periods | the base (spec 0018 *Value sourcing*), unchanged |
| task 1 | median ATRs and band widths (bps) | the candle lab reader over the development range, aggregated to 30 minute and 4 hour bars |
| results | verdict, reasons, metrics | the promotion row on the VPS (`gate evaluate` output) |
| results | `avg_hold_minutes`, exit reason shares | holdout run `positions` and `decisions` (laptop Postgres, the holdout `run_id`) |
| results | gross and cost per trade | holdout run `run_results`: `fees_total`, `funding_total`, `trade_count`, net P&L |

### Key invariants

- No float for money: prices, stops, bands, VWAP, channels, and indicators are `Decimal` end to end, in the default context, with no rounding in the rules.
- One position at a time, one action per bar close, no decision before warmup.
- Every open position has a resting protective stop at least 1 ATR from the decision mid, or is being closed.
- A rule's `ready()` never turns false again once true, because the base skips its exit and time stop checks while `ready()` is false (`node/bar_rule.py` `on_bar`).
- No existing strategy module, and nothing in any existing strategy's code closure, changes, so no existing `code_hash` or `frozen_hash` moves, and no deploy is needed. The only existing files edited are the lab registry and the look ahead test (and `lab/lookahead.py` only if AC-2 needs it), none of which is in a closure.
- A candidate's rules, grid, and modules never change after its first `lab study` run. Any change is a revision (AC-12).
- Real candle data is seen by a candidate only through its study.

### Security model

Nothing new. The lab runs on the laptop as `lab_app` in demo, the gate on the VPS as `gate_app`. No Kraken key, no regulated data. The repo is public, so every hypothesis and verdict is world readable.

### Configuration required

None. No new settings or secrets; three registry entries and three study files.

### Critical test scenarios

- Happy path: `noise_breakout` runs `lab study`, `lab holdout`, push, and `gate evaluate` end to end on 30 minute bars, and its verdict with gross and cost per trade lands in its child spec, verifies **AC-1**, **AC-5**, **AC-8**, **AC-9**, **AC-10**
- Hash stability: every existing `code_hash` is the same on main before and after the feature, verifies **AC-1**, **AC-13**
- Bands across midnight: the 23:30 bar uses the old day's open and slot 47 history; the 00:00 bar starts a new day open, a new VWAP, and slot 0, verifies **AC-3**
- Missing midnight bar: a day whose first bar opens at 00:30 has no bands and adds no history, and the next day's bands still work, verifies **AC-3**
- No look ahead in the band: a bar's `sigma` never includes its own move (a fixture with one huge move at slot 5 today leaves today's slot 5 `sigma` unchanged and moves tomorrow's), verifies **AC-3**
- Config: `bar_spec = "15-MINUTE-LAST"` in a `noise_breakout` study file and `"1-HOUR-LAST"` in a `donchian_4h` one each exit 64 before any run, verifies **AC-4**
- Look ahead: every candidate passes the spec 0014 prefix test, verifies **AC-2**
- Each child spec's *Tests*, verifies **AC-5** to **AC-7**

## Build plan

Tracer Bullet: `noise_breakout` goes end to end first (helpers, rule, node, study, holdout, push, gate, results), which proves 30 minute bars and the noise math through every layer before anything else is built. Each later slice adds one rule and runs it to a verdict before the next.

**Slice 1: thin thread (`noise_breakout` to a recorded verdict)**
1. Task 1 cost check (`scripts/spikes/cost_check_trend.py`): median ATRs and band widths in bps, recorded in `verify.md`. Satisfies **AC-8** (informs it, gates nothing)
2. Record every existing `code_hash` on main (AC-1 baseline). `strategy/noise_area.py` (`DayVwap`, `NoiseBands`) with its tests. Satisfies **AC-1**, **AC-3**
3. `noise_breakout` rule, node, config checks, registry entry, child tests, look ahead test entry, `apps/lab/studies/noise_breakout.toml`, committed; run study, holdout, push, evaluate (each VPS step asked first); fill in *Results*. Then the AC-9 checkpoint with the trader. Satisfies **AC-2**, **AC-4**, **AC-5**, **AC-8**, **AC-9**, **AC-10**

**Slice 2: time of day variant (only if the checkpoint says run it)**
4. `noise_breakout_tod` rule (on top of the frozen `noise_breakout` module), node, registry, tests, study file; run to a verdict with the AC-11 line. Satisfies **AC-2**, **AC-4**, **AC-6**, **AC-8** to **AC-11**

**Slice 3: multi day trend**
5. `donchian_4h` rule, node, registry, tests, study file; run to a verdict. Satisfies **AC-2**, **AC-4**, **AC-7**, **AC-8** to **AC-10**

**Slice 4: revisions and close out**
6. For each `fail` or `insufficient` candidate you choose to revise (not `noise_breakout_tod`): *Revision 1*, commit, `<name>_r1`, run to a verdict. Satisfies **AC-12**
7. Close out: `gate evaluate` again for every study, "final family N" lines, the AC-1 hash comparison again, tick the scope row, add the paper follow up if any passed. Satisfies **AC-1**, **AC-13**

## Consequences

**Positive**:
- Tests the idea class with the most recent published evidence (intraday trend on BTC, multi day Donchian trend) and the one structural difference from every failed rule: no fixed target, so a winner can be many times the cost.
- Small code: two helper classes, three pure rules, three thin node modules, three study files. No base change, no migration, no deploy.
- `noise_breakout` should trade far above the 60 trade floor (a few hundred to a few thousand holdout trades), so it gets a real verdict instead of `insufficient`.

**Negative / tradeoffs**:
- Expect mostly `fail`. Published intraday results are gross of fees and not peer reviewed; ours are net of 14 bps per round trip.
- Unlimited re-entry (the trader's choice) means a choppy day can pay the round trip several times. `ema_pullback` lost with 706 holdout trades, so a high trade count is a warning sign here, not a comfort.
- Holding overnight (no day end, the trader's choice) departs from the published design, which closes at the session end, and pays funding on longer holds. The frozen lines after midnight are our own rule, not the paper's.
- `donchian_4h` may trade only 15 to 80 times in the holdout year, so its verdict may be `insufficient` under the 60 trade floor. The floor is not changed for it.
- Joining the hour level family raises N for every hour level study, so each added trial makes every later pass harder. Three studies here add 10 grid points (3 + 3 + 4).
- All candidates share the 2025-10-19 holdout year with specs 0018 and 0019, which is more selection on one year than N fully captures.
- `noise_breakout_tod`'s filter comes from a published study whose sample may reach into our holdout year (AC-11).
- A day whose 00:00 candle is missing has no bands, so no entries that day. Rare for BTC, and it fails closed.
- Inside one minute the candle lab orders the high and low by position side (spec 0014 AC-9). Trend stops are wide, so this matters less than for the earlier rules.

**Neutral**:
- No migration, no new setting.
- Spec 0019 stays `In Progress` as it was; this spec does not reopen its candidates 3 and 4.

## Follow-up

- [x] `/scope`: enroll feature 23 "Intraday trend candidates" in Slice 7, linking spec 0020 (done by /architect, 2026-10-26).
- [ ] On the first `pass` (without a label): `/scope` the feature that runs a chosen registered strategy instead of `PlaceholderTimed` for paper evidence (same follow up as specs 0018 and 0019).
- [ ] `/sync`: `packages/core/AGENTS.md` should list `strategy/noise_area.py` once it lands.
- [ ] Later, only if a trend candidate clears the fee hurdle: volatility target sizing (a new risk path), a Donchian lookback ensemble (needs fractional sizing), BTC plus ETH for more trades (the deferred ETH candles item), and a slow gate path for daily bars. Each needs its own `/architect`.
