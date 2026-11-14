# 0029. six_vote_trend: a six vote confluence strategy on 1 hour bars, demo forward test

**Date**: 2026-11-06 (amended 2026-11-06: AC-12 lets the demo run with the loss limits and cooldown off, at your request, since r2 had hit the daily loss limit that day and you are busy testing)
**Status**: Accepted

## Summary

`six_vote_trend` trades a coin only when six independent signals all point the same way on the 1 hour chart: the coin's own trend, BTC's trend, the 9/21 EMA, RSI momentum, a 20 bar breakout, and a volume spike. It enters with a maker order and falls back to market, trails a 3 ATR stop on every price tick (a setting can turn the trail off), and leaves after about 24 hours at the latest. It replaces r2 on the VPS demo on the same 12 coins as soon as it is built and promoted, with no lab screen and no gate, because you chose a live forward test over more backtests.

Be honest about the odds. In the 2026-11-06 backtests this exact entry made +70 and +33 bps a trade on the development year, then lost 12 bps a trade on the fresh year (see `rationale.md`). The tick trail and the maker first entry are new and untested. This is a demo experiment, not a candidate for live money.

## Requirements

**User stories**:
- As the trader, I want to enter only when all six votes agree, so that I trade rarely and only on strong setups.
- As the trader, I want a cheap maker entry that never misses the trade, so that I save fees when I can without losing the signal.
- As the trader, I want a stop that trails the best price on every tick and a 24 hour limit, so that winners are protected and no trade lingers.
- As the operator, I want it to replace r2 on demo now, on the same coins and risk settings, so that I see how it behaves live.

**Acceptance criteria**:
- **AC-1** (the rule): a new pure module `trading_core/strategy/six_vote.py` with `SixVoteRule`, satisfying the `BarRule` protocol (`trading_core.strategy.bar_rule`), fed one 1 hour bar close at a time. On each update it computes six votes, each +1, −1, or 0, from the bar and its own indicators:
  - V1 coin trend: sign(close − EMA200)
  - V2 BTC trend: the sign last set by `set_btc_trend` (AC-3); 0 until set
  - V3 9/21: sign(EMA9 − EMA21) when sign(close − EMA9) is the same, else 0
  - V4 RSI: +1 when RSI14 (Wilder) > 55, −1 when < 45, else 0
  - V5 breakout: +1 when close > the highest high of the previous 20 bars, −1 when close < the lowest low of the previous 20 bars, else 0
  - V6 volume spike: +1 when volume > 3 × the mean volume of the previous 50 bars, the bar's range > 1.5 × ATR, and the close is in the top quarter of the bar; −1 for the same with the close in the bottom quarter; else 0

  Definitions pinned to the backtest (scratchpad `confluence.py`): EMAs are `ewm(span=n, adjust=False)` (seeded with the first close); RSI14 is `ewm(alpha=1/14, adjust=False)` of up and down moves, `100 − 100 / (1 + up/down)`, with down 0 giving 100; the ATR is the base's Wilder ATR(14) including the current bar (what `rule.update` receives); the 20 bar high and low and the 50 bar volume mean exclude the current bar; "top quarter" is `(close − low) / range ≥ 0.75`, "bottom quarter" `≤ 0.25`, and a bar with zero range votes 0 on V6. `score` is the sum. `entry()` returns a long signal when `score` reaches 6 at this bar and was below 6 at the bar before, a short when it reaches −6 from above −6, else `None`. The signal's stop is `close − 3 × ATR` for a long and `close + 3 × ATR` for a short, its reason `six_vote`. `exit()` always returns `None` (the time stop and the stop do the exiting). `ready()` is true once at least 200 bars were fed (EMA200), at least 51 (the 50 bar volume mean plus the current bar), at least 21 (the 20 bar window), at least 15 (RSI14), and the ATR has a value. `values()` returns `score`, the six votes (`v_trend`, `v_btc`, `v_ema`, `v_rsi`, `v_breakout`, `v_volume`), `ema9`, `ema21`, `ema200`, `rsi14`. It does no I/O and imports no Nautilus.
- **AC-2** (the node): a new node `trading_core/node/six_vote_trend.py` with `SixVoteTrend(MakerBarRuleStrategy)` and `SixVoteTrendConfig(MakerBarRuleConfig)`, `STRATEGY_NAME = "six_vote_trend"`, `BAR_SPEC = "1-HOUR-LAST"` (bars built from trades, as r2's 5 minute bars). Config: `time_stop_bars` 24, `atr_period` 14, `atr_floor_mult` 1, `entry_offset_atr` 0 (the limit rests at the signal close), `entry_wait_bars` 1, `exit_wait_bars` 1, `atr_min_bps` `None` (no volatility floor), `spread_max_atr` 0.1 (r2's spread check, same `spread_wide` reason), `trail_atr: Decimal | None` 3 (`None` turns the trail off: a fixed 3 ATR stop, the backtested behavior), `trail_min_step_atr` 0.1, `trail_min_interval_s` 5 (per coin), `trail_max_refusals` 5, `btc_instrument_id` (`PF_XBTUSD.KRAKEN`), `btc_wait_s` 3. A bad value (a wrong `bar_spec`, `trail_atr` ≤ 0 when set, `trail_min_step_atr` < 0, `trail_min_interval_s` < 1, `trail_max_refusals` < 1) refuses at config build time. The inherited base settings (`stale_quote_s`, `stale_check_s`, `use_market_context`, and the rest) take r2's values. No existing module is edited (AC-11).
- **AC-3** (BTC vote): for every coin other than BTC, the node also subscribes to `BarType.from_str(f"{btc_instrument_id}-1-HOUR-LAST-INTERNAL")` (the same internal aggregation form the base uses for its own bar type) and keeps its own BTC EMA200 with its own readiness: ready after at least 200 BTC closes fed (seed included); before that V2 is 0. It keeps a map from BTC bar close time to `sign(btc_close − btc_ema200)`. A coin bar uses the BTC sign of the **same** close time. If that BTC bar has not arrived yet (the two bars share a timestamp and arrive in any order), the coin bar is held for up to `btc_wait_s` (3 s) and fed when the BTC bar arrives; after the wait it is fed with the last known BTC sign and an INFO log `btc_bar_late`. The BTC strategy instance uses its own bars for V2 (no second subscription). The config builder refuses a coin list that does not contain `btc_instrument_id` (BTC must be loaded and its trades fed), with a clear message.
- **AC-4** (maker first entry): an approved entry is a post only GTC limit at the signal close (rounded to the tick away from the market, the base's `entry_limit_px` with offset 0), sized by the base (`check_entry` with the limit as `mid`, the taker fee). After one bar close (`entry_wait_bars` 1) the base cancels the remainder. The fallback is built by overriding `_canceled` and `_refused`, handling the entry cases **before** the base (whose entry cancel branch resets the trade state), and never calling `super()` for a case it handles:
  - **When it fires.** Only for a cancel of the limit (`client_order_id == _entry_id`) that the node asked for (`_entry_cancel_asked` true), with the limit not in `_retired`, no exit decided (`_exit_decision` and `_exit_id` both `None`), and the phase not `EXITING`. A cancel from `flatten`, `_retire_entry`, `_decide_exit`, `on_position_closed`, or a restart goes to the base unchanged. A post only rejection of the limit (`due_post_only` set, the limit's id) fires it too, with `why = post_only_rejected`.
  - **Check again before sending.** The market goes out up to an hour after the decision, so it repeats the base's checks: gate not halted or paused, `_entry_blocks()` empty, no pending rejected stop verdict, a cached quote no older than `stale_quote_s`, the mid not at or beyond `_stop_px` (the signal's floored stop), then `check_entry` at the current mid with the same stop. Any refusal goes flat with INFO `entry_fallback_skipped` and the `why` (`halted`, `paused`, `blocked:<name>`, `stale_quote`, `stop_through`, or the risk reason); a risk refusal also gets the base's WARNING risk event. No new decision row.
  - **What it sends.** A GTC market order, side from `_side`, quantity `min(limit.leaves_qty, verdict quantity)` (the limit's unfilled rest; the approved quantity is read from the cached limit order, since `_decide_entry` keeps it only in a local), tags from the same entry decision. INFO `entry_fallback_market` with `why` = `timeout` (nothing filled), `remainder` (part filled), or `post_only_rejected`. The phase stays `ENTERING` (or `PROTECTED` with a part filled), `_entry_cancel_asked` stays true (so the base's entry wait never cancels the market order), and the node tracks the market as `_fallback_id`. Nothing filled: `_entry_id` becomes the market's id, so its fills run the base's entry fill path (first fill submits the stop, the `stop_through` check applies; the stop's parent tag then names the market order). Part filled: the stop already rests and follows the quantity through `_sync_stop`.
  - **Entry price.** After any fill of the fallback order, `_entry_px` is set from the position's `avg_px_open` (the base computes it from the limit's fills only).
  - **Fallback failures.** A rejection or denial of the fallback market goes to the base's path (`order_rejected` risk event, then flat when nothing is held). A cancel or expiry of the fallback market logs WARNING `entry_fallback_lost` and goes flat when nothing is held. Any other rejection of the limit keeps the base's behavior.

  The trades per day cap counts the position once. Risk is sized again at the fallback price, so realised risk never exceeds the risk limit, but the entry can be far from the signal close in a fast hour (disclosed in Consequences).
- **AC-5** (trailing stop, off when `trail_atr` is `None`): while a position is open, every quote tick updates the best price since entry (the highest bid for a long, the lowest ask for a short; the first fill price starts it). The trail level is `best − trail_atr × ATR` for a long (rounded down to the tick) and `best + trail_atr × ATR` for a short (rounded up), ATR being the base's value at the last bar close; while the ATR has no value (after a restart, until the first bar close) the trail does nothing. The node calls `modify_order(stop, trigger_price=<level>)` only when all hold:
  - the stop order exists with status `ACCEPTED`, is not in flight, not pending update, not pending cancel, and not `TRIGGERED`;
  - no trail modify is pending (`_trail_pending`, set when sent, cleared by `on_order_updated` or `on_order_modify_rejected` for the stop);
  - the level is tighter than the stop's **current** `trigger_price` (read from the order, not `_stop_px`) by at least max(`trail_min_step_atr` × ATR, one tick);
  - at least `trail_min_interval_s` seconds passed since this coin's last trail modify;
  - no market close is under way (`_closing_at_market`, kill switch), and the trail is not disabled for this trade.

  The stop never loosens. On `on_order_updated` for the stop, `_stop_px` is set to the new trigger (it feeds the base's exit step and `stop_through` checks) and INFO `stop_trailed` logs the old and new trigger. A refused trail modify logs INFO `stop_trail_rejected` with the reason (the base's own `stop_modify_rejected` log still appears, since `OrderModifyRejected` does not say what was modified); after `trail_max_refusals` (5) refusals in a row the trail is disabled for this trade with WARNING `stop_trail_disabled`, and the resting stop stays where it is. The base's quantity follow (`_sync_stop`) keeps working and already skips while a modify is pending. Task 3 starts with a laptop demo spike confirming the Kraken adapter accepts a `trigger_price` modify on a stop market (and whether it needs the quantity too); if it does not, stop and come back to `/architect` (the `None` switch keeps the strategy runnable meanwhile).
- **AC-6** (time exit): at the 24th bar close counted by the base's `bars_held` (which counts from the entry, so a fallback entry can hold up to about 25 hours) the base's time stop decides `time_stop`: a post only reduce only limit at the passive touch for one bar close (`exit_wait_bars` 1), then market for the rest (the base's fallback, AC-8 of spec 0026). The trailing stop keeps resting until the position is flat.
- **AC-7** (decisions): every entry decision row (approved or refused) carries the bar inputs (`bar_close`, `atr`) and every value of AC-1's `values()`, so the dashboard and SQL show why a trade was taken. Refusals by the spread check record `spread_wide` as r2 does.
- **AC-8** (warmup seed): `trader/warmup.py` gains interval, keep, and minimum parameters (`interval` "5m" or "1h", `keep_bars`, `min_bars`), with r2's values (5m, 600, 200) as the defaults so r2 is unchanged. For `six_vote_trend` the trader fetches the last 300 closed 1 hour candles (minimum 250, else `warmup_seed_failed`) per coin from `/api/charts/v1/trade/<symbol>/1h`, with the same retry and failure rules (spec 0025 AC-7 to AC-10). BTC's seed is fetched once and passed to every instance as `btc_seed` (a new `build_paper_strategy` argument beside `seed`). `six_vote_trend` joins `SEEDED_STRATEGIES` and the multi coin list. The node feeds its coin seed through the ATR and the rule with r2's join guard (first live bar ignored as partial, `seed_stale` beyond two 1 hour intervals), and the BTC seed through its BTC EMA with the same staleness rule at the first live BTC bar (a stale BTC seed is dropped with `seed_stale`, so V2 stays 0 until 200 live BTC bars). The seed never records a decision or trades. The candle between the seed's last close and the first live bar is not refetched (it skews the windows slightly), so the first decision comes at the second live bar close, up to 2 hours after a start. Task 2 checks that the charts API's `volume` and the trade built bar's volume are in the same unit (both in coins), and if not, converts the seed's volume before it is fed.
- **AC-8a** (per bar values log): at every coin bar close once the rule is ready, the node logs INFO `six_vote_values` with the coin, the bar close time, and every value of `values()`. This is the start check and the way to see a silent V2.
- **AC-9** (restart): `resume_position` takes back a position under its resting stop as the base does (`bars_held` from the open time, past 24 bars exits at once at market as `time_stop` with `resumed = true`). The trail resumes from the resting stop's trigger: the best price restarts at the current quote, it waits until the ATR has a value (the first live bar close), and the stop is only moved if the new trail level is tighter. Open limits are cancelled by the base's resume (a resting market order is not expected). A part filled entry that survives a restart stays at its filled size; no fallback is sent (`_entry_cancel_asked` is false after a restart). An entry limit with no position after a restart is left to the trader's startup reconcile, as for r2.
- **AC-10** (paper wiring): `PAPER_STRATEGY` accepts `six_vote_trend`, more than one coin is allowed, each coin gets its own instance with its own `order_id_tag` and the per coin blocks wrapper (`trader/coin_blocks.py`), as r2. `paper_version` records name `six_vote_trend`, kind `hour_level`, and a config holding every setting of AC-2 plus `instruments` (sorted `PAPER_INSTRUMENTS`). Outside demo the config builder refuses. The Overview card uses r2's `MakerBarRuleView` unchanged. No schema change, no new risk setting.
- **AC-11** (frozen modules): no file in the version closure of any existing strategy is edited (`bar_rule_maker.py` and `donchian_scalp_r2.py` included). A hash baseline is taken first; at close out every existing strategy's version hash is unchanged and the module hash list matches the baseline except the new lines for `strategy/six_vote.py` and `node/six_vote_trend.py`. If a needed change can only go into the maker base, stop and come back to `/architect`. The node reads private base state (`_entry_cancel_asked`, `_entry_id`, `_retired`, `_exit_decision`, `_exit_id`, `_stop_id`, `_stop_px`, `_atr`, `_side`); that coupling is safe only while the base stays frozen, and the node's module docstring says so.
- **AC-12** (demo switch, now): after merge and deploy (`--no-deps`, with `PAPER_STRATEGY=donchian_scalp_r2` still set), you promote the new version by hand (`candidate → paper`), then `kfs.env` is backed up and `PAPER_STRATEGY=six_vote_trend` is set, with `PAPER_INSTRUMENTS` unchanged (the 12 coins), and the trader is recreated. **Loss limits** (amended 2026-11-06): for the demo forward test the loss limits and the cooldown are off: `RISK_DAILY_LOSS_LIMIT_USD=1000000`, `RISK_TOTAL_LOSS_LIMIT_USD=1000000`, `RISK_LOSS_COOLDOWN_S=0`. Every other `RISK_*` line stays unchanged. `verify.md` names the backup (`/etc/kfs/kfs.env.bak-20261106-sixvote`) and the old values. Before the forward test is judged, or before anything moves toward live, the three lines go back to the backup's values (a new version, promoted by hand again). Any halt row deleted to start the switch is saved to a file under `/etc/kfs/` first and named in `verify.md`. This ends r2's demo week early; `verify.md` notes r2's last run id and its end time (UTC and Amsterdam). Start checks for each coin: no `warmup_seed_failed` or `seed_stale`, a `six_vote_values` log line (AC-8a) by the second live bar close with `score` and all six votes, V2 not 0 (BTC ready), and the coin shows on the Overview. There is no fixed review date; the forward test runs until you decide.

## Decision

**Chosen option**: Option 2: a new strategy module on r2's maker base, overriding the entry fallback and adding the tick trail in its own module, with a pure rule in `strategy/`.

Build `SixVoteTrend(MakerBarRuleStrategy)` with `SixVoteRule`, a 1 hour seed, and the trader wiring, and switch the VPS demo to it as soon as it is promoted. No lab screen, no gate.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Rationale

Reasoning, options, and the backtest evidence: see [rationale.md](rationale.md).

## Feature design

### Rule (`trading_core/strategy/six_vote.py`)

`SixVoteRule` holds EMA9, EMA21, EMA200, RSI14 (Wilder), rolling windows of the last 20 highs and lows and the last 50 volumes, the previous score, the BTC trend sign, and the pending signal. It reuses the pure indicator helpers in `strategy/indicators.py` or `strategy/bar_indicators.py` where one matches the backtest definition exactly (imported unchanged); otherwise it carries its own small helper. `set_btc_trend(sign: int) -> None` is the one method beyond `BarRule`. `consumed()` clears the pending signal; `on_flat()` does nothing (the score crossing rule already needs a fresh cross).

### Node (`trading_core/node/six_vote_trend.py`)

- `on_start`: the base's subscriptions plus BTC's 1 hour bars (non BTC coins), then the seeds (AC-8).
- `on_bar`: a BTC bar updates the BTC EMA and returns. A coin bar runs the seed join guard (as r2, written in this module), sets the BTC trend on the rule (AC-3), then calls the base's `on_bar`.
- `_pre_entry`: the spread check (r2's logic, rewritten here).
- `_canceled` and `_refused` overrides: the entry fallback (AC-4) for the entry order only; every other order goes to the base unchanged.
- `on_quote_tick`: the base's handling, then the trail (AC-5).
- `on_order_modify_rejected`: the base's handling, plus `stop_trail_rejected` for a price modify.
- `resume_position`: the base's, then the trail resume (AC-9).

### State transitions

```
FLAT ──approved entry──▶ ENTERING (maker limit at the close)
ENTERING ──filled in full──▶ PROTECTED (stop at close ± 3 ATR)
ENTERING ──one bar, no fill / post only rejected──▶ ENTERING (market entry, same decision)
ENTERING ──one bar, part filled──▶ PROTECTED (market for the rest, stop follows)
ENTERING ──market fallback skipped (mid beyond stop) / refused──▶ FLAT
PROTECTED ──quote tick, trail tighter by the step, 5 s since last──▶ PROTECTED (stop trigger moved)
PROTECTED ──24th bar close──▶ EXITING (maker limit at the touch, one bar, then market)
any ──stop fills──▶ FLAT
any ──stop fails / stop_through / kill switch──▶ EXITING (market)
```

### Data model sketch

No schema change. Votes and score live in `decisions.inputs` (JSON). Maker or taker per fill is already in `fills.liquidity`. Trail moves are visible as `order_events` modify rows and the `stop_trailed` log.

### API surface

No new endpoint or command. `PAPER_STRATEGY` gains `six_vote_trend`.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| Votes V1, V3 to V6 (AC-1) | indicators | the coin's 1 hour bars (`BarInput`, volume included) and the base's ATR(14) |
| Vote V2 (AC-3) | BTC trend | BTC 1 hour bars subscribed by the node, EMA200 kept in the node; the last BTC close at or before the coin bar's close |
| Entry (AC-4) | limit price, quantity, stop | `entry_limit_px` with offset 0; `check_entry` with `mid = limit_px`; the rule's stop floored by `floored_stop` at `atr_floor_mult` |
| Fallback (AC-4) | quantity | the approved quantity minus the filled quantity of the limit |
| Fallback check (AC-4) | mid | the cached quote at the cancel confirmation |
| Trail (AC-5) | best price | quote ticks: the highest bid (long) or lowest ask (short) since the first fill |
| Trail (AC-5) | ATR | the base's ATR at the last bar close |
| Trail (AC-5) | step, interval, refusals, switch | `trail_min_step_atr` 0.1, `trail_min_interval_s` 5, `trail_max_refusals` 5, `trail_atr` 3 or `None` (config constants in `trader/paper_strategy.py`) |
| Trail (AC-5) | current stop level | the stop order's `trigger_price` from the cache |
| Fallback (AC-4) | approved quantity, side, stop, decision | the cached limit order (`quantity`, `leaves_qty`), `_side`, `_stop_px`, `entry_decision` |
| Fallback (AC-4) | sized quantity | `check_entry` at the current mid with the same stop |
| BTC vote (AC-3) | same hour BTC sign | the node's map of BTC bar close time to sign; `btc_wait_s` 3 |
| Seed (AC-8) | interval, keep, minimum | `warmup_seed` parameters: 1h, 300, 250 for this strategy; 5m, 600, 200 default |
| Values log (AC-8a) | votes and score | `rule.values()` at each bar close |
| Trail (AC-5) | tick | `instrument.price_increment` |
| Time exit (AC-6) | bar count | the base's `bars_held` against `time_stop_bars` 24 |
| Seed (AC-8) | candles | Kraken charts API `1h`, last 300 closed candles per coin plus BTC |
| Version (AC-10) | config JSON | r2's version config pattern with AC-2's fields and `instruments` |
| Demo switch (AC-12) | coin list, risk | the VPS `kfs.env` as it is today (12 coins, `RISK_MAX_TRADES_PER_DAY=100`, daily loss limit as set) |

### Key invariants

- A position is never without a resting stop market order, except the base's existing windows.
- The trailing stop only ever tightens; it never moves while a market close is under way.
- At most one stop modify is in flight at a time.
- No entry fires unless all six votes agree, and only on the bar where the score reaches ±6.
- The entry fallback can only enter in the signal's direction, for at most the approved quantity, and never when the mid is already beyond the stop.
- The seed never makes an order or a decision row.

### Security model

No new secret. The only new outbound call is the 1 hour candle fetch to the same public charts API the 5 minute seed uses. Demo only: the config builder refuses outside `TRADING_ENV=demo`.

### Configuration required

No new environment variable. On the VPS, after the promote: back up `/etc/kfs/kfs.env`, then set `PAPER_STRATEGY=six_vote_trend`. Rollback is one line back to `donchian_scalp_r2` (its promotion stays).

### Critical test scenarios

- Votes: a fixture where all six agree long on bar N and five agree on bar N−1 gives one long signal at N with stop `close − 3 ATR`; the same score on N+1 gives none; 6 → 5 → 6 gives a second signal. RSI exactly 55 votes 0. Verifies **AC-1**.
- BTC vote: a coin bar at 10:00 uses BTC's 10:00 close, not 11:00; no BTC bar yet means no signal. Verifies **AC-3**.
- Fallback: no fill after one bar sends one market order for the full quantity under the same decision tag and logs `entry_fallback_market`, and the base's entry wait never cancels it; a 40% fill sends market for 60% and the stop ends at 100% with `_entry_px` the position's average; a mid beyond the stop, a paused gate, a stale quote, or a `check_entry` refusal at the cancel each send nothing and go flat with `entry_fallback_skipped`; a kill switch cancel of the limit sends no fallback; a rejected fallback market raises the risk event. Verifies **AC-4**.
- Trail: a long with entry 100, ATR 2, stop 94; bids 101, 101.1, 103 move the stop to 95 (step 0.2 met) and then 97 once 5 s passed, and `_stop_px` follows each accepted update; a bid of 99 changes nothing; a pending modify blocks the next; five refusals in a row disable the trail with `stop_trail_disabled`; during a kill switch close nothing moves; with `trail_atr = None` the stop never moves. Verifies **AC-5**.
- BTC timing: the coin's 10:00 bar arriving before BTC's 10:00 bar waits and then uses BTC's 10:00 sign; with no BTC bar within 3 s it uses the last sign and logs `btc_bar_late`; a coin list without BTC refuses at config build. Verifies **AC-3**.
- Time exit: the 24th bar close sends a maker exit; one bar later the rest goes at market. Verifies **AC-6**.
- Decision inputs: an approved entry row holds `score` 6 and the six votes. Verifies **AC-7**.
- Seed: a seed of 300 bars makes the rule ready on the first live bar; a stale seed is dropped (`seed_stale`); a failed fetch logs `warmup_seed_failed`. Verifies **AC-8**.
- Restart: a resumed long with stop 95 does not trail before the first bar close (no ATR); after it, with bid 104 and ATR 2, the stop moves to 98; with bid 96 nothing moves. Verifies **AC-9**.
- Values log: every ready bar close logs `six_vote_values` with the score and six votes. Verifies **AC-8a**.
- Paper: `paper_version` holds every setting; 12 coins build 12 instances; live refuses. Verifies **AC-10**.
- Hashes: baseline matches except the two new lines. Verifies **AC-11**.

## Build plan

Tracer Bullet: a thin thread from the rule to a laptop demo trader first, then the order paths, then the VPS switch.

1. Hash baseline: record every registered strategy version's code hash and the module hash list to a scratch file. Satisfies **AC-11**.
2. Thin thread: `SixVoteRule` with its unit tests (AC-1 fixtures); `SixVoteTrend` with the BTC vote, market fallback skipped for now (maker entry and base timeout only); the trader wiring (`PaperStrategyName`, config builder, `paper_version`, `build_paper_strategy`, multi coin check, `MakerBarRuleView`); the 1 hour seed fetch in `trader/warmup.py` (interval parameters) and `trader/main.py` with `btc_seed`; the seed volume unit check; the `six_vote_values` log. Laptop smoke on demo with `PAPER_STRATEGY=six_vote_trend` on BTC plus one coin: starts, records a version, `six_vote_values` lines with V2 set by the second bar. Take the stack down. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-7**, **AC-8**, **AC-8a**, **AC-10**.
3. Order paths: first the laptop demo spike for a stop `trigger_price` modify (stop and return to `/architect` if Kraken refuses it); then the entry fallback (AC-4), the trailing stop with its switch (AC-5), the time exit check (AC-6), and `resume_position` (AC-9), each with the test scenarios above. Satisfies **AC-4**, **AC-5**, **AC-6**, **AC-9**.
4. Close out and switch: compare hashes with task 1, merge, deploy with `--no-deps`, get the version id (`trader.version` with `PAPER_STRATEGY=six_vote_trend`), you promote it by hand, back up `kfs.env`, set `PAPER_STRATEGY=six_vote_trend`, recreate the trader, run the start checks for every coin, note r2's last run in `verify.md`. Satisfies **AC-11**, **AC-12**.

## Consequences

**Positive**:
- About 1 trade per coin every two weeks, so fees are a small share of each move (holds up to a day, ATR stops of 1% to 3%).
- Reuses r2's maker base, safety paths, seed pattern, card, and risk limits; no schema or dashboard change.
- Every decision row explains itself (six votes and the score).

**Negative / tradeoffs**:
- It failed the fresh year in backtest (−12 bps a trade); the forward test may simply confirm that.
- Few trades: weeks of demo give only dozens of trades, so any result stays noisy for a long time.
- The tick trail differs from what was backtested (a fixed stop with a 24 bar exit); in noisy markets a 3 ATR trail can exit earlier than the backtest did. Its effect is untested.
- r2's demo week ends early; the 2026-11-12 review has only about three days of r2 on 12 coins.
- The market fallback pays the taker fee on most entries a fast market leaves behind, and can enter up to an hour after the signal, far from its close, with the stop still at the signal's level (risk is sized again at that price, so the loss limit holds, but the trade is worse).
- After a start or restart, the first decision waits for the second live bar (up to 2 hours), and the trail waits for the first.
- The node depends on private state of the frozen maker base; if that base is ever replaced, this node must be checked again.
- The loss limits and cooldown are off on demo (AC-12, amended): a bad day is not stopped, so a day's loss can run far past the old 50 USD limit. The per order, exposure, leverage, and trades per day caps still hold. This is fine on demo money only.

**Neutral**:
- Two more modules enter the hash list; r2 and every other version stay as they are.

## Follow-up

- [ ] Optional: register `six_vote_trend` in the candle lab later, to compare the demo fills with a backtest of the same code.
- [ ] Decide when to judge the forward test (no fixed point was set); a count of 60 closed trades is a reasonable minimum before reading anything into the result.
- [ ] Before judging the forward test, put the three loss limit lines back from `/etc/kfs/kfs.env.bak-20261106-sixvote` and promote the new version (AC-12).
- [ ] Add a scope row (feature 30) for this strategy with `/scope`.
