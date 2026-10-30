# 0025. donchian_scalp_r1: EMA 200 trend filter, midline exit, spread filter

**Date**: 2026-10-30
**Status**: In Progress

## Summary

`donchian_scalp_r1` is a new demo paper strategy that sits beside `donchian_scalp` and leaves it untouched. It keeps the same 5 minute channel breakout and adds three things from the blueprint. It trades only in the direction of the 200 bar EMA (exponential moving average, a smoothed trend line), it exits when a close crosses the middle of the channel, and it skips an entry when the bid/ask spread is wide compared with the ATR (average true range, the typical bar size). So that it can trade right after a deploy instead of waiting about 17 hours for the EMA, it seeds its indicators from Kraken's public 5 minute candles at start. Before it goes on demo, two lab screens (one for the old version, one for r1) show whether the changes help after fees.

## Requirements

**User stories**:
- As the trader, I want a donchian scalp that skips counter trend breakouts and takes profit at the channel middle, so that demo trades are fewer but better.
- As the trader, I want entries skipped when the spread is wide, so that a thin book does not eat the edge.
- As the trader, I want the strategy ready to trade right after a deploy, so that a restart does not cost a day of trades.
- As the trader, I want a lab comparison of the old and new versions, so that I know whether the additions help before I watch demo results.

**Acceptance criteria**:
- **AC-1** (entry): a new pure rule `DonchianMidlineRule` (`trading_core/strategy/donchian_midline.py`) gives a long when the bar's close is above the highest high of the previous `entry_bars` (20) bars **and** strictly above EMA(`ema_period`, 200) of the closes up to and including this bar. It gives a short when the close is below the lowest low of the previous 20 bars **and** strictly below the EMA. A close exactly at the EMA gives no entry. The structural stop is `stop_atr` (3) ATR from the close, as in `DonchianRule`. Every signal is a one bar event, as the `BarRule` contract says.
- **AC-2** (exit): in a position, the rule exits with reason `midline_exit` when a long closes strictly below the midline, or a short closes strictly above it. The midline is `(entry_high + entry_low) / 2` of the previous 20 bars, read before the current bar joins the window, so it trails bar by bar. The 10 bar opposite channel exit (`channel_exit`) is not used. The base's ATR floored stop and the 60 bar time stop are unchanged.
- **AC-3** (readiness): the rule is ready only once the 20 bar channel is full, the base's ATR(14) has a value, and the EMA has a value (200 closes). Before that, nothing is decided, as in the base.
- **AC-4** (node and config): a new node `DonchianScalpR1` (`trading_core/node/donchian_scalp_r1.py`) with `DonchianScalpR1Config`: `bar_spec` must be `5-MINUTE-LAST`, `entry_bars` 20 (at least 2), `stop_atr` 3 (at least 1), `ema_period` 200 (at least 2), `time_stop_bars` 60, `atr_period` 14, `atr_floor_mult` 1, and `spread_max_atr: Decimal | None` (default `None`; when set it must be above 0). A bad value refuses at config build time.
- **AC-5** (frozen modules): no file in the version closure of any existing strategy is edited. The closure is every `trading_core` module a strategy imports plus every package `__init__.py` on the way (`versioning.py`), so it covers `strategy/donchian.py`, `strategy/bar_rule.py`, `strategy/indicators.py`, `strategy/bar_indicators.py`, `node/bar_rule.py`, `node/strategy.py`, the donchian nodes, `recording/*`, `risk/*`, and all `__init__.py` files. New modules are not re-exported from any existing `__init__.py`. The code hash of every existing strategy version is the same before and after this feature (checked against a baseline taken in task 1). `alerts/rules.py` and the trader app sit outside every closure, so editing them is fine.
- **AC-6** (spread filter): when `spread_max_atr` is set, the check runs only after the base's own refusals are clear: the gate allows entries and `blocks.active()` is empty, so a halted, paused, reconciling, or blocked trader records its real reason, never `spread_wide`. The node then reads the cached quote. If `ask - bid` is strictly greater than `spread_max_atr × ATR` (the base's ATR at that bar), no order is sent. A decision row is recorded with intent `enter`, the signal's side and reason, verdict rejected, `risk_reason = "spread_wide"`, and inputs holding the bar inputs plus `structural_stop`, `bid`, `ask`, `spread`, `spread_limit`, `mid`, and `quote_age_ms`. One INFO log line has code `entry_skipped_spread`. No risk event and no alert. A spread equal to the limit passes. With `spread_max_atr = None`, the entry path is exactly the base's.
- **AC-7** (warmup seed): when the trader starts with `PAPER_STRATEGY=donchian_scalp_r1`, before it builds the strategy (and before the node runs) it fetches Kraken production's public 5 minute trade candles for `PF_XBTUSD` and keeps the latest 600 complete ones (a candle whose close time is after the fetch time is dropped). At the first live bar (AC-10) the strategy feeds them, oldest first, into the base's ATR and the rule, with no entry, exit, or decision. With a good seed the rule is ready at the first full live bar, so an entry can happen at that bar's close after a deploy.
- **AC-8** (seed checks): the seed is refused as a whole when the payload is not the expected JSON, `more_candles` is true (one request must hold the whole window), a value is not a decimal, a candle has `high < low` or a close outside `[low, high]`, open times are not strictly increasing, an open time is not on a 5 minute UTC boundary, or fewer than 200 complete candles remain. A missing 5 minute candle inside the seed is allowed (the live bars skip empty intervals too).
- **AC-9** (seed failure is safe): when the fetch fails after its retries (timeout, connection error, HTTP 429 or 5xx), or AC-8 refuses the seed, the trader logs WARNING `warmup_seed_failed` with the reason. It then starts normally without a seed and warms up from live bars (about 17 hours before the first entry). A new alert rule turns that log code into a WARNING phone alert. Positions, stops, and the restart and reconcile flow of spec 0023 work exactly as without the seed. Known risk, from the frozen base: while the rule is not ready, the base skips the time stop and the rule exit, so after a cold start with an open position only the ATR stop protects it until the rule is ready (about 17 hours for r1). This needs both a seed failure and a restart with a position open.
- **AC-10** (seed joins live): the seed is held until the first live bar arrives. That bar is ignored (it covers only part of its interval, because the strategy started mid bar), and so is any live bar whose close time is at or before the seed's last close (equality included). At the first live bar, if its close time is more than 2 intervals (10 minutes) after the seed's last close (so more than one bar is missing between them), the seed is dropped with the same `warmup_seed_failed` warning (reason `seed_stale`) and the strategy warms up from live bars. Otherwise the seed is fed, and the next live bar is the first to decide. With no seed, none of this applies and live bars work exactly as in the base.
- **AC-11** (paper wiring): `PAPER_STRATEGY` accepts `donchian_scalp_r1`. `paper_version` records name `donchian_scalp_r1`, kind `scalping`, and a config with every setting, including `ema_period` 200 and `spread_max_atr` `"0.1"`. Outside demo the config builder refuses, as for the other donchian strategies. The dashboard views (`BarRuleView`) work unchanged.
- **AC-12** (decision inputs): every decision row's inputs carry exactly the rule's four values `ema`, `midline`, `entry_high`, and `entry_low` (each `null` until it exists; `midline` is `null` until both channel bounds exist), next to the base's `bar_close` and `atr`. The inner rule's `exit_high` and `exit_low` are not included.
- **AC-13** (lab screens): the lab registry has `donchian_scalp` and `donchian_scalp_r1`, both with `kind=SCALPING`, `candle_screen=True`, `candle_gate=False`, and `run_fields=_candle_fields` (`lab screen` refuses any other kind). The r1 factory builds `DonchianScalpR1` with an empty seed. Two study files are committed before either runs: `studies/donchian_scalp.toml` and `studies/donchian_scalp_r1.toml`. Each is a candle study (`lab = "candle"`, `seed = 42`, the same header lines as `connors_rsi_15m.toml`) with no `[strategy.grid]` table, so it makes one `base` run. Its `[strategy.config]` spells out `bar_spec = "5-MINUTE-LAST"`, `entry_bars = 20`, `stop_atr = 3`, `atr_period = 14`, `atr_floor_mult = 1`, `time_stop_bars = 60`, plus `ema_period = 200` for r1, with `spread_max_atr` left off. The shared settings follow `connors_rsi_15m.toml` (range 2022-04-01 to 2025-10-18, taker fee 0.0005, maker fee 0.0002, slippage 2 bps, risk table unchanged). The two runs are 2 more trials in the scalping family. Both run through `lab study` and then `lab screen`. This spec's `verify.md` records for each: the screen verdict, trades per day, net P&L, gross P&L per trade, cost per trade, daily Sharpe, max drawdown, and the r1 minus baseline difference of each figure.
- **AC-13a** (real spread check): one DuckDB query on the recorder's catalog gives the distribution (median, 90th, 99th percentile) of `(ask - bid) / ATR(14)` on 5 minute BTC bars over the recorded days, and `verify.md` records it, with the share of bar closes where 0.1 would have skipped. The 0.1 value stays unless the spec is amended.
- **AC-14** (demo): after AC-13, whatever the verdicts, r1's version is promoted by hand (`candidate → paper`) and the VPS runs it with `PAPER_STRATEGY=donchian_scalp_r1`. The promoted version is the demo config (`spread_max_atr` 0.1), whose hash differs from the lab's (filter off), so the screen evidence is for a sibling config, not the exact promoted version. After that deploy, the trader logs no `warmup_seed_failed` and the first decision rows show `ema` and `midline` with values.

## Decision

**Chosen option**: Option 1: a new `donchian_scalp_r1` variant beside `donchian_scalp` (new pure rule plus new node), with a seeded warmup and the spread check in the node.

Build r1 as its own rule and node modules on the frozen `BarRuleStrategy` base, judge it in two candle screens against the unchanged `donchian_scalp`, then run it on demo.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

### Pure rule (`trading_core/strategy/donchian_midline.py`)

`DonchianMidlineRule(entry_bars, stop_atr, ema_period)` implements the `BarRule` protocol by composition. It holds a `DonchianRule` for the channels and the breakout, and an `Ema` from `trading_core.strategy.indicators` (unchanged, imported) fed with every close.

- `update(bar, atr)`: feeds the inner `DonchianRule` (which reads its channels before the bar joins), then the EMA with `bar.close`, then computes `midline = (entry_high + entry_low) / 2` from the inner rule's public `entry_high` and `entry_low`.
- `ready()`: `inner.ready() and ema.value is not None`.
- `entry()`: the inner rule's signal, kept only when its side agrees with the EMA (AC-1). Reasons stay `donchian_long` and `donchian_short`, so the journal groups them the same way.
- `exit(trade)`: `midline_exit` per AC-2; the inner `exit` is never called.
- `consumed()` and `on_flat()`: nothing to clear (level conditions).
- `values()`: exactly `entry_high`, `entry_low` (the inner rule's public attributes), `midline`, `ema` (AC-12). Never the inner `values()`.

### Node (`trading_core/node/donchian_scalp_r1.py`)

`DonchianScalpR1(BarRuleStrategy)` takes the config, gate, blocks, record, and `seed: Sequence[BarInput] = ()` (empty means no seed; the default lets the lab's four argument factory build it). Its module docstring says it is demo only, with no gate pass.

- `on_start`: just `super().on_start()`. The seed waits for the first live bar.
- `on_bar` (override, a guard in front of the base): while the seed is pending, the first live bar runs the AC-10 join check, then either feeds the seed through the base's `self._atr.update(...)` and `self.rule.update(...)` (the same two calls the base's `on_bar` makes, nothing else) or drops it with the warning. That first bar is then ignored. After a fed seed, a bar at or before the seed's last close is ignored too. An ignored bar returns before any ATR or rule update and leaves `_last_bar` untouched. Right after the seed is fed, an entry signal left by the last seed candle is consumed (`rule.consumed()` when `rule.entry()` is not `None`), so the seed never trades. Every other bar goes to `super().on_bar(bar)`. With an empty seed the guard is off.
- `_decide_entry` (override): the base does not check the gate or blocks before it reads the quote; it passes them into `check_entry`. So r1 first looks at `self._gate.halted`, `self._gate.paused`, and `self._blocks.active()`. If any is set, it calls `super()._decide_entry(...)` so `check_entry` records the real reason. Otherwise, with `spread_max_atr` set and a quote in the cache, the AC-6 check runs. A wide spread records the refused decision and returns:
  `self._record_decision(uuid.uuid7(), Intent.ENTER, side, signal.reason, inputs, False, "spread_wide", self.clock.timestamp_ns())`, where `side` comes from the base module's `_order_side(signal.side)` and `inputs` is the bar inputs plus `structural_stop`, `bid`, `ask`, `spread`, `spread_limit`, `mid`, and `quote_age_ms` (all decimal text, the age an int). In every other case (narrow spread, no quote, filter off) it calls `super()._decide_entry(...)`, which already handles a missing quote.

Using the base's single underscore members (and its module private `_order_side`) from a subclass is deliberate: the base is frozen, so r1 must not edit it, and these members are stable because the base cannot change.

### Warmup seed

- **Pure checks** (`trading_core/strategy/warmup.py`, no I/O): `seed_bars(candles, interval_ns, now_ns, keep) -> list[BarInput]` turns raw candles (open time in ms plus decimal text) into `BarInput`s with `ts_ns = open + interval` (the close time, as live bars use). It drops incomplete candles, keeps the latest `keep`, and raises a typed `SeedRefusedError(reason)` for every AC-8 case.
- **Fetch** (`apps/trader/src/trader/warmup.py`, standard library HTTP only, no credentials): one `GET https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/5m?from=<s>&to=<s>`, with `from` 620 bars (600 plus a margin of 20) before now and `to` now, in seconds. About 620 candles fit in one response, so there is no paging; `more_candles: true` refuses the seed (AC-8). The body is `{"candles": [{"time", "open", "high", "low", "close", "volume"}]}`, where `time` is the candle's open time in ms and OHLC arrive as strings, parsed as `Decimal`. A 5 s timeout per try, at most 3 tries: a timeout, connection error, 429, or 5xx is retried after 1 s, then 2 s. Anything else fails at once. Worst case it adds about 20 s to a start, well under the trader's compose healthcheck `start_period` of 90 s (`x-node-healthcheck` in `compose.yaml`).
- **Where it runs**: `trader/main.py`, in `main()` before `run_node`, only when `PAPER_STRATEGY=donchian_scalp_r1`. It runs before the node exists, never on the node's event loop. The result goes to `run_node` and on to `build_paper_strategy`, which gains a `seed: Sequence[BarInput] = ()` parameter and passes it to `DonchianScalpR1` only. `PaperStrategyName` (the `Literal` in `trader/paper_strategy.py`) gains `"donchian_scalp_r1"`. Any failure becomes an empty seed plus the AC-9 warning. It never raises out of startup.
- **Alert**: a new log rule in `trading_core/alerts/rules.py` with id `warmup_seed_failed`, Tier WARNING, matching the trader's WARNING log line with that code (the line carries a `reason` field). The key is the code alone, so a crash loop of restarts makes one alert, not many. No `min_open_s`: one failure is worth knowing. The rule table's coverage test (spec 0013, AC-19) gains the new rule.

The endpoint is a code constant, not a setting. Market data always comes from Kraken production (Kraken shut its futures demo on 14 July 2026, see `node/kraken.py`), so there is nothing to configure.

### Data model sketch

No schema change. The spread skip reuses the `decisions` table (`risk_reason` is free text). Nothing new is stored for the seed; it lives only in memory.

### State transitions

Unchanged from `BarRuleStrategy` (`FLAT → ENTERING → PROTECTED → EXITING → FLAT`). The seed adds a step before the first bar: `start → seeded (ready) | cold (warming up)`.

### API surface

No new command or endpoint. Configuration: `PAPER_STRATEGY` gains the value `donchian_scalp_r1`. Lab: `lab study studies/donchian_scalp.toml`, `lab study studies/donchian_scalp_r1.toml`, then `lab screen donchian_scalp` and `lab screen donchian_scalp_r1`.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| Entry (AC-1) | channel high and low | inner `DonchianRule`, previous 20 bars |
| Entry (AC-1) | EMA | `Ema(200)` of closes, seeded then live |
| Entry stop | structural stop | `close ± 3 × ATR`, then the base's floor |
| Exit (AC-2) | midline | `(entry_high + entry_low) / 2`, same windows |
| Spread check (AC-6) | bid, ask | `cache.quote_tick(instrument)` at the bar close |
| Spread check (AC-6) | ATR | the base's ATR(14) passed to `_decide_entry` |
| Spread check (AC-6) | limit multiplier | `spread_max_atr`: `Decimal("0.1")` constant in `trader/paper_strategy.py`; `None` in the lab |
| Seed (AC-7) | candles | Kraken charts API, `trade` tick type, `5m` |
| Seed (AC-7) | `now_ns` for completeness | the trader's wall clock (UTC) at fetch |
| Seed join (AC-10) | live time | the first live bar's `ts_event` (its close time) |
| Seed (AC-7) | count 600, minimum 200 | constants in `trader/warmup.py` (3 × EMA period, so the EMA has settled; the minimum is the EMA period). They sit outside every version hash, so changing them does not change r1's version |
| Spread check (AC-13a) | real spread to ATR | the recorder's catalog quotes and trades, via DuckDB |
| Version (AC-11) | config JSON | `_donchian_version_config` pattern plus `ema_period`, `spread_max_atr` |
| Screens (AC-13) | figures | each study's `lab screen` JSON and run report |

### Key invariants

- The rule never reads a bar's own high or low in its channel or midline (no look ahead), as with `DonchianRule`.
- The seed never causes an order or a decision row. Only live bars decide.
- A seed problem can only delay entries. It never stops the trader or touches a position.
- The spread check can only refuse an entry. Exits and stops are never filtered.
- At most one entry or exit per bar close (the base's rule) still holds.

### Security model

No new secret. The seed fetch is an unauthenticated GET to Kraken's public charts API, and no key or header carrying one is sent. The strategy stays demo only: the config builder refuses outside `TRADING_ENV=demo` (AC-11), and live never runs it.

### Configuration required

No new environment variable. On the VPS, set `PAPER_STRATEGY=donchian_scalp_r1` in `/etc/kfs/kfs.env` by hand, after the manual promote (AC-14).

### Critical test scenarios

- Rule: a breakout above the channel with the close above the EMA gives a long; the same breakout with the close below the EMA gives nothing; a close exactly at the EMA gives nothing. Verifies **AC-1**.
- Rule: a long exits on the first close below the trailing midline and not before; a short mirrors it; `channel_exit` never appears. Verifies **AC-2**.
- Rule: not ready at 199 closes, ready at 200 with a full channel and ATR. Verifies **AC-3**.
- Config: `ema_period` 1, `spread_max_atr` 0 or negative, and a wrong `bar_spec` each refuse. Verifies **AC-4**.
- Hashes: the baseline hash file matches after the build. Verifies **AC-5**.
- Spread: a spread of `0.1 × ATR + one tick` records `spread_wide` with `structural_stop` in its inputs and sends no order; exactly `0.1 × ATR` enters; `None` matches the base's decisions bar for bar; a paused gate with a wide spread records the pause reason, not `spread_wide`. Verifies **AC-6**.
- Seed: 600 good candles make the rule ready after the first (ignored) live bar; a feed of 650 keeps the latest 600; an in progress candle is dropped; an entry signal on the last seed candle never becomes a decision. Verifies **AC-7**.
- Seed checks: each AC-8 case (including `more_candles: true`) raises `SeedRefusedError` with its reason; a single missing candle passes. Verifies **AC-8**.
- Seed failure: a fetch that times out every try leaves an empty seed, logs `warmup_seed_failed`, the alert rule matches it, and the trader starts. Verifies **AC-9**.
- Join: a first live bar closing 10 minutes after the seed's last close feeds the seed; 15 minutes drops it as `seed_stale`; the first live bar itself never updates the ATR or rule; a later bar with the same close time as the seed's last is ignored. Verifies **AC-10**.
- Paper: `paper_version` for `donchian_scalp_r1` holds every setting, and a live env refuses. Verifies **AC-11**.
- Inputs: a decision row carries `ema` and `midline`. Verifies **AC-12**.

## Build plan

Tracer Bullet: the first two tasks give a thin real thread (rule to node to trader on the laptop demo). The lab screens come right after, because they need only the rule and give the earliest answer on the fee hurdle. Each later task thickens one segment end to end. Demo (task 6) happens whatever the screen verdicts.

1. Hash baseline: record the code hash of every registered strategy version to a scratch file, as specs 0021 and 0022 did. Satisfies **AC-5**.
2. Thin thread: `DonchianMidlineRule` with its unit tests, `DonchianScalpR1` and its config (no seed, no spread check yet; `seed` already defaults to `()`), the `PaperStrategyName` value, `paper_version`, and `build_paper_strategy` wiring. Laptop smoke: stack up with `PAPER_STRATEGY=donchian_scalp_r1`, the trader starts and records a version. Then take the stack down. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-11**, **AC-12**.
3. Lab screens: register both strategies in `lab/strategies.py` (AC-13 kinds and flags), commit both study files, run `lab study` and `lab screen` for each, and write the AC-13 table into `verify.md`. Run the AC-13a spread query and record it too. Satisfies **AC-13**, **AC-13a**.
4. Spread filter: the `_decide_entry` override, `spread_max_atr` 0.1 in the paper config, and tests. Satisfies **AC-6**.
5. Warmup seed: `trading_core/strategy/warmup.py` (pure checks and tests), `trader/warmup.py` (fetch, retries), the `main()` call and the `seed` parameter through `run_node` and `build_paper_strategy`, the node's first bar join and feed, and the `warmup_seed_failed` alert rule with its coverage test. Laptop smoke: a start shows the rule ready after the first live bar. Then take the stack down. Satisfies **AC-7**, **AC-8**, **AC-9**, **AC-10**.
6. Close out and demo: compare hashes against task 1, merge, deploy (`--no-deps`), promote r1 by hand, set `PAPER_STRATEGY` on the VPS, redeploy, and check the first decisions. Satisfies **AC-5**, **AC-14**.

## Migration plan

**Strategy**: strangler (r1 runs beside the unchanged `donchian_scalp`; one env line picks which one trades on demo).
**Phases**:
1. Merge and deploy with `PAPER_STRATEGY=donchian_scalp` still set. Nothing changes on the VPS, and `donchian_scalp`'s version hash is the same, so no promote is needed.
2. Get r1's version id (`trader/version.py` with `PAPER_STRATEGY=donchian_scalp_r1`), run `gate promote --manual --step candidate-paper --version <id>`, set `PAPER_STRATEGY=donchian_scalp_r1` in `/etc/kfs/kfs.env` (back it up first), and redeploy.
**Rollback**: set `PAPER_STRATEGY=donchian_scalp` again and redeploy. Its promotion still stands.
**Risks**: a deploy with r1 set before the promote exits 4 and rolls back (the known deploy guard). The trader is flat at the switch only if no `donchian_scalp` position is open, so switch while flat, or let spec 0023's restart flow take the position back under r1's exits.

## Consequences

**Positive**:
- `donchian_scalp` stays runnable and frozen, so you can switch back with one env line.
- The lab gets its first measurement of `donchian_scalp` itself, which went to demo without one.
- A deploy no longer costs a day of r1 trades.
- Every spread skip is countable in the decisions and the journal.

**Negative / tradeoffs**:
- The lab cannot judge the spread filter (its spread is synthetic), so that part is judged on demo only. The lab and demo versions of r1 have different config hashes.
- The trader gains its first outbound HTTP call outside Nautilus, plus up to about 20 s on a start when Kraken is slow.
- After a cold start (seed failed) with an open position, only the ATR stop protects it for about 17 hours, because the frozen base skips the time stop and the rule exit until the rule is ready (AC-9).
- The first live bar after every start is thrown away, so the first r1 decision comes about 5 to 10 minutes after a deploy.
- r1 subclasses the frozen base's underscore members (`_decide_entry`, `_atr`, `_record_decision`). A future `bar_rule_v2` would need its own r1 variant.
- Two more trials join the scalping family, which raises the multiple testing bar (family N) for later scalping candidates.
- The midline exit cuts winners earlier. On 5 minute bars that can push gross P&L per trade below the round trip cost (see the premise note in `rationale.md`).

**Neutral**:
- The seed candles come from Kraken's own aggregation and the live bars from ours. Small differences in a bar's high or low are possible. Closes, which the EMA uses, should match.
- The first live bar after a start is partial. r1 ignores it (AC-10); `donchian_scalp` still uses it.

## Follow-up

- [ ] A split exit (half at 1.5R, the rest trailed) from the same blueprint, as a later revision if r1 shows promise on demo.
- [ ] Whether the seed should also serve `donchian_scalp` and `donchian_4h` (their warmup is 1.75 hours and 3.5 days). That needs their own new modules, because they are frozen.
- [ ] After a few weeks of demo, count `spread_wide` skips and compare with the AC-13a distribution. If there are none, the filter is not doing anything, and 0.1 ATR may be too loose for BTC's one tick spread; amend the value here.
- [ ] A `bar_rule_v2` base could run the time stop while the rule warms up, which closes the cold start risk in AC-9.
