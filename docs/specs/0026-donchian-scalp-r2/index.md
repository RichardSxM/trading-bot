# 0026. donchian_scalp_r2: maker orders and a volatility floor on r1's signals

**Date**: 2026-10-31 (amended 2026-10-31: AC-5's `stop_through` check also reads the cached quote, and a rejected stop is settled by the quote, as spec 0017 AC-18; both first screens were blocked for the rest of the run by a stop that was already through at a wide spread; amended again 2026-10-31: AC-17 screens run from new `_v2` study files, since the lab refuses to resume a study whose code changed)
**Status**: In Progress

## Summary

`donchian_scalp_r2` takes r1's signals and changes how it trades them. The entry and the planned exits become post only limit orders (maker orders, which rest in the book and pay the lower maker fee). Entries are skipped when the bars are too small for the fee to be worth paying. The protective stop and the kill switch close stay market orders, so a fast move can never leave a position unprotected. r1 stays frozen. r2 runs a candle screen on BTC and on PEPE, then it replaces r1 on the VPS demo whatever the verdict.

Be honest with yourself about what to expect. On r1's own 5,002 BTC signals the best maker version with this filter still lost about 0.36 to 0.7 ATR per trade in sample (see `rationale.md`). r2 is a cheap test of whether lower costs bring the edge back. It is not a likely winner.

## Requirements

**User stories**:
- As the trader, I want every planned order to rest as a limit, so that I pay the maker fee and no spread on most trades.
- As the trader, I want the stop and the kill switch to stay market, so that lower costs never come at the price of safety.
- As the trader, I want entries skipped when ATR is small next to the fee, so that the worst losing trades never happen.
- As the trader, I want r2 screened against r1 in the lab and then running on demo, so that I see both the backtest and how maker orders behave live.

**Acceptance criteria**:
- **AC-1** (new base): a new module `trading_core/node/bar_rule_maker.py` with `MakerBarRuleStrategy` and `MakerBarRuleConfig`. It runs a `BarRule` (`trading_core.strategy.bar_rule`) bar by bar with the same order of work as `BarRuleStrategy`: update the ATR and the rule on every bar close, decide nothing until both are ready, run the exit step in a position, the entry step while flat, and otherwise consume and drop the signal. At most one entry or exit decision per bar close. Its phases are `FLAT`, `ENTERING`, `PROTECTED`, `EXITING`, with the same string values as the frozen base's `Phase`. It has `flatten` (AC-11) and `resume_position` (AC-12). No module in any existing strategy's version closure is edited (AC-15).
- **AC-2** (entry limit): an approved entry is a post only, GTC limit order for the approved quantity at `close − entry_offset_atr × ATR` for a long, or `close + entry_offset_atr × ATR` for a short. `close` is the signal bar's close and ATR is the base's ATR at that bar. The price is rounded to the tick away from the market: down for a buy, up for a sell. With `entry_offset_atr = 0.5` this is the price the study tested.
- **AC-3** (entry sizing and stop): the stop is computed exactly as r1 computes it: the rule's structural stop (`close ± 3 ATR`), floored by `floored_stop` at `atr_floor_mult` ATR from the mid of the cached quote. `check_entry` (frozen risk code, unchanged) sizes the entry with the limit price in its `mid` field, so the risk per trade is measured from where the order fills, not from the mid. The fee passed is the taker fee, as today (an upper bound on costs). If the limit is not strictly on the profit side of the stop, the entry is refused as `bad_signal` with no order. The decision inputs carry `mid`, `limit_px`, `entry_offset_atr`, and the stop fields the base records.
- **AC-4** (entry wait): the wait is counted in bar closes, not by a timer, so the lab and live behave the same. At the start of each bar close, before the ATR, rule, and steps, an entry still working that was submitted `entry_wait_bars` (3) or more bar closes ago has its remainder cancelled. With nothing filled, the round trip ends (back to `FLAT`) with an INFO log `maker_entry_timeout` and no risk event. With a part filled, that part is kept (AC-5). An unfilled entry never counts toward the trades per day cap (the frozen `risk/limits.py` counts opened positions); a part filled one counts as one trade.
- **AC-5** (partial fills): on the entry's first fill, the reduce only stop market order is submitted for the position's quantity, with the base's accept timeout and `STOP_NOT_ACCEPTED` fail closed path. Each later fill (including fills that arrive after r2 asked to cancel the remainder, up to the cancel's confirmation) modifies the stop's quantity to the position's quantity. The modify follows `ScalpStrategy`'s rule (`_sync_stop`): it is skipped while the stop is `TRIGGERED`; a refused modify logs `stop_modify_rejected` and is tried again at the next fill or bar close; only a stop that is no longer open fails closed (`STOP_NOT_ACCEPTED`, exit at market). The `OrderCanceled` of an entry r2 cancelled itself, with a position open, is the normal end of the entry, not a failure. `bars_held` counts bar closes from the first fill, so the time stop runs from there. The `stop_through` check runs once, on the first fill, before the stop is submitted (amended 2026-10-31, the rule of spec 0017 AC-18): the stop is through when the first fill's price is at or beyond `stop_px`, or the cached quote is (bid at or below `stop_px` for a long, ask at or above it for a short; the frozen `strategy.scalp.stop_through` helper, imported unchanged). With no cached quote, the fill price alone decides, as in the base. When it is through, no stop is placed: the entry's remainder is retired (a later fill of it closes at once as `late_entry_fill`, AC-10) and the position exits at market through `_decide_exit` with reason `stop_through`, logged at INFO, with no block and no risk event, so the next signal can trade. Later fills update the average entry price but never trigger it.

  A stop the venue **rejects** after that check is handled as `_stop_failed` handles a failure, minus the block and the risk event: the stop id is forgotten (kept, with the venue's reason, in a pending verdict), the accept timer is cancelled, the entry remainder is retired, the exit limit and its 5 s guard are cancelled, the phase is `EXITING`, and the market exit goes out at once unless a market close is already under way (AC-11). That market exit carries the decision tag the base gives it (the exit decision if one exists, else the entry decision). The rejection is logged at WARNING with code `stop_rejected` and the venue's reason. The pending verdict is then settled by a quote: the cached quote at the moment the rejection arrives, or else the next quote (`on_quote_tick`). If that quote has the stop through (the bid or ask test above, by quote only), a new `exit` decision `stop_through` is recorded with no order of its own (intent `exit`, the closing side, approved), logged at INFO, with no block and no risk event. Otherwise the `STOP_NOT_ACCEPTED` block and the critical risk event follow, as the base's `_stop_failed` writes them. The pending verdict lives outside the trade state: the position closing and the reset to `FLAT` leave it waiting. While it waits, r2's entry refusal reads it as an active block and refuses with `blocked:stop_not_accepted` (fail closed). `flatten` (AC-11) and `resume_position` (AC-12) leave it waiting; `on_stop` with it still waiting records the block and the risk event. A stop that is denied, cancelled by someone else, not accepted in time, or no longer open keeps the base's `STOP_NOT_ACCEPTED` path unchanged.

  Both `stop_through` rows carry one input set: `stop_px`, `bid`, `ask`, `mid` (null when there is no cached quote), `entry_px` (the average fill price so far), `bars_held`, and `bar_close` when a bar has closed. The first fill row also carries what `_decide_exit` adds (`position_qty`, `exit_mode = "market"`).
- **AC-6** (post only entry rejected): an entry rejection with `OrderRejected.due_post_only` true (the field exists on Nautilus 1.231's event) drops the signal: back to `FLAT`, an INFO log `maker_entry_rejected`, no risk event, no retry. Any other denial or rejection of the entry, including a post only one where the venue does not set the flag, keeps the base's WARNING `order_rejected` risk event (fail loud). Task 2's spike records whether the sandbox and the candle lab set the flag.
- **AC-7** (planned exit limit): a `midline_exit` or `time_stop` decision sends a post only, reduce only, GTC limit for the position's quantity at the passive touch from the cached quote: a long sells at the best ask, a short buys at the best bid. The protective stop stays resting and keeps protecting the position until the position is closed; it is not cancelled before this exit. A fill of the exit limit that leaves a position shrinks the stop to the position's quantity (the AC-5 modify rule). The exit decision's inputs carry `exit_limit_px` and `exit_mode = "maker"`. With no cached quote, or a quote older than `stale_quote_s`, the exit goes straight to market with `exit_mode = "market"` and `why` = `no_quote` or `stale_quote` in its inputs.
- **AC-8** (exit fallback): the exit limit's remainder is replaced by a reduce only market order when any of these happens: (a) at the start of a bar close, the limit was submitted `exit_wait_bars` (3) or more bar closes ago, (b) any rejection or denial of the limit (post only or not; an exit is never blocked or failed for it), (c) it is cancelled or expires without r2 asking. For (a), r2 cancels the limit and sends the market order when the cancel is confirmed, or after a 5 s guard timer if no confirmation arrives. The market order is sent only while a position still exists, sized from the position's quantity at that moment (both are reduce only, so they can never close more than the position). The market order carries the same exit decision tag. One INFO log `exit_fallback_market` with `why` = `timeout`, `post_only_rejected` (flag set), `rejected` (flag not set), or `canceled`. No new decision row. A refused market exit retries every 1 s, as in the base. Every r2 timer is cancelled when the position closes. With the 60 bar time stop plus a 3 bar exit wait, a position can last up to 63 bars; that is intended.
- **AC-9** (exits stay market): a `stop_through` exit, a stop that fails (rejected, whatever its later verdict, denied, not accepted in time, no longer open), a late entry fill, and every resumed exit (AC-12) close at market, never by limit. The protective stop is always a stop market order.
- **AC-10** (one order in flight): a signal while an entry rests, while in a position, or while an exit rests is consumed and dropped (the base's rule). Once an exit is decided it stands: a later close back across the midline does not cancel it. An exit decided while part of the entry still rests first cancels that remainder; a fill of it that still arrives is closed at once at market (`late_entry_fill`), as `ScalpStrategy` does.
- **AC-11** (kill switch): `flatten` cancels a resting entry and a resting exit limit, then closes the position at market with the kill switch tags. It is idempotent: a market close already sent is not sent again, but a resting exit limit does not count as a close sent. During the AC-8 cancel guard it sends the market close at once and cancels the guard timer, so only one market close goes out. The cancel of the entry it asked for is not logged as `maker_entry_timeout`. A stop accept timeout that fires while a market close is under way (kill switch or any other) does nothing. The stop rests until the position closes, as in the base.
- **AC-12** (restart): `resume_position` takes back a position under its resting stop as the base does (`bars_held` from the position's open time; no stop fails closed). Any r2 limit (entry remainder or exit) found open at the restart is cancelled, and no decision is recorded for that. A position past `time_stop_bars` exits at once at market with a decision row `reason = time_stop`, inputs `exit_mode = "market"` and `resumed = true`. Otherwise the next bar's exit step decides again.
- **AC-13** (volatility floor): after the base's own refusals (gate halted or paused, any entry block) and before the spread check, an entry whose `ATR / close × 10,000` is strictly below `atr_min_bps` (10) is not sent. A decision row is recorded with intent `enter`, the signal's side and reason, verdict rejected, `risk_reason = "vol_low"`, and inputs holding the bar inputs plus `structural_stop`, `atr_bps`, and `atr_min_bps`. One INFO log `entry_skipped_vol`. No risk event and no alert. `atr_bps` equal to the floor passes. With `atr_min_bps = None` the check is off.
- **AC-14** (r2 node and config): a new node `trading_core/node/donchian_scalp_r2.py` with `DonchianScalpR2(MakerBarRuleStrategy)` and `DonchianScalpR2Config`. It builds `DonchianMidlineRule` (imported unchanged) with `entry_bars` 20, `stop_atr` 3, `ema_period` 200, and keeps r1's spread check (AC-6 of spec 0025, `spread_max_atr`, after the volatility floor) and r1's warmup seed join (AC-7 to AC-10 of spec 0025, same codes `warmup_seed_failed` and `seed_stale`), both rewritten in its own module. Config: `bar_spec` must be `5-MINUTE-LAST`, `time_stop_bars` 60, `atr_period` 14, `atr_floor_mult` 1, `entry_offset_atr` 0.5 (at least 0), `entry_wait_bars` 3 (at least 1), `exit_wait_bars` 3 (at least 1), `atr_min_bps: Decimal | None` 10 (when set, at least 0), `spread_max_atr: Decimal | None` (when set, above 0). A bad value refuses at config build time. Decision inputs carry the rule's four values (`ema`, `midline`, `entry_high`, `entry_low`) next to `bar_close` and `atr`, as r1's do.
- **AC-15** (frozen modules): no file in the version closure of any existing strategy is edited, `r1` included (`node/donchian_scalp_r1.py`, `node/bar_rule.py`, `node/scalp_base.py`, `strategy/*`, `risk/*`, `recording/*`, every `__init__.py`). New modules are not exported from any existing `__init__.py`. Importing a frozen helper unchanged (such as `strategy.scalp.stop_through`, AC-5) is not an edit. A hash baseline is taken in task 1. At close out, every existing strategy's version hash is unchanged, and the module hash list matches the baseline line for line except the new `donchian_scalp_r2` line. Trader side files (`apps/trader/...`) sit outside every closure. If a trader side change needs a module inside `trading_core`, the build first prints the closures with the hash tool and confirms it is in none.
- **AC-16** (paper wiring): `PAPER_STRATEGY` accepts `donchian_scalp_r2`. `paper_version` records name `donchian_scalp_r2`, kind `scalping`, and a config holding every setting: `ema_period` 200, `entry_offset_atr` `"0.5"`, `entry_wait_bars` 3, `exit_wait_bars` 3, `atr_min_bps` `"10"`, `spread_max_atr` `"0.1"`, and `instruments` (sorted `PAPER_INSTRUMENTS`). More than one coin is allowed with `donchian_scalp_r1` or `donchian_scalp_r2`. The warmup seed fetch runs for both. Each coin gets its own r2 with its own `order_id_tag` and the per coin blocks wrapper (`trader/coin_blocks.py`), exactly as r1 does. Outside demo the config builder refuses. A new trader side view `MakerBarRuleView` gives the snapshots `exiting` (true while an exit limit or market exit is working), `position_opened_ns`, and the stop order, so the Overview card works unchanged. The card shows positions only; a resting entry is not shown.
- **AC-17** (lab screens): the lab registry gains `donchian_scalp_r2` (`kind=SCALPING`, `candle_screen=True`, `candle_gate=False`, `run_fields=_candle_fields`), its factory built with an empty seed. Two study files are committed before either runs: `studies/donchian_scalp_r2_v2.toml` (study name `donchian_scalp_r2_v2`) and `studies/donchian_scalp_r2_pepe_v2.toml` (study name `donchian_scalp_r2_pepe_v2`) (amended 2026-10-31). Each is the matching r1 study file (`donchian_scalp_r1.toml`, `donchian_scalp_r1_pepe.toml`) with the r2 strategy name and these lines spelled out in `[strategy.config]`: `entry_offset_atr = 0.5`, `entry_wait_bars = 3`, `exit_wait_bars = 3`, `atr_min_bps = 10`, with `spread_max_atr` left off. The first two files, `studies/donchian_scalp_r2.toml` and `studies/donchian_scalp_r2_pepe.toml`, stay committed unchanged, and their studies stay in Postgres as invalid trials: they ran on the code from before the AC-5 amendment, and a block that never clears stopped each one after its first week. The lab keys a study by name and code hash and refuses to resume it on new code (exit 64), so the reruns need new names. All four count as trials in the scalping family (as spec 0017 counted its void screens). `verify.md` names the two invalid studies and their batch ids, with no figures table. Each runs through `lab study`, then `lab screen`. `verify.md` records for each coin: the screen verdict, trades per day, net P&L, gross P&L per trade, cost per trade, daily Sharpe, max drawdown, the entry fill share, the maker exit share, the count of `vol_low` skips, and the r2 minus r1 difference of each figure r1 also has (r1's fill shares are n/a). The **entry fill share** is entries with any fill divided by approved `enter` decisions (so `vol_low`, `spread_wide`, and `bad_signal` rows are not in it). The **maker exit share** is planned exit decisions (`midline_exit`, `time_stop`) whose limit closed the whole position, divided by all planned exit decisions; a part filled exit counts as a fallback. Both come from the run bundle's `decisions`, `orders`, and `fills` (with `liquidity`) through one committed script, `apps/lab/studies/maker_figures.py`. `verify.md` labels the maker exit share an upper bound: the candle lab fills a limit at the touch from synthetic quotes with no queue.
- **AC-18** (demo): after AC-17, whatever the verdicts, r2's two coin version is promoted by hand (`candidate → paper`) and the VPS runs `PAPER_STRATEGY=donchian_scalp_r2` with `PAPER_INSTRUMENTS=PF_PEPEUSD.KRAKEN,PF_XBTUSD.KRAKEN`. This ends r1's demo record; r1's promotion stays, so switching back is one env line. After the deploy, for each coin: no `warmup_seed_failed`, and the first decision rows show `ema` and `midline` with values. After the first week, `verify.md` records, by running `maker_figures.py` on the VPS Postgres rows (`decisions`, `orders`, `fills`) for r2's run: approved entries, entries filled (in full, in part, none), entries rejected (order status `rejected`), the maker exit share and the fallbacks, and the fees paid per round trip next to r1's demo weeks. The fallback `why` split comes from the trader log's `exit_fallback_market` lines (`kfs-compose logs trader`), as far back as the log reaches.

## Decision

**Chosen option**: Option 2: a new maker base module `node/bar_rule_maker.py` beside the frozen `BarRuleStrategy`, with the r2 node on it.

Build r2 as `DonchianScalpR2` on a new `MakerBarRuleStrategy` that copies the frozen base's bar loop and safety paths and adds the maker entry, the maker exit with a market fallback, and partial fills, reusing `DonchianMidlineRule` unchanged. Screen it on BTC and PEPE, then replace r1 on demo.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Rationale

Reasoning, options, and the study evidence: see [rationale.md](rationale.md).

## Feature design

### Maker base (`trading_core/node/bar_rule_maker.py`)

`MakerBarRuleStrategy(KfsStrategy, StaleQuoteBlock)` with `MakerBarRuleConfig(GuardedConfig)`. It starts as a copy of `BarRuleStrategy` (bar loop, ATR, quote and bar subscriptions, stale quote check, stop submit and accept timer, `STOP_NOT_ACCEPTED`, exit retry, `on_position_closed`, timers, recording helpers). It adds the maker parts, adapted from `ScalpStrategy`'s maker entry, entry timeout, partial fill, late fill, and target refusal code. Its module docstring says it is the base for maker variants and is frozen once spec 0026 is Accepted (a change after that goes into a new module; until then demo findings may still change it).

Config fields beyond `BarRuleConfig`'s: `entry_offset_atr: Decimal`, `entry_wait_bars: int`, `exit_wait_bars: int`, `atr_min_bps: Decimal | None`. Subclass hooks: `_pre_entry(bar, atr, signal) -> bool` (return False after recording a refusal; the base calls it after the gate and blocks check and before sizing), used by r2 for the volatility floor and the spread check in that order.

Tracked state: `_entry_id`, `_entry_working` (the entry still rests), `_entry_bars` (bar closes since the entry was submitted), `_entry_retired` (r2 asked to cancel the remainder), `_stop_id`, `_exit_id` and `_exit_kind` (`"limit"` or `"market"`), `_exit_bars` (bar closes since the exit limit was submitted), `_exit_decision`, `_side`, `_stop_px`, `_entry_px`, `bars_held`, `_kill_switch_tags`. The two waits are bar counts checked at the start of `on_bar` (AC-4, AC-8). Timers, all through the base's `_alert` helper so the backtest clock drives them too: `_STALE`, `_STOP_ACCEPT`, `_EXIT_RETRY`, and `maker_exit_cancel` (the 5 s guard of AC-8).

**Spike first (task 2).** Before the order paths are built, one test each in the candle lab engine and the demo sandbox shows: (1) what a post only limit that would cross produces (`OrderRejected` with `due_post_only` set or not, and its `reason`); (2) that a reduce only stop market and a reduce only exit limit for the same full position both stay open together, and that when one fills the other is not filled too. If (2) fails on either venue, stop and amend this spec before building AC-7.

### State transitions

```
FLAT ──approved entry──▶ ENTERING (limit rests, no fill)
ENTERING ──timeout, no fill / post only rejected / refused──▶ FLAT
ENTERING ──first fill──▶ stop submitted ──stop accepted──▶ PROTECTED (entry may still rest; stop follows the quantity)
PROTECTED ──timeout with a part filled──▶ PROTECTED (remainder cancelled)
PROTECTED ──midline_exit or time_stop──▶ EXITING (exit limit at the touch, stop still rests)
EXITING ──limit fills──▶ FLAT (position closed, everything left is cancelled)
EXITING ──wait over / post only rejected / cancelled by venue──▶ EXITING (market for the rest)
any ──stop fills──▶ FLAT
any ──stop fails / stop_through / kill switch──▶ EXITING (market)
```

### r2 node (`trading_core/node/donchian_scalp_r2.py`)

`DonchianScalpR2(MakerBarRuleStrategy)` takes the config, gate, blocks, record, and `seed: Sequence[BarInput] = ()`. `STRATEGY_NAME = "donchian_scalp_r2"`, `BAR_SPEC = "5-MINUTE-LAST"`. Module docstring: demo only, no gate pass, trades only after a manual promote.

- `on_bar`: the same seed join guard as r1 (first live bar ignored, `seed_stale` beyond 2 intervals, bars at or before the seed's last close ignored, the seed's last entry signal consumed). Written in this module; r1's module is not imported for it.
- `_pre_entry`: the volatility floor (AC-13), then the spread check (spec 0025 AC-6, same inputs and the `spread_wide` reason).

### Trader side

- `trader/paper_strategy.py`: `PaperStrategyName` gains `"donchian_scalp_r2"`; constants `R2_ENTRY_OFFSET_ATR = Decimal("0.5")`, `R2_ENTRY_WAIT_BARS = 3`, `R2_EXIT_WAIT_BARS = 3`, `R2_ATR_MIN_BPS = Decimal(10)` next to r1's `spread_max_atr` constant (0.1, reused); `donchian_scalp_r2_config(settings, run_id, instrument_id)`; the r2 branch in `paper_version` and `build_paper_strategy`; the multi coin check accepts r1 or r2; `MakerBarRuleView`.
- `trader/main.py`: the seed fetch and the per coin seed mapping run when `PAPER_STRATEGY` is r1 or r2 (today only r1).
- `trader/warmup.py`: docstring only; the fetch is the same.
- No dashboard change, no new alert rule, no schema change.

### Data model sketch

No schema change. `vol_low` reuses the `decisions` table (`risk_reason` is free text). Maker or taker per fill is already in `fills.liquidity`. The exit fallback is visible as a second order under the same exit decision.

### API surface

No new endpoint or command. `PAPER_STRATEGY` gains `donchian_scalp_r2`. Lab: `lab study studies/donchian_scalp_r2_v2.toml`, `lab study studies/donchian_scalp_r2_pepe_v2.toml`, then `lab screen donchian_scalp_r2_v2` and `lab screen donchian_scalp_r2_pepe_v2`.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| Entry (AC-2) | signal, side, structural stop | `DonchianMidlineRule.entry()` (spec 0025 AC-1) |
| Entry (AC-2) | `close` | the signal bar's close (`BarInput.close`) |
| Entry (AC-2) | ATR | the base's `Atr(14)` at the signal bar |
| Entry (AC-2) | offset | `entry_offset_atr`: `R2_ENTRY_OFFSET_ATR` in `trader/paper_strategy.py`; the study file in the lab |
| Entry (AC-2) | tick | `instrument.price_increment` |
| Entry (AC-3) | stop | `floored_stop(side, structural_stop, mid, atr, atr_floor_mult, tick)`, mid from the cached quote |
| Entry (AC-3) | quantity | `check_entry` with `mid = limit_px`, equity, open notional, taker fee, as the base |
| Entry wait (AC-4) | deadline | `_entry_bars` reaching `entry_wait_bars` (bar closes since submit) |
| Stop (AC-5) | quantity | the position's quantity after each fill |
| Stop through (AC-5) | verdict | the first fill's price and the cached quote (bid for a long, ask for a short) against `stop_px`, through `strategy.scalp.stop_through` |
| Rejected stop (AC-5) | verdict | the cached quote when the rejection arrives, else the next quote, against `stop_px` |
| Exit (AC-7) | reason | the base's time stop check, then `rule.exit` (`midline_exit`) |
| Exit (AC-7) | limit price | cached quote: best ask for a long, best bid for a short, at the decision |
| Exit (AC-7) | quote age | `clock.timestamp_ns() − quote.ts_event` against `stale_quote_s` |
| Exit fallback (AC-8) | deadline | `_exit_bars` reaching `exit_wait_bars`; cancel guard 5 s (code constant, the base's `STOP_ACCEPT_TIMEOUT_NS` value) |
| Figures (AC-17, AC-18) | entry fill share, maker exit share | `apps/lab/studies/maker_figures.py` over `decisions`, `orders`, `fills.liquidity` (bundle in the lab, Postgres on the VPS) |
| Exit fallback (AC-8) | quantity | the position's quantity when the market order is sent |
| Post only (AC-6, AC-8) | why | `OrderRejected.due_post_only` |
| Vol floor (AC-13) | `atr_bps` | `atr / bar.close × 10,000`, decimal |
| Vol floor (AC-13) | `atr_min_bps` | `R2_ATR_MIN_BPS` (10); the study file in the lab |
| Spread (AC-14) | bid, ask, limit | as spec 0025 (cached quote, `spread_max_atr` 0.1 on demo, off in the lab) |
| Seed (AC-14) | candles | the trader's existing per coin fetch (spec 0025 AC-20) |
| Version (AC-16) | config JSON | `_donchian_version_config` pattern plus the r2 fields and `instruments` |
| Screens (AC-17) | figures | each study's `lab screen` JSON and run bundle (`fills.liquidity`, decisions) |
| Demo week (AC-18) | fill and fallback counts | the VPS `orders`, `fills`, `decisions` rows and the trader log codes |

### Key invariants

- A position is never without a resting stop market order, except in the base's existing windows (between the first fill and the stop's acceptance, and while a close is under way).
- A maker order is only ever an entry or a planned exit. The stop, the kill switch close, the stop failure close, `stop_through`, a late fill close, and the exit fallback are market or stop market.
- Every exit order is reduce only, so a limit and a market or stop filling together can never flip the position.
- A resting exit limit always ends within `exit_wait_bars` bars plus 5 s, filled or replaced by market.
- No signal acts while any r2 order other than the stop rests.
- The volatility floor and the spread check can only refuse an entry. They never touch an exit or a stop.
- The seed never makes an order or a decision row.
- A stop rejected with the stop already through never blocks entries; any other stop failure always does. A late `stop_through` decision row has no order of its own.

### Security model

No new secret, no new outbound call (the seed fetch already exists). The strategy is demo only: its config builder refuses outside `TRADING_ENV=demo`, and live never runs it.

### Configuration required

No new environment variable. On the VPS, after the manual promote, change `PAPER_STRATEGY=donchian_scalp_r1` to `PAPER_STRATEGY=donchian_scalp_r2` in `/etc/kfs/kfs.env` (back the file up first). `PAPER_INSTRUMENTS` stays as it is.

### Critical test scenarios

- Entry: a long signal at close 100, ATR 2, offset 0.5, tick 0.5 gives a post only buy limit at 99.0; a short gives a sell at 101.0; a price between ticks rounds away from the market. Verifies **AC-2**.
- Sizing: the quantity equals `check_entry` called with `mid = limit_px`; a limit at or beyond the stop records `bad_signal` with no order. Verifies **AC-3**.
- Wait: no fill in 15 minutes cancels the order, logs `maker_entry_timeout`, records no risk event, and the next signal can enter. Verifies **AC-4**.
- Partial: a 40% fill submits a stop for 40%; a further 30% fill modifies it to 70%; the third bar close cancels the last 30%, a 10% fill arriving before the cancel is confirmed takes the stop to 80%, and the cancel confirmation is not a failure. A refused modify logs `stop_modify_rejected` and the next fill syncs it; a stop that is no longer open sets `STOP_NOT_ACCEPTED` and exits at market; a `TRIGGERED` stop is not modified. A second fill beyond the stop price does not trigger `stop_through`. Verifies **AC-5**.
- Stop through by the quote: a long's maker entry fills at 99.0 above `stop_px` 94.0 while the cached bid is 93.5; no stop is submitted, the position exits at market as `stop_through`, no block is added, and the next approved signal sends an entry. With no cached quote the stop is submitted. Verifies **AC-5**.
- Rejected stop: the stop is rejected while the cached bid is through, which sends one market exit and records `stop_through` with no block; rejected with the cached bid above, entries refuse with `blocked:stop_not_accepted` until the next quote, even after the position has closed, then a through quote clears it with no block and a quote above adds `STOP_NOT_ACCEPTED` and the critical risk event. A rejection while an exit limit rests cancels the limit and its guard and sends one market exit; a rejection during a kill switch close sends no second one; a part filled entry's remainder fills after the rejection and closes as `late_entry_fill`; stopping the strategy with the verdict waiting records the block. A stop not accepted in time still adds the block. Both `stop_through` rows have the same input keys. Verifies **AC-5**.
- Post only entry: a `due_post_only` rejection goes flat with no risk event; a margin rejection, and a post only rejection without the flag, raise the WARNING risk event. Verifies **AC-6**.
- Exit: a midline exit on a long sends a reduce only sell limit at the best ask while the stop still rests; a 30% fill of it shrinks the stop to 70%; the rest filling closes the position and cancels the stop. A quote older than `stale_quote_s` sends a market exit with `why = stale_quote`. Verifies **AC-7**.
- Fallback: on the third bar close the limit is cancelled and a market for the remaining quantity goes out after the cancel is confirmed; no confirmation within 5 s still sends it; a limit that fills during the guard sends nothing (no position); any rejection of the limit sends it at once; closing the position cancels the guard timer. Verifies **AC-8**.
- Stop during exit: the stop fills while the exit limit rests; the position closes, the limit is cancelled, and no market order is sent. Verifies **AC-7**, **AC-8**.
- Market paths: `stop_through`, a stop not accepted, and a late entry fill each send market orders, never limits. Verifies **AC-9**, **AC-10**.
- Flip: a close back above the midline while the exit limit rests changes nothing. A signal while the entry rests is dropped. Verifies **AC-10**.
- Kill switch: with an entry part filled and resting, flatten cancels the remainder (no `maker_entry_timeout` log) and sends one market close; with an exit limit resting, flatten cancels it and sends one market close; during the 5 s cancel guard, flatten sends one market close and the guard sends none; a stop accept timeout during the close does nothing; a second flatten sends nothing more. Verifies **AC-11**.
- Restart: a resumed position with an open exit limit has the limit cancelled and keeps its stop; past the time stop it exits at market with a `time_stop` decision marked `resumed`. Verifies **AC-12**.
- Figures: on a fixture bundle, `maker_figures.py` gives the expected entry fill share (with `vol_low` rows left out) and counts a part filled exit as a fallback. Verifies **AC-17**.
- Floor: `atr_bps` 9.99 records `vol_low` and sends nothing; 10 passes; a paused gate records the pause reason, not `vol_low`; `None` turns it off. Verifies **AC-13**.
- Config: `entry_wait_bars` 0, `entry_offset_atr` −0.1, `atr_min_bps` −1, a wrong `bar_spec` each refuse. Verifies **AC-14**.
- Seed: the r1 seed scenarios of spec 0025 (fed, stale, first bar ignored, last seed signal consumed) pass on r2. Verifies **AC-14**.
- Hashes: the baseline matches after the build, except the new r2 line. Verifies **AC-15**.
- Paper: `paper_version` for r2 holds every setting; two coins with r2 build two strategies with tags `PEPEUSD` and `XBTUSD`; live refuses; the view reports `exiting` while an exit limit rests. Verifies **AC-16**.

## Build plan

Tracer Bullet: the first slice is a thin real thread (maker base with entry and stop, r2 node, trader wiring, laptop demo smoke). The lab screens follow as soon as the order paths are complete, because they give the earliest answer. Demo comes last, whatever the verdicts.

1. [x] Hash baseline: record every registered strategy version's code hash and the module hash list to a scratch file, as spec 0025 task 1 did. Satisfies **AC-15**.
2. [x] Spike, then thin thread. The spike (see *Spike first*) records the post only rejection event and the two reduce only orders on both venues; a failure stops the build for an amendment. Then `MakerBarRuleStrategy` with the bar loop, the maker entry (AC-2, AC-3), the entry wait for the no fill case, a full fill with its stop, and market exits only; `DonchianScalpR2` and its config without the seed; the trader wiring (`PaperStrategyName`, config builder, `paper_version`, `build_paper_strategy`, `MakerBarRuleView`, the multi coin check). Unit tests for each. Laptop smoke on demo with `PAPER_STRATEGY=donchian_scalp_r2`: the trader starts and records a version. Then take the stack down. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-14**, **AC-16**.
3. [x] Order paths: partial fills and the stop following the quantity, the post only entry rejection, the maker exit with its fallback, the late fill close, one order in flight, `flatten`, `resume_position`, with the test scenarios above. Satisfies **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-9**, **AC-10**, **AC-11**, **AC-12**.
4. [x] Filters and seed: the volatility floor and the spread check in `_pre_entry`, the seed join in r2's `on_bar`, the seed fetch running for r2 in `trader/main.py`, with tests. Satisfies **AC-13**, **AC-14**, **AC-16**.
5. [ ] Lab screens. First the AC-5 amendment in `bar_rule_maker.py` (the quote in the first fill check and the rejected stop settled by the quote) with its two test scenarios, then rebuild the lab image (with `GIT_COMMIT`); results from before the amendment are invalid trials, named in `verify.md` with no figures. Then register r2, commit both `_v2` study files (the screens test checks all four files) and `maker_figures.py` (with its fixture test), run `lab study` and `lab screen` for each coin, write the AC-17 table into `verify.md`. Satisfies **AC-17**.
6. [ ] Close out and demo: compare hashes against task 1, merge, deploy (`--no-deps`), get r2's two coin version id (`trader.version` with both settings set), promote it by hand, set `PAPER_STRATEGY=donchian_scalp_r2` in `kfs.env` (backed up first), redeploy the trader, check the first decisions and the card for both coins. Satisfies **AC-15**, **AC-18**.
7. [ ] Demo week: after 7 days, record the AC-18 fill and fallback counts in `verify.md`. Satisfies **AC-18**.

## Migration plan

**Strategy**: strangler. r2 deploys beside the frozen r1, and one env line picks which one trades.
**Phases**:
1. Merge and deploy with `PAPER_STRATEGY=donchian_scalp_r1` still set. No existing hash moves, so r1 keeps running with no promote.
2. Promote r2's two coin version by hand, set `PAPER_STRATEGY=donchian_scalp_r2` (backed up `kfs.env`), and redeploy the trader. Switch while both coins are flat if you can; on demo a restart marks an open position abandoned anyway.

**Rollback**: set `PAPER_STRATEGY=donchian_scalp_r1` again and redeploy. Its promotion still stands.
**Risks**: a deploy with r2 set before the promote exits 4 and rolls back (the known deploy guard). A switch with a position open abandons it on demo.

## Consequences

**Positive**:
- Most round trips pay the maker fee on both sides and no spread, which the study puts at 0.5 to 0.75 ATR saved per trade.
- The trades that lose most (bars small next to the fee) are skipped, and every skip is countable as `vol_low`.
- Safety paths are unchanged: the stop is always a stop market order, and every forced close is market.
- The maker base is reusable for later bar strategies, and r1 stays frozen as a one line rollback.

**Negative / tradeoffs**:
- The study says r2 most likely still loses after fees, even in sample. You may spend a build to confirm a negative.
- Adverse selection: a limit 0.5 ATR back fills most often when the breakout fails and price comes back, and misses the clean runs. The study counts this, but only on candles.
- Fill realism is weak everywhere r2 can be tested. The candle lab fills a limit from synthetic quotes with no queue, and the demo sandbox fills on quotes with no queue either (`node/sandbox.py`). Neither shows a real queue position, so real maker fill rates will be lower than both report. The AC-18 demo week shows how the logic behaves, not the true fill rate.
- About a quarter of signals do not fill, and the floor skips about 40% of BTC signals, so BTC trades far less often and the sample grows slowly.
- The exit limit sits at the passive touch, not at the bar close the study used. Fills there come a little later and a little less often than in the study.
- A resting exit can turn a small win into a stop loss in the 15 minutes before the fallback. The stop bounds it.
- A new base is about as much code again as `BarRuleStrategy`, with a new partial fill path to test.
- Four more trials in the scalping family (two of them invalid) raise the multiple testing bar for later scalping candidates.
- r1's demo record ends at the switch.

**Neutral**:
- `check_entry` sizes from the limit price, so with the stop 2.5 ATR from the fill instead of 3, a full fill is about 20% larger than r1's for the same risk. It can therefore hit the exposure or leverage cap sooner (a refusal, as today). The guard figures in the decision row show the limit price as `entry_px`, while `mid` stays the real mid.
- PEPE's bars are large next to the fee, so the floor rarely skips PEPE.

## Follow-up

- [ ] If the demo week shows the touch exit mostly falling back to market, try an exit limit one tick inside the touch, or a shorter wait, in a later revision.
- [ ] A floor as a multiple of the round trip cost instead of fixed bps, if fees change.
- [ ] Spec 0025's cold start risk (the time stop waits for the rule to be ready) is copied into the maker base with the bar loop. Fix it in the next base revision, not here.
- [ ] Before any live use, measure real maker fill rates with the replay lab's pessimistic queue on recorded book days.
