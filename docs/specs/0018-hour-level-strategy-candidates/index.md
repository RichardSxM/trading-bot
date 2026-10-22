# 0018. Hour level strategy candidates: six rule based BTC strategies on one shared base, run one at a time through the gate

**Date**: 2026-10-21 (revised the same day after a cross check by another model)
**Status**: In Progress

## Summary

This feature writes six well known hour level trading ideas as real strategies and puts each one through the promotion gate of spec 0015, one at a time, on BTC 1 hour candles from April 2022. Every candidate shares one strategy base: it sizes each trade from its stop at 0.5% risk, places a protective stop, and exits on a target or a time limit. Each candidate only adds its own small, pure entry and exit rule. Each candidate's hypothesis and settings are committed in this spec before any result is seen. The verdict (pass, fail, or insufficient evidence) is written back into the candidate's file. To keep the family honest, the gate's trial count now spans every strategy of the same kind tested in the same lab, not each one alone.

## Structure

The umbrella decision (shared base, run protocol, family trial count) lives in this file. Each candidate is a child spec with its hypothesis, exact rules, settings, grid, tests, and a results section. Run order is the file order.

| # | File | Strategy | What it is |
|---|---|---|---|
| 1 | [0001-connors-short-rsi.md](0001-connors-short-rsi.md) | `connors_rsi` | Short period RSI extremes in the direction of the 200 bar average, exit at the 5 bar average. The tracer: it proves the shared base. |
| 2 | [0002-bollinger-rsi-reversion.md](0002-bollinger-rsi-reversion.md) | `bollinger_rsi_1h` | Close back inside a Bollinger band after an RSI extreme, only while the bands are flat, exit at the middle band. |
| 3 | [0003-nr4-inside-bar-breakout.md](0003-nr4-inside-bar-breakout.md) | `nr4_breakout` | A narrow (NR4) or inside bar, then a close beyond its edge. |
| 4 | [0004-failed-breakout-reversal.md](0004-failed-breakout-reversal.md) | `failed_breakout` | A sweep of the prior UTC day's high or low that closes back inside, then a close through the last swing the other way. |
| 5 | [0005-trend-rsi-pullback.md](0005-trend-rsi-pullback.md) | `trend_rsi_pullback` | Layer 1 of your layered plan: 20 EMA trend tag plus an RSI(14) pullback. |
| 6 | [0006-trend-rsi-macd.md](0006-trend-rsi-macd.md) | `trend_rsi_macd` | Layer 2: layer 1 plus a MACD histogram trigger, kept only if it beats layer 1. |

Deferred to a second round (a new `/architect` amendment): Brooks H2/L2 and the three push wedge, rule detected chart patterns (double tops and bottoms, channels, triangles, flags, support and resistance), divergence, layered RSI, regime gated versions of these six, and ETH.

## Requirements

**User stories**:
- As the trader, I want each hour level idea tested as real strategy code over years of BTC candles with every cost, so I know which ones survive fees and funding.
- As the trader, I want each hypothesis written down and committed before its result exists, so I can't bend it after the fact.
- As the trader, I want every verdict, fails included, kept in git next to the hypothesis, so I learn from the losers too.
- As the trader, I want the gate to count every idea I tried, so that testing many and keeping the lucky one doesn't pass.

**Acceptance criteria**:

*Shared base (`BarRuleStrategy`)*
- **AC-1** (bar order): `trading_core/node/bar_rule.py` provides `BarRuleStrategy`, a `KfsStrategy` subclass that copies `macd_baseline`'s order lifecycle unchanged (*Base lifecycle* below): its phases (`FLAT`, `ENTERING`, `PROTECTED`, `EXITING`), `StaleQuoteBlock`, the guard figures, `flatten(halt_id)` for the kill switch, the stop accept timer and `STOP_NOT_ACCEPTED` block, exit retry, `_refused`, and `on_position_closed`. It subscribes to `<instrument_id>-<bar_spec>-INTERNAL`. On each bar close, in this order: (1) the base's `Atr` is updated with the bar, then the rule with `update(bar, atr)`, always, also while blocked, in a position, or exiting; (2) in phase `ENTERING` or `PROTECTED` with a position, `bars_held` goes up by 1 and the exit step runs (AC-4); (3) in phase `FLAT` with no position and no exit sent at this bar, the entry step runs (AC-2); (4) in phase `EXITING`, nothing more (the exit retry timer owns it). At most one entry or exit is sent per bar close. No decision is made until the rule and the ATR both report ready (warmup inside the range). A zero volume bar (spec 0014 AC-11) is a normal bar. Any exception inside a bar decision is logged (`bar_decision_failed`) and sends nothing, as `macd_baseline` does.
- **AC-2** (entry and stop): the rule's `entry()` is a pure read of its state (it changes nothing) and returns `None` or an `EnterSignal(side, structural_stop, reason)`. Every signal is a one bar event: after `entry()` is asked, the base calls `rule.consumed()`, which clears the setup that fired, whether the entry is then approved, refused, or skipped. With no quote in the cache the entry is skipped (`entry_skipped_no_quote`, no decision row). Otherwise the base takes `mid` = the quote mid (the value `check_entry` judges against) and turns the signal into the stop actually used: for a long, `stop = min(structural_stop, mid − atr_floor_mult × ATR)` rounded down to the price tick; for a short, `max(structural_stop, mid + atr_floor_mult × ATR)` rounded up. A structural stop at or above the mid for a long (at or below for a short) is refused with a decision reason `bad_signal` and logged at WARNING (`bar_rule_bad_signal`), with nothing sent. The entry goes through `check_entry` with `stop_px` set and no `fixed_quantity`, so it is sized so that the loss at the stop, including both taker fees, `stop_slippage_bps`, and funding over the hold, is `risk_per_trade` of equity, then clipped to the caps (spec 0011). Every other `EntryRequest` field is sourced as in spec 0014 AC-13, except `funding_periods = funding_periods(now, time_stop_bars × bar_interval_ns)`. `InvalidRiskInput` is caught and logged (`risk_input_invalid`) as `macd_baseline` does. An approved entry is a market order (taker). A refusal is recorded as in `macd_baseline`; it is not retried, because the signal is consumed.
- **AC-3** (protective stop): when the entry fills completely, a reduce only stop market order is placed at the stop decided in AC-2 (the absolute price, not an offset from the fill). If the average fill price is already at or beyond that stop (at or below it for a long), the base sends a market exit with decision reason `stop_through` instead and places no stop. A stop that is rejected, denied, cancelled by someone else, or not accepted in time is handled exactly as `macd_baseline` handles it (`STOP_NOT_ACCEPTED` block, critical risk event, market exit).
- **AC-4** (exits): `bars_held` counts the bar closes since the bar the entry was sent on (the first bar close after it is 1). At each bar close in a position the base exits at market (cancelling the resting stop first, then `_submit_exit`) with reason `time_stop` when `bars_held ≥ time_stop_bars`, otherwise with the rule's reason when `rule.exit(trade)` returns one. `trade` is `TradeView(side, entry_px, stop_px, bars_held)` with `entry_px` the average fill price and `stop_px` the placed stop; the rule reads the bar it last got from `update`. A position closed from any source (its stop, an exit, the kill switch, the liquidation guard, the range end closer) cancels what rests, resets the base's trade state, and calls `rule.on_flat()`, which resets only the rule's trade state (setups follow each child spec's *Setup state*).
- **AC-5** (decision records): every entry decision (approved or refused) and every exit records a `decisions` row whose `inputs` hold `bar_close`, `mid`, `atr`, `structural_stop`, `stop`, `floor_applied` (bool), `bars_held` (exits), `verdict.figures.as_inputs()` (entries), and the rule's `values()`, in the same JSON value format as `macd_baseline`'s inputs. Entry reasons are the rule's code (for example `connors_long`); exit reasons are `target`, `time_stop`, `stop_through`, or a rule specific code named in its child spec.

*Indicators, code, and config*
- **AC-6** (indicators): the new pure module `trading_core/strategy/bar_indicators.py` provides `Sma(n)`, `Rsi(n)` (Wilder: average gain and loss seeded with the simple mean of the first n changes, then `(prev × (n − 1) + x) ÷ n`; RSI 100 when the average loss is 0 and the gain is not, 50 when both are 0), `Atr(n)` (Wilder true range: max of high − low, |high − previous close|, |low − previous close|; seeded with the mean of the first n true ranges; the first bar has no true range), and `Bollinger(n, k)` (middle = SMA(n), bands = middle ± k × the population standard deviation of the last n closes, square root by `Decimal.sqrt`). All are `Decimal` in and out and return `None` until ready: `Sma` from bar n, `Rsi` and `Atr` from bar n + 1, `Bollinger` from bar n. Unit tests check hand worked values and those warmup counts. `trading_core/strategy/indicators.py`, `macd_baseline`, and `buy_and_hold` are not edited, so their `code_hash` is unchanged (checked by `gate versions` before and after).
- **AC-7** (one module per candidate, base frozen): each candidate has a pure rule `trading_core/strategy/<name>.py` implementing the `BarRule` protocol of `trading_core/strategy/bar_rule.py`, and a thin node module `trading_core/node/<name>.py` (its config class and a `BarRuleStrategy` subclass that builds the rule), so its `code_hash` closure is its own (spec 0015 AC-1). Each is registered in `apps/lab/src/lab/strategies.py` with kind `hour_level` and passes the spec 0014 AC-19 look ahead prefix test, which runs over every registered candle strategy. `strategy/bar_rule.py`, `strategy/bar_indicators.py`, and `node/bar_rule.py` are in every candidate's closure, so they are built against all six rule shapes before the first `lab study` and not edited after it. A later change to the base goes into new modules (`bar_rule_v2`) used only by candidates not yet studied; a candidate whose base must change is a revision (AC-17).
- **AC-8** (config validation): the base config and each candidate config refuse bad values in their constructor with `ValueError`, so `lab study` exits 64 at validation, before any run (spec 0014 AC-6). Base: `bar_spec` as spec 0014 AC-11, `atr_period ≥ 2`, `atr_floor_mult > 0`, `time_stop_bars ≥ 1`. Each child spec's *Settings* lists its own checks.
- **AC-9** to **AC-14** (candidate rules): `connors_rsi` (AC-9), `bollinger_rsi_1h` (AC-10), `nr4_breakout` (AC-11), `failed_breakout` (AC-12), `trend_rsi_pullback` (AC-13), and `trend_rsi_macd` (AC-14) each behave exactly as the *Rules* and *Setup state* sections of their child spec, proven by the unit tests that child lists on hand built bar fixtures.

*Studies and verdicts*
- **AC-15** (study files): each candidate has `apps/lab/studies/<name>.toml`, the *Shared study settings* below plus the candidate's `[strategy.config]` and `[strategy.grid]` from its child spec, with `[study] name = "<name>"`, `lab = "candle"`, `seed = 42`. Every grid has 4 to 6 points. The study file is in a commit before `lab study` first runs it (verify step: the commit time is earlier than `studies.created_at`). A candidate is never run on real candles outside its study (no `lab backtest` of it before or during its study; synthetic fixtures in tests only), so every look at real data is a counted trial.
- **AC-16** (run protocol): candidates run in the order of *Structure*, one at a time. For each: `lab study`, `lab holdout`, `scripts/push-bundles.sh`, then `gate evaluate <name>` on the VPS. The next candidate's `lab study` starts only after the previous candidate's results commit (AC-17). A study that fails or is interrupted is resumed (spec 0015 AC-3, AC-5), never renamed to start over.
- **AC-17** (results record): after `gate evaluate`, the candidate's child spec gets its *Results* section filled in, in its own commit (`docs(specs): <name> verdict (0018)`), with: study name, the promotion id, the evaluation date, the verdict, every reason (code, value, threshold), and these metrics from the promotion's `metrics`: walk forward Sharpe, holdout Sharpe against the buy and hold and MACD baseline Sharpes, holdout return after costs, holdout marked max drawdown, deflated Sharpe probability, PBO, holdout trades, the trial count N, and the trial pool size. Fails and insufficient verdicts are recorded the same way and never removed.
- **AC-18** (one revision): after a `fail` or `insufficient` verdict, a candidate may get at most one revision. It is written into its child spec as *Revision 1* before it runs: what changes and why, citing development and walk forward evidence only (never holdout figures). It is committed, then runs as a new strategy name and study `<name>_r1` with its own node module. A switch to `4-HOUR-LAST` bars is a valid revision. It counts in N, gets its own results labelled "holdout seen" (its holdout is the same year whose verdict prompted it), and is final. No revision for a candidate that passed.
- **AC-19** (family trials, amends spec 0015 AC-11 and AC-19): `gate evaluate` sets N to the count of distinct `study_runs.frozen_hash` over `development` and `holdout` rows of every study whose strategy has the same `strategies.kind` and whose `studies.lab` equals the studied one (benchmark rows never count, failed runs do). The deflated Sharpe trial variance uses the same family: one per day Sharpe per distinct `frozen_hash` with a `completed` `development` run (its `run_results.risk_adjusted_ratio ÷ √365`, the earliest such run when a hash repeats, runs with a NULL ratio skipped), replacing today's variance over the study's own grid points. The promotion `metrics` gain `trial_scope: "kind+lab"` and `trial_pool` (the number of Sharpes in the variance). `/strategies/{study_id}` shows both the study's own trial count and "trials in family (N)" from the promotion `metrics`. Existing promotions are not changed. The family rule applies to `scalping` replay studies too (feature 16). The N of the first candidate's evaluation is written into its results as N0, so earlier family studies already on the VPS are visible.
- **AC-20** (layer comparison): `trend_rsi_macd`'s settings shared with layer 1 are copied, before it runs, from the `grid_point` of the `completed` `holdout` row of the original `trend_rsi_pullback` study (never a revision's), resolved through that study's stored plan. Its results record whether the MACD layer is kept: kept only when its verdict is `pass` and its walk forward Sharpe is above layer 1's.
- **AC-21** (close out): when all six candidates, plus any revision started, have a verdict, `gate evaluate` runs once more for each of their studies, so every one is also judged with the final family N (new promotion rows; the first rows stay). Each child's results add a "final family N" line with that verdict. The scope row is then ticked. A `pass` doesn't start paper trading here; it adds the follow up in *Follow-up*.

## Decision

**Chosen option**: Option 2: one shared `BarRuleStrategy` node base with a small pure rule per candidate, run one at a time through the existing study and gate, with the trial count widened to the strategy kind and lab.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `htmx` (`.claude/skills/htmx/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

```
strategy/bar_indicators.py (Sma, Rsi, Atr, Bollinger)   strategy/indicators.py (Ema, Macd, unchanged)
            └──────────────┬──────────────────────────────────────┘
strategy/<name>.py  (pure BarRule: update, ready, entry, consumed, exit, on_flat, values)
            ▼
node/<name>.py  ─►  node/bar_rule.py BarRuleStrategy (macd_baseline lifecycle, ATR floor, check_entry, stop, exits, decisions)
            ▼
lab registry (hour_level) ─► lab study ─► lab holdout ─► push-bundles ─► gate evaluate (family N) ─► child spec Results
```

### Pure rule contract (`trading_core/strategy/bar_rule.py`)

- `BarInput(ts_ns, open, high, low, close, volume)`, all `Decimal` except `ts_ns`; `ts_ns` is the bar close.
- `Side` (`long`, `short`), `EnterSignal(side, structural_stop, reason)`, `TradeView(side, entry_px, stop_px, bars_held)`.
- Every rule is built with `bar_interval_ns` (from `bar_spec`) plus its own settings.
- `BarRule` protocol:
  - `update(bar, atr)` (`atr` is the base's value, `None` until ready; no rule keeps its own ATR)
  - `ready() -> bool`
  - `entry() -> EnterSignal | None` (a pure read, asked only when flat, after `update`)
  - `consumed() -> None` (clears the setup that fired)
  - `exit(trade) -> str | None` (asked only in a position, after `update`)
  - `on_flat() -> None` (resets trade state)
  - `values() -> dict[str, Decimal | str | int | bool | None]`
- `r_target(trade, target_r) -> Decimal` (`entry_px ± target_r × |entry_px − stop_px|`), shared by the R multiple candidates. The stop floor (`floored_stop(side, structural_stop, mid, atr, mult, tick) -> tuple[Decimal, bool]`) also lives here as a pure function the base calls.

### Base lifecycle (events, all as `macd_baseline`)

| Event | Phase before | What the base does |
|---|---|---|
| bar close, flat, signal | `FLAT` | AC-2; approved → market entry, `ENTERING` |
| entry denied or rejected | `ENTERING` | risk event, back to `FLAT` |
| entry fully filled | `ENTERING` | stop at the decided price, accept timer (AC-3), or `stop_through` exit |
| stop accepted | `ENTERING` | `PROTECTED` |
| stop rejected, denied, cancelled by others, or late | `ENTERING`, `PROTECTED` | `STOP_NOT_ACCEPTED` block, critical risk event, `EXITING`, market exit |
| bar close, exit condition | `ENTERING`, `PROTECTED` | cancel the stop, `EXITING`, market exit |
| exit refused | `EXITING` | risk event, retry in 1 s |
| `flatten(halt_id)` | any | cancel the entry, kill switch tagged market exit |
| position closed (any source) | any | cancel what rests, reset, `rule.on_flat()`, `FLAT` |

### Base config (`BarRuleConfig`, every candidate inherits it)

| Field | Default | Meaning |
|---|---|---|
| `bar_spec` | `"1-HOUR-LAST"` | bar type (spec 0014 AC-11) |
| `atr_period` | 14 | ATR for the stop floor and the rules |
| `atr_floor_mult` | `"1"` | minimum stop distance in ATRs |
| `time_stop_bars` | per candidate | exit after this many bar closes |
| `use_market_context` | true | funding through `market_context()` |
| `stale_quote_s`, `stale_check_s` | as spec 0014 AC-13 | the `StaleQuoteBlock` mixin |

### Shared study settings (every `apps/lab/studies/<name>.toml`)

```toml
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2022-04-01                    # first whole month after the 2022-03-24 floor
to = 2025-10-18                      # the day before holdout_start
seed = 7                             # the run seed (look ahead cut times)
stale_quote_s = 300
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]                               # every value written out, none left to a default
max_order_notional = "20000"
max_leverage = "2"
max_total_exposure = "20000"
risk_per_trade = "0.005"
maintenance_margin_rate = "0.01"
min_liq_distance_pct = "10"
liq_stop_multiple = "3"
stop_slippage_bps = "5"              # the sizing's assumed stop slippage, separate from slippage_bps

[strategy]
name = "<name>"
[strategy.config]                    # the candidate's fixed settings
[strategy.grid]                      # 4 to 6 points
[study]
name = "<name>"
lab = "candle"
seed = 42                            # the gate's bootstrap seed
```

Folds use the candle defaults (12 months in sample, 6 month steps), which gives 5 folds from 2022-04-01 to 2025-09-30. The holdout runs from 2025-10-19 to the last complete UTC day. Each run warms up inside its own range, so the holdout loses its first `ready` bars (about 8 days for an SMA(200) candidate).

### Data model sketch

No migration. Strategies, versions, studies, runs, promotions, and folds are the spec 0002 and 0015 tables, unchanged. New rows only: one `strategies` row per candidate (kind `hour_level`), and the usual study, run, and promotion rows. The AC-19 change is a query change in `trading_core/recording/gate_queries.py` (trial count and the new trial Sharpes input) plus a new `CandidateInputs.trial_sharpes: tuple[float, ...]` field that `trading_core/gate/verdict.py` uses for the deflated Sharpe variance.

### State transitions

The base's phases are in *Base lifecycle*. Each candidate's own setup states (pending setup bar, armed, trigger window) are inside its pure rule and listed in its child spec's *Setup state*. Shared rule for all of them: while a position is open, setups keep updating, but any trigger that would fire is dropped (it is consumed, not saved for later).

### API surface (no new commands)

| Command or route | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `lab study apps/lab/studies/<name>.toml` | CLI | study file | development runs, bundles | `lab_app` | 64 bad file, grid, or config, 2 missing candles or funding, 1 a grid point failed |
| `lab holdout <name>` | CLI | study name | holdout plus benchmark runs, bundles | `lab_app` | 64 not ready or spent, 1 a run failed |
| `lab gate <name> --dry-run` | CLI | study name | verdict preview (laptop family only) | `lab_app` SELECT | 64 unknown study |
| `scripts/push-bundles.sh <dir>...` | shell | batch dirs | import summary on the VPS | Tailscale SSH as `kfs` | nonzero on rsync or import failure |
| `gate evaluate <name>` | CLI (VPS) | study name | promotion id, verdict, reasons, metrics (family N, pool) | `gate_app` | 64 unknown study |
| `GET /strategies/{study_id}` | GET | uuid | study detail with both trial counts | dashboard session | 404, 422 |

### Value sourcing

| Action | Value | Source |
|---|---|---|
| bar close | OHLCV of the bar | the internal bar from trade ticks (spec 0014 AC-11) |
| every bar | ATR | the base's `Atr(atr_period)`, passed to the rule in `update` |
| rule | `bar_interval_ns` | parsed from `bar_spec` by the node module |
| entry | `mid` | the cached quote's mid at the bar close (none → skip) |
| entry | structural stop | the candidate rule (its child spec *Rules*) |
| entry | stop used | `floored_stop` with `mid`, `atr_floor_mult`, and the instrument's price tick |
| entry | quantity | `check_entry` sizing from the study file `[risk]` (`risk_per_trade` 0.005, `stop_slippage_bps` 5), clipped to the caps |
| entry | funding periods | `funding_periods(now, time_stop_bars × bar_interval_ns)` |
| entry | equity, open notional, maintenance rate, size step, funding rate, blocks | as spec 0014 AC-13 (`PlaceholderTimed` sourcing) |
| exit | `entry_px` | the entry order's average fill price |
| exit | `stop_px` | the placed protective stop |
| exit | `bars_held` | bar closes counted by the base since the entry bar |
| exit | R multiple target | `r_target(trade, target_r)` |
| study | ranges, folds | `thresholds.toml` `holdout_start` (2025-10-19) and candle fold defaults |
| study | grid points | the child spec's grid, copied into the study file |
| `trend_rsi_macd` study | layer 1 settings | the `grid_point` of the original `trend_rsi_pullback` study's `completed` `holdout` row, through its stored plan's grid |
| `gate evaluate` | N | `count(distinct study_runs.frozen_hash)` joined through `studies` (same `lab`) and `strategies` (same `kind`), `role <> 'benchmark'` |
| `gate evaluate` | trial Sharpes, `trial_pool` | per distinct `frozen_hash` of that family's `completed` `development` runs: the earliest run's `run_results.risk_adjusted_ratio ÷ √365`; the pool is their count |
| results | every metric and reason | the promotion row's `metrics` and `reasons` on the VPS (`gate evaluate` output) |
| results | N0 | the first candidate's evaluation `metrics.trial_count` |
| results | layer kept or not | `trend_rsi_macd` and `trend_rsi_pullback` promotions' walk forward Sharpe metrics |

### Key invariants

- No float for money: prices, stops, targets, indicators, and sizes are `Decimal` end to end. The gate's statistics stay float, as spec 0015 AC-6.
- One position at a time, one action per bar close, no decision before warmup.
- Every open position has a resting protective stop, or is being closed.
- A stop is never closer than `atr_floor_mult × ATR` to the decision mid.
- Every signal is used at most once; a refused or dropped signal is never retried later.
- A candidate's rules, grid, and base modules never change after its first `lab study` run. Any change is a revision (AC-18) under a new name.
- Real candle data is seen by a candidate only through its study.
- `indicators.py`, `macd_baseline`, and `buy_and_hold` stay byte identical, so benchmark hashes don't move.

### Security model

Nothing new. The lab runs on the laptop as `lab_app` in demo, the gate on the VPS as `gate_app`; the dashboard stays read only. No Kraken key is involved, and no regulated data. The repo is public, so every hypothesis and verdict is world readable, which is fine.

### Configuration required

None. No new settings or secrets; six study files and six registry entries.

### Critical test scenarios

- Happy path: `connors_rsi` runs `lab study`, `lab holdout`, push, import, and `gate evaluate` end to end, and the verdict lands in its child spec, verifies **AC-9**, **AC-15**, **AC-16**, **AC-17**
- Stop floor: a structural stop 0.2 ATR from the mid is moved to 1 ATR and rounded away from the mid; one on the wrong side of the mid is refused as `bad_signal`, and the setup is consumed, verifies **AC-2**
- Sizing: with equity 10,000, the expected quantity is `risk_per_trade × equity ÷ loss_per_unit` (the stop distance plus both taker fees, 5 bps stop slippage, and funding) floored to the size step; a very tight stop is clipped to `max_order_notional`, verifies **AC-2**
- No quote: a bar close with no cached quote skips the entry with `entry_skipped_no_quote` and records no decision, verifies **AC-2**
- Stop through: a fill beyond the decided stop exits at market with `stop_through`, verifies **AC-3**
- Stop rejected: a rejected stop sets `STOP_NOT_ACCEPTED` and exits, as `macd_baseline`'s test, verifies **AC-3**
- Time stop: a position still open after `time_stop_bars` closes exits with `time_stop` at that close, and no entry is sent at the same close, verifies **AC-1**, **AC-4**
- Stuck exit: a refused exit retries after 1 s, and bar closes meanwhile send nothing, verifies **AC-1**
- Kill switch: `flatten` cancels a pending entry and closes the position with the kill switch tags, verifies **AC-1**
- Outside close: the liquidation guard closes a position; the stop is cancelled, the trade state resets, and `rule.on_flat` runs, verifies **AC-4**
- Indicators: `Rsi`, `Atr`, `Sma`, `Bollinger` match hand worked sequences with their warmup counts, including the RSI 100 and 50 edge cases, verifies **AC-6**
- Config: `time_stop_bars = 0` in a study file exits 64 before any run, verifies **AC-8**
- Look ahead: every candidate passes the spec 0014 prefix test, verifies **AC-7**
- Family N: two `hour_level` candle strategies with 4 and 2 distinct hashes give N = 6 and a pool of 6 for each study; a `scalping` replay study and an `hour_level` replay study are not counted, verifies **AC-19**
- Hash stability: `macd_baseline`'s and `buy_and_hold`'s `code_hash` are the same before and after the feature, verifies **AC-6**

## Build plan

Tracer Bullet: `connors_rsi` goes end to end first (indicators, base, rule, study, the family N change on the VPS, verdict in git). The base is built against all six rule shapes in slice 1 and is then frozen (AC-7), so later slices only add rules.

**Slice 1: thin thread (`connors_rsi` to a recorded verdict)**
1. `strategy/bar_indicators.py` (`Sma`, `Rsi`, `Atr`, `Bollinger`) with hand worked and warmup tests. Satisfies **AC-6**
2. `strategy/bar_rule.py` (types, `BarRule`, `floored_stop`, `r_target`), checked against every child spec's *Rules* and *Setup state* so no later candidate needs a base change. Satisfies **AC-2**, **AC-4**, **AC-7**
3. `node/bar_rule.py` `BarRuleStrategy` and `BarRuleConfig` with validation: the `macd_baseline` lifecycle, bar order, ATR floor on the mid, `check_entry` with the hold horizon, consumed signals, market entry, protective stop at the decided price, `stop_through`, exits, outside close reset, decision inputs. Tests on a short candle fixture through the candle lab, including the lifecycle scenarios above. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-8**
4. `connors_rsi` pure rule, node module, registry entry, the child spec's tests, the look ahead prefix test. Satisfies **AC-7**, **AC-8**, **AC-9**
5. Family trials: the `gate_queries` count and trial Sharpes by kind and lab, `CandidateInputs.trial_sharpes`, `verdict.py` variance, `trial_scope` and `trial_pool` metrics, the study detail page. Tests with a throwaway database. Deploy to the VPS (expect the spec 0015 AC-26 refusal if the placeholder's closure changed, then promote and deploy again). Satisfies **AC-19**
6. `apps/lab/studies/connors_rsi.toml`, committed; run study, holdout, push, evaluate; fill in *Results* with N0. Satisfies **AC-15**, **AC-16**, **AC-17**

**Slice 2: mean reversion, second candidate**
7. `bollinger_rsi_1h` rule, node, registry, tests, study file; run to a verdict. Satisfies **AC-8**, **AC-10**, **AC-15**, **AC-16**, **AC-17**

**Slice 3: breakouts and fades**
8. `nr4_breakout` rule, node, registry, tests, study file; run to a verdict. Satisfies **AC-8**, **AC-11**, **AC-15**, **AC-16**, **AC-17**
9. `failed_breakout` rule (prior UTC day levels from `bar_interval_ns`, `SwingTracker` swings), node, registry, tests, study file; run to a verdict. Satisfies **AC-8**, **AC-12**, **AC-15**, **AC-16**, **AC-17**

**Slice 4: your layered plan**
10. `trend_rsi_pullback` rule (20 EMA trend tag through `indicators.Ema`), node, registry, tests, study file; run to a verdict. Satisfies **AC-8**, **AC-13**, **AC-15**, **AC-16**, **AC-17**
11. `trend_rsi_macd` rule (layer 1 plus `indicators.Macd`), node, registry, tests; study file with layer 1's holdout settings; run to a verdict; record the layer comparison. Satisfies **AC-8**, **AC-14**, **AC-15**, **AC-16**, **AC-17**, **AC-20**

**Slice 5: revisions and close out**
12. For each `fail` or `insufficient` candidate you choose to revise: *Revision 1* in its child spec, commit, `<name>_r1` module and study, run to a verdict. Satisfies **AC-18**
13. Close out: `gate evaluate` again for every study with the final family N, "final family N" lines in each *Results*, tick the scope row, add the paper follow up if any passed. Satisfies **AC-21**

## Consequences

**Positive**:
- Six real verdicts over four and a half years of BTC history with honest costs, with the hypothesis committed before each result.
- New candidates after this one are small: a pure rule, a thin node module, a study file.
- The family trial count makes the deflated Sharpe check harder to beat by trying many ideas, which is the risk this feature creates.

**Negative / tradeoffs**:
- Expect mostly `fail` and `insufficient` verdicts; the gate is strict on purpose and the holdout year has to beat buy and hold.
- The base is frozen after the first study. A base bug found later can't be fixed in place for studied candidates: they keep the old code, or get a revision.
- All candidates share one holdout year (2025-10-19 on). Each spends its own holdout, but six looks at the same year is still selection; the family N covers only part of it.
- Earlier candidates are first judged at a smaller N than later ones. The close out evaluation evens this out, but the first verdicts stay in the log.
- The family N counts only what reaches the VPS. Discipline (no `lab backtest` of a candidate on real data) carries the rest, and `lab gate --dry-run` on the laptop can show a different N.
- The family N raises the bar for every later hour level candle study, including `macd_baseline` studies, and also for feature 16's scalping replay family.
- Structural stops on narrow setups often hit the ATR floor, so some candidates trade a wider stop than their idea suggests.
- Market entries on the close cost the taker fee and miss the move inside the breakout bar.
- Every signal is one bar long, so a refused entry loses that trade for good.
- Mean reversion candidates bet against BTC's trend at times; symmetric rules in a rising market may still fail `concentration_direction`.
- `bollinger_rsi_1h` and `failed_breakout` may not reach 60 holdout trades and come back `insufficient`.
- A revision's holdout is the year that judged its parent, so its verdict is weaker evidence ("holdout seen").
- Changing `gate_queries.py` may change the placeholder's `frozen_hash` if it is in its import closure, so that deploy needs a manual paper promotion first.

**Neutral**:
- No migration and no new setting. Six registry entries, six study files, and about fifteen new core modules.
- Spec 0015 AC-11 and AC-19 are amended by AC-19 here (a pointer is added there).

## Follow-up

- [x] Spec 0015: note on AC-11 and AC-19 that the trial count and variance span the strategy kind and lab (spec 0018 AC-19).
- [ ] On the first `pass`: `/scope` a feature that lets the trader run a chosen registered strategy instead of `PlaceholderTimed`, so the passer can gather paper evidence (spec 0015 AC-16).
- [ ] Second round (`/architect hour level strategy candidates, round 2`): Brooks H2/L2 and wedge, rule detected chart patterns, divergence, layered RSI, regime gated versions (20 EMA trend tag or ADX), and ETH once the candle lab has PF_ETHUSD.
- [ ] `/sync`: `packages/core/AGENTS.md` should list `strategy/bar_rule.py`, `strategy/bar_indicators.py`, and `node/bar_rule.py`.
