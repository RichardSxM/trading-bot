# 0025. donchian_scalp_r1: EMA 200 trend filter, midline exit, spread filter

**Date**: 2026-10-30
**Amended**: 2026-10-30, two coins: one trader runs r1 on BTC and PEPE (AC-15 to AC-25; AC-5, AC-7, AC-11, AC-14 amended)
**Status**: In Progress

## Summary

`donchian_scalp_r1` is a new demo paper strategy that sits beside `donchian_scalp` and leaves it untouched. It keeps the same 5 minute channel breakout and adds three things from the blueprint. It trades only in the direction of the 200 bar EMA (exponential moving average, a smoothed trend line), it exits when a close crosses the middle of the channel, and it skips an entry when the bid/ask spread is wide compared with the ATR (average true range, the typical bar size). So that it can trade right after a deploy instead of waiting about 17 hours for the EMA, it seeds its indicators from Kraken's public 5 minute candles at start. Before it goes on demo, two lab screens (one for the old version, one for r1) show whether the changes help after fees.

The amendment adds a second coin. One trader runs r1 on BTC and on PEPE at the same time, on one shared paper account, with one strategy instance per coin. A new `PAPER_INSTRUMENTS` setting picks the coins, and the coin list is part of r1's version, so one manual promote covers both. The account wide limits (exposure, daily and total loss, trades per day) and the entry blocks stay shared, so a problem on either coin stops new entries on both. The dashboard shows a position row per coin, the recorder starts recording PEPE, and a PEPE lab screen (after a fix to the lab's off tick check) gives a baseline to compare the demo against.

## Requirements

**User stories**:
- As the trader, I want a donchian scalp that skips counter trend breakouts and takes profit at the channel middle, so that demo trades are fewer but better.
- As the trader, I want entries skipped when the spread is wide, so that a thin book does not eat the edge.
- As the trader, I want the strategy ready to trade right after a deploy, so that a restart does not cost a day of trades.
- As the trader, I want a lab comparison of the old and new versions, so that I know whether the additions help before I watch demo results.
- As the trader, I want r1 to also trade PEPE in the same trader, so that I see how the same rule does on a more volatile coin without running a second stack.

**Acceptance criteria**:
- **AC-1** (entry): a new pure rule `DonchianMidlineRule` (`trading_core/strategy/donchian_midline.py`) gives a long when the bar's close is above the highest high of the previous `entry_bars` (20) bars **and** strictly above EMA(`ema_period`, 200) of the closes up to and including this bar. It gives a short when the close is below the lowest low of the previous 20 bars **and** strictly below the EMA. A close exactly at the EMA gives no entry. The structural stop is `stop_atr` (3) ATR from the close, as in `DonchianRule`. Every signal is a one bar event, as the `BarRule` contract says.
- **AC-2** (exit): in a position, the rule exits with reason `midline_exit` when a long closes strictly below the midline, or a short closes strictly above it. The midline is `(entry_high + entry_low) / 2` of the previous 20 bars, read before the current bar joins the window, so it trails bar by bar. The 10 bar opposite channel exit (`channel_exit`) is not used. The base's ATR floored stop and the 60 bar time stop are unchanged.
- **AC-3** (readiness): the rule is ready only once the 20 bar channel is full, the base's ATR(14) has a value, and the EMA has a value (200 closes). Before that, nothing is decided, as in the base.
- **AC-4** (node and config): a new node `DonchianScalpR1` (`trading_core/node/donchian_scalp_r1.py`) with `DonchianScalpR1Config`: `bar_spec` must be `5-MINUTE-LAST`, `entry_bars` 20 (at least 2), `stop_atr` 3 (at least 1), `ema_period` 200 (at least 2), `time_stop_bars` 60, `atr_period` 14, `atr_floor_mult` 1, and `spread_max_atr: Decimal | None` (default `None`; when set it must be above 0). A bad value refuses at config build time.
- **AC-5** (frozen modules): no file in the version closure of any existing strategy is edited. The closure is every `trading_core` module a strategy imports plus every package `__init__.py` on the way (`versioning.py`), so it covers `strategy/donchian.py`, `strategy/bar_rule.py`, `strategy/indicators.py`, `strategy/bar_indicators.py`, `node/bar_rule.py`, `node/strategy.py`, the donchian nodes, `recording/*`, `risk/*`, and all `__init__.py` files. New modules are not re-exported from any existing `__init__.py`. The code hash of every existing strategy version is the same before and after this feature (checked against a baseline taken in task 1). `alerts/rules.py` and the trader app sit outside every closure, so editing them is fine. *Amended for two coins:* the trader side modules `node/recording.py` (AC-18), `node/paper_snapshots.py` (AC-21), `node/context_feed.py` if its list needs it (AC-20), and the lab only `lab/candles.py` (AC-23) may change, but only after the build confirms each is in no strategy's version closure (the hash tool prints each strategy's closure). The close out check is then: every existing strategy's version hash is unchanged, and the module hash list matches the baseline line for line except the new `donchian_scalp_r1` line and the lines of those named modules.
- **AC-6** (spread filter): when `spread_max_atr` is set, the check runs only after the base's own refusals are clear: the gate allows entries and `blocks.active()` is empty, so a halted, paused, reconciling, or blocked trader records its real reason, never `spread_wide`. The node then reads the cached quote. If `ask - bid` is strictly greater than `spread_max_atr × ATR` (the base's ATR at that bar), no order is sent. A decision row is recorded with intent `enter`, the signal's side and reason, verdict rejected, `risk_reason = "spread_wide"`, and inputs holding the bar inputs plus `structural_stop`, `bid`, `ask`, `spread`, `spread_limit`, `mid`, and `quote_age_ms`. One INFO log line has code `entry_skipped_spread`. No risk event and no alert. A spread equal to the limit passes. With `spread_max_atr = None`, the entry path is exactly the base's.
- **AC-7** (warmup seed): when the trader starts with `PAPER_STRATEGY=donchian_scalp_r1`, before it builds the strategy (and before the node runs) it fetches Kraken production's public 5 minute trade candles for `PF_XBTUSD` and keeps the latest 600 complete ones (a candle whose close time is after the fetch time is dropped). At the first live bar (AC-10) the strategy feeds them, oldest first, into the base's ATR and the rule, with no entry, exit, or decision. With a good seed the rule is ready at the first full live bar, so an entry can happen at that bar's close after a deploy. *Amended:* with more than one coin, this happens once per coin in `PAPER_INSTRUMENTS` (AC-20), with that coin's symbol in place of `PF_XBTUSD`.
- **AC-8** (seed checks): the seed is refused as a whole when the payload is not the expected JSON, `more_candles` is true (one request must hold the whole window), a value is not a decimal, a candle has `high < low` or a close outside `[low, high]`, open times are not strictly increasing, an open time is not on a 5 minute UTC boundary, or fewer than 200 complete candles remain. A missing 5 minute candle inside the seed is allowed (the live bars skip empty intervals too).
- **AC-9** (seed failure is safe): when the fetch fails after its retries (timeout, connection error, HTTP 429 or 5xx), or AC-8 refuses the seed, the trader logs WARNING `warmup_seed_failed` with the reason. It then starts normally without a seed and warms up from live bars (about 17 hours before the first entry). A new alert rule turns that log code into a WARNING phone alert. Positions, stops, and the restart and reconcile flow of spec 0023 work exactly as without the seed. Known risk, from the frozen base: while the rule is not ready, the base skips the time stop and the rule exit, so after a cold start with an open position only the ATR stop protects it until the rule is ready (about 17 hours for r1). This needs both a seed failure and a restart with a position open.
- **AC-10** (seed joins live): the seed is held until the first live bar arrives. That bar is ignored (it covers only part of its interval, because the strategy started mid bar), and so is any live bar whose close time is at or before the seed's last close (equality included). At the first live bar, if its close time is more than 2 intervals (10 minutes) after the seed's last close (so more than one bar is missing between them), the seed is dropped with the same `warmup_seed_failed` warning (reason `seed_stale`) and the strategy warms up from live bars. Otherwise the seed is fed, and the next live bar is the first to decide. With no seed, none of this applies and live bars work exactly as in the base.
- **AC-11** (paper wiring): `PAPER_STRATEGY` accepts `donchian_scalp_r1`. `paper_version` records name `donchian_scalp_r1`, kind `scalping`, and a config with every setting, including `ema_period` 200, `spread_max_atr` `"0.1"`, and (amended) `instruments` (AC-17). Outside demo the config builder refuses, as for the other donchian strategies. The dashboard views (`BarRuleView`) work unchanged.
- **AC-12** (decision inputs): every decision row's inputs carry exactly the rule's four values `ema`, `midline`, `entry_high`, and `entry_low` (each `null` until it exists; `midline` is `null` until both channel bounds exist), next to the base's `bar_close` and `atr`. The inner rule's `exit_high` and `exit_low` are not included.
- **AC-13** (lab screens): the lab registry has `donchian_scalp` and `donchian_scalp_r1`, both with `kind=SCALPING`, `candle_screen=True`, `candle_gate=False`, and `run_fields=_candle_fields` (`lab screen` refuses any other kind). The r1 factory builds `DonchianScalpR1` with an empty seed. Two study files are committed before either runs: `studies/donchian_scalp.toml` and `studies/donchian_scalp_r1.toml`. Each is a candle study (`lab = "candle"`, `seed = 42`, the same header lines as `connors_rsi_15m.toml`) with no `[strategy.grid]` table, so it makes one `base` run. Its `[strategy.config]` spells out `bar_spec = "5-MINUTE-LAST"`, `entry_bars = 20`, `stop_atr = 3`, `atr_period = 14`, `atr_floor_mult = 1`, `time_stop_bars = 60`, plus `ema_period = 200` for r1, with `spread_max_atr` left off. The shared settings follow `connors_rsi_15m.toml` (range 2022-04-01 to 2025-10-18, taker fee 0.0005, maker fee 0.0002, slippage 2 bps, risk table unchanged). The two runs are 2 more trials in the scalping family. Both run through `lab study` and then `lab screen`. This spec's `verify.md` records for each: the screen verdict, trades per day, net P&L, gross P&L per trade, cost per trade, daily Sharpe, max drawdown, and the r1 minus baseline difference of each figure.
- **AC-13a** (real spread check): one DuckDB query on the recorder's catalog gives the distribution (median, 90th, 99th percentile) of `(ask - bid) / ATR(14)` on 5 minute BTC bars over the recorded days, and `verify.md` records it, with the share of bar closes where 0.1 would have skipped. The 0.1 value stays unless the spec is amended.
- **AC-14** (demo): after AC-13, whatever the verdicts, r1's version is promoted by hand (`candidate → paper`) and the VPS runs it with `PAPER_STRATEGY=donchian_scalp_r1`. The promoted version is the demo config (`spread_max_atr` 0.1), whose hash differs from the lab's (filter off), so the screen evidence is for a sibling config, not the exact promoted version. After that deploy, the trader logs no `warmup_seed_failed` and the first decision rows show `ema` and `midline` with values. *Amended:* the demo runs both coins from one deploy: `PAPER_INSTRUMENTS=PF_PEPEUSD.KRAKEN,PF_XBTUSD.KRAKEN` is set together with `PAPER_STRATEGY`, and the promoted version is that two coin config (AC-17). The checks above hold for each coin: no `warmup_seed_failed` for either, the first decision rows of both coins show `ema` and `midline`, and the Overview card shows a row for each coin (AC-21). The PEPE screen (AC-24) runs before this deploy, but like AC-13 its verdict does not block it.
- **AC-15** (coin setting): a new setting `PAPER_INSTRUMENTS` in `StrategySettings`, a comma list of Kraken instrument ids, default `PF_XBTUSD.KRAKEN`, so nothing changes until it is set. The trader refuses to start when an entry does not match `PF_<SYMBOL>.KRAKEN`, the list is empty, or it holds a duplicate. More than one coin is allowed only with `PAPER_STRATEGY=donchian_scalp_r1`; any other strategy with more than one coin refuses to start. The list is sorted wherever it is used, so the order you write it in changes nothing. After the data client loads its instruments, and before any strategy is built, the trader also refuses to start, naming the missing id, when a coin in the list is not in the instrument cache (for example `PF_PEPEUSD` missing from the demo venue).
- **AC-16** (one strategy per coin): the trader builds one `DonchianScalpR1` per coin in the one node. All share the run id, the pause gate, the entry blocks, and the record writer. Each has its own `instrument_id`, its own seed (AC-20), and its own `order_id_tag`: the symbol without `PF_` (`XBTUSD`, `PEPEUSD`), so the two Nautilus strategy ids differ. The tag is a Nautilus `StrategyConfig` field that `donchian_scalp_r1_config` sets; it is not part of r1's version config, so one version covers every coin (AC-17). The kill switch already attaches every loaded strategy, so a halt and its flatten cover every coin. Other strategies keep their default tag and stay single coin.
- **AC-17** (one version for the coin set): r1's version config gains `"instruments"`, the sorted list of `PAPER_INSTRUMENTS` (`["PF_XBTUSD.KRAKEN"]` for one coin). So one manual `candidate → paper` promote covers the whole set, and a different set is a different version that needs its own promote. No other strategy's version config changes.
- **AC-18** (one recording actor): `RecordingActor` takes the list of coins in place of one. At start it writes an instrument row for each coin (before any order row), and it subscribes to and books funding per coin, with the funding rate kept per coin. The funding boundary and its timer stay one for all coins: at each hourly boundary one pass books every coin's open positions with that coin's rate, and raises at most one funding alert for the pass. The snapshot timer loops over every coin. The liquidation estimate skips a coin that has no mid yet, so PEPE without a quote never hides BTC's estimate. Order, position, and account events are still recorded once each, because they are subscribed by topic, not by coin. With one coin, the rows it writes are the same as today.
- **AC-19** (risk across coins): each coin sizes with the unchanged frozen sizing (`risk_per_trade` 0.5% of the account's equity). The Nautilus risk engine's `max_notional_per_order` gets an entry for every coin at `RISK_MAX_ORDER_NOTIONAL_USD`, so no coin trades without a per order cap. The account wide limits stay shared and unchanged: total exposure 500 USD, daily loss 20 USD, total loss 100 USD, and 50 trades per day across both coins. The entry blocks stay shared: any block (a stale quote, a recording failure, a permanent `stop_not_accepted`) stops new entries on every coin, while stops and exits keep working. Because each strategy runs its own frozen stale check, which adds and clears `STALE_FEED` for its own coin only, each strategy gets a per coin blocks wrapper (trader side, `trader/coin_blocks.py`) with the same `add`, `clear`, `active`, and `since` as `EntryBlocks`. It passes every reason straight to the shared blocks, except `STALE_FEED`: for that one it keeps a stale flag per coin, adds the shared block when the first coin goes stale, and clears it only when no coin is stale. So a fresh BTC never clears a stale PEPE's block. Exposure is first come: if BTC fills the 500 USD cap, PEPE's entry refuses, and that is accepted.
- **AC-20** (context and seed per coin): the context feed subscribes every coin in `PAPER_INSTRUMENTS`, so each coin's funding rate is known. The seed (AC-7 to AC-10) is fetched per coin from `https://futures.kraken.com/api/charts/v1/trade/<SYMBOL>/5m`, one coin after another, before the node runs. The `warmup_seed_failed` line gains an `instrument` field, and a failure warms up only that coin from live bars. The AC-10 join runs per coin at that coin's first live bar. With 3 tries of 5 s per coin, two coins add at most about 40 s to a start, still under the 90 s `start_period`.
- **AC-21** (snapshots and dashboard): the trader writes a new snapshot `positions` (`{env}:snapshot:positions`) whose data is `{"coins": [...]}`: one entry per coin in sorted order, each holding the fields `position_snapshot` writes today plus `base` (the coin's base currency from the instrument, `BTC` or `PEPE`), with two changes: `avg_entry_px` is rounded to the smaller of 0.01 and the coin's tick (BTC stays at cents, exactly as today; PEPE keeps its digits), and a coin with no mid yet gets an entry `{"instrument_id", "base", "side": "unknown"}` instead of being left out, so it never blanks the other coins. It no longer writes the `position` snapshot. The `account` snapshot stays one, account wide: it takes the coin list, and its unrealized P&L sums every open position, each at its own coin's mid. In `trader_state`, `exiting` is true when any coin is exiting, and `position_opened_ns` is the earliest open position's (no reader uses it today besides the snapshot itself). The Overview position card shows one row per coin, each with the same open, flat, bad, and lost states as today plus `unknown` (shown as waiting for a quote), and each row shows its size in its own `base`. `pnl` and `equity` stay at card level. The Wanted, Trader, and Kraken view (`sides`) is built per row; on demo it stays as today, with no Kraken side. The `POSITION_EVENT` partial renders the whole list. A missing `positions` snapshot, or one older than the heartbeat's red limit, shows the whole card `offline`.
- **AC-22** (recorder): `RECORDER_INSTRUMENTS` on the VPS gains `PF_PEPEUSD.KRAKEN`. That is a setting change only, so the recorder and the compactor record PEPE's book and quotes like the other coins, under the same disk floors and alerts.
- **AC-23** (off tick guard): in `trading_core/lab/candles.py`, the price noise bound becomes per instrument: the smaller of `1e-8` and one hundredth of the coin's tick. For any tick of `1e-6` or more it stays `1e-8`, so no BTC or ETH candle changes (a test rebuilds a BTC month and gets the same rows). For PEPE (tick `1e-10`) it is `1e-12`, and a test shows a PEPE price half a tick off is detected as off tick. Before the PEPE screen, the lab's stored PEPE 1 minute months are checked again under the new bound, and `verify.md` records how many candles were off tick.
- **AC-24** (PEPE screen): `studies/donchian_scalp_r1_pepe.toml` is committed before it runs. It is `donchian_scalp_r1.toml` with three changes: `instrument_id = "PF_PEPEUSD.KRAKEN"`, `from = 2023-06-01` (the first whole month after PF_PEPEUSD opened), and `slippage_bps = "5"` for the thinner book. It is one more trial in the scalping family. It runs through `lab study` and then `lab screen donchian_scalp_r1_pepe`, and `verify.md` records the AC-13 figures for it, next to r1 on BTC. Its verdict does not block the demo.
- **AC-25** (PEPE spread check): after 7 full days of recorded PEPE quotes (AC-22), the AC-13a query runs for PEPE, and `verify.md` records its distribution and the share of bar closes where 0.1 would have skipped. If 0.1 would skip more than half of PEPE's bar closes, `spread_max_atr` becomes a value per coin by amending this spec. Until that amendment ships, the demo keeps running with 0.1 for both coins (so PEPE may mostly skip), and the dashboard shows nothing different. This does not block the demo, but the feature is not done until it is recorded.

## Decision

**Chosen option**: Option 1: a new `donchian_scalp_r1` variant beside `donchian_scalp` (new pure rule plus new node), with a seeded warmup and the spread check in the node.

Build r1 as its own rule and node modules on the frozen `BarRuleStrategy` base, judge it in two candle screens against the unchanged `donchian_scalp`, then run it on demo.

*Amendment:* run r1 on BTC and PEPE in the one trader, one strategy instance per coin on a shared paper account, with the coin list in r1's version. The trader side code (settings, wiring, recording actor, snapshots, dashboard) learns about a list of coins. The frozen strategy code does not change.

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

### Two coins (amendment)

What already works for two coins, so it stays as is: the Kraken data client loads every instrument (`load_all=True`), the sandbox sets its fees on every instrument it loads, the kill switch attaches every loaded `KfsStrategy`, the loss limit actor and the guard's exposure and equity sum every open position, and decision, order, fill, and position rows already carry `instrument_id`. On demo the reconcile executor is not built (live only), and the drill stays BTC only.

What changes, all outside the strategy closures:

- **Settings** (`trading_core/settings`): `paper_instruments: str = "PF_XBTUSD.KRAKEN"` plus a parsed, sorted `instrument_list` with the AC-15 checks, and a check across fields that refuses more than one coin unless `paper_strategy == "donchian_scalp_r1"`. Compose passes `PAPER_INSTRUMENTS: ${PAPER_INSTRUMENTS:-PF_XBTUSD.KRAKEN}` to the trader and the gate's version tool, so the default means no `kfs.env` line is needed for a deploy.
- **Wiring** (`trader/main.py`): the `BTC_PERP` uses on the paper path become the coin list. `build_paper_strategy` is called once per coin and returns one strategy and one view per coin. The r1 config builder takes the `order_id_tag` (AC-16). `max_notional_per_order` gets one entry per coin (AC-19). `ContextFeedConfig.instrument_ids` is the coin list (AC-20). `seed` becomes a mapping from coin to its candles. The instrument cache check (AC-15) runs before the strategies are built. The live path (reconcile executor, drill) keeps `BTC_PERP`.
- **Blocks per coin** (`trader/coin_blocks.py`, new): the `STALE_FEED` aware wrapper over the shared `EntryBlocks` (AC-19). Each strategy gets its own wrapper, and all of them share one `EntryBlocks`. The frozen `stale.py` is unchanged; it simply calls the wrapper.
- **Version** (`trader/paper_strategy.py`): `paper_version` adds `"instruments": settings.instrument_list` to r1's config only (AC-17). `python -m trader.version` therefore needs `PAPER_INSTRUMENTS` set the same way as the trader to print the id you promote.
- **Seed** (`trader/warmup.py`): `CHARTS_URL` becomes a format string with the symbol, and the fetch runs once per coin (AC-20). The pure checks in `trading_core/strategy/warmup.py` are unchanged.
- **Recording actor** (`node/recording.py`): `instrument_ids: Sequence[InstrumentId]`. Instrument rows at start for each coin. `on_funding_rate` keeps its rates per coin. One funding boundary and timer for all coins; each pass books every coin's open positions with that coin's rate and raises at most one alert. The snapshot timer loops over the coins. `_liquidation_px` skips a coin with no mid. The funding unbooked key already holds the coin.
- **Snapshots** (`node/paper_snapshots.py`): `positions_snapshot(cache, coins_and_views)` returns `{"coins": [...]}` with one entry per coin (an `unknown` entry for a coin with no mid), each with `base`. `_cents` becomes a rounding to `min(0.01, tick)`. `account_snapshot` takes the coin list and sums the unrealized P&L of every open position at its own coin's mid (as the guard's `unrealized(cache)` does). `paper_status` takes the list of views and folds `exiting` (any) and `position_opened_ns` (earliest).
- **Dashboard** (`dashboard/live/position.py`, `web/app.py`, the `overview_position.html` partial): read `positions` and build one row view per entry with the existing per position logic, plus the `unknown` state, the `base` unit in place of the hardcoded `BTC`, and `sides` per row (`dashboard/live/sides.py`, `poller.py`). `pnl`, `equity`, and the `LOST` fallback stay at card level. The card's offline check applies to the snapshot as a whole. The dashboard still never imports Nautilus.
- **Lab** (`lab/candles.py`): the per instrument noise bound (AC-23), taken from the pinned instrument's tick that the candle builder already reads.

**Lifecycle on two coins.** Each coin's strategy runs its own `FLAT → ENTERING → PROTECTED → EXITING → FLAT` on its own instrument. They meet only at the shared account, the shared blocks, and the shared limits. One can hold a position while the other is flat. Each one's first live bar join (AC-10) happens on its own.

### Data model sketch

No schema change. The spread skip reuses the `decisions` table (`risk_reason` is free text). Nothing new is stored for the seed; it lives only in memory.

### State transitions

Unchanged from `BarRuleStrategy` (`FLAT → ENTERING → PROTECTED → EXITING → FLAT`). The seed adds a step before the first bar: `start → seeded (ready) | cold (warming up)`.

### API surface

No new command or endpoint. Configuration: `PAPER_STRATEGY` gains the value `donchian_scalp_r1`. Lab: `lab study studies/donchian_scalp.toml`, `lab study studies/donchian_scalp_r1.toml`, then `lab screen donchian_scalp` and `lab screen donchian_scalp_r1`. *Amended:* `PAPER_INSTRUMENTS` (AC-15); the Redis snapshot `positions` replaces `position` (AC-21); lab: `lab study studies/donchian_scalp_r1_pepe.toml`, then `lab screen donchian_scalp_r1_pepe` (AC-24). Still no schema change: every row that needs a coin already has `instrument_id`.

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
| Coins (AC-15) | which coins trade | `PAPER_INSTRUMENTS`, sorted, default `PF_XBTUSD.KRAKEN` |
| Strategy id (AC-16) | `order_id_tag` | the coin's symbol without `PF_`, from the instrument id |
| Version (AC-17) | `instruments` | `settings.instrument_list` |
| Per order cap (AC-19) | notional per coin | `RISK_MAX_ORDER_NOTIONAL_USD`, the same for every coin |
| Seed (AC-20) | candle URL | `CHARTS_URL` with the instrument id's symbol |
| Funding (AC-18) | rate per coin | `FundingRateUpdate.instrument_id` of each update |
| Snapshot (AC-21) | per coin row | `position_snapshot` per coin, from the cache and that coin's view |
| Account (AC-21) | unrealized P&L | every open position in the cache, each at its own coin's mid |
| Snapshot (AC-21) | `base` | the instrument's base currency in the cache |
| Snapshot (AC-21) | `avg_entry_px` rounding | `min(0.01, instrument.price_increment)` |
| Blocks (AC-19) | stale feed per coin | each strategy's own frozen stale check, through its wrapper |
| Noise bound (AC-23) | coin tick | the pinned instrument JSON's price increment |
| PEPE screen (AC-24) | candles | the lab catalog's `PF_PEPEUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL` bars from 2023-06-01 |
| PEPE spread (AC-25) | quotes | the recorder's PEPE quote ticks, from the AC-22 deploy on |

### Key invariants

- The rule never reads a bar's own high or low in its channel or midline (no look ahead), as with `DonchianRule`.
- The seed never causes an order or a decision row. Only live bars decide.
- A seed problem can only delay entries. It never stops the trader or touches a position.
- The spread check can only refuse an entry. Exits and stops are never filtered.
- At most one entry or exit per bar close (the base's rule) still holds, per coin.
- No coin ever trades without its own per order notional cap (AC-19).
- An event is recorded once, however many coins run (AC-18).
- A block on one coin stops new entries on all of them. It never stops a stop or an exit.
- The stale feed block is on whenever any coin is stale; one coin's fresh quotes never clear it (AC-19).
- The trader never starts with a coin it cannot find in the instrument cache (AC-15).

### Security model

No new secret. The seed fetch is an unauthenticated GET to Kraken's public charts API, and no key or header carrying one is sent. The strategy stays demo only: the config builder refuses outside `TRADING_ENV=demo` (AC-11), and live never runs it.

### Configuration required

*Amended:* one new environment variable, `PAPER_INSTRUMENTS` (AC-15). It has a default in code and in `compose.yaml`, so a deploy without it in `kfs.env` keeps BTC only and does not roll back. On the VPS, after the manual promote of the two coin version, set these in `/etc/kfs/kfs.env` by hand (back the file up first):

- `PAPER_STRATEGY=donchian_scalp_r1`
- `PAPER_INSTRUMENTS=PF_PEPEUSD.KRAKEN,PF_XBTUSD.KRAKEN`
- `RECORDER_INSTRUMENTS` with `PF_PEPEUSD.KRAKEN` added to its current value (AC-22)

Add `PAPER_INSTRUMENTS` to `deploy/kfs.env.example`, with a comment.

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
- Settings: `PF_PEPEUSD.KRAKEN,PF_XBTUSD.KRAKEN` and the reverse order give the same sorted list; a duplicate, an empty entry, and `BTCUSD` each refuse; two coins with `donchian_scalp` refuse; a coin missing from the instrument cache refuses start and names the id. Verifies **AC-15**.
- Wiring: two coins build two r1 strategies with tags `PEPEUSD` and `XBTUSD`, and the kill switch holds both; a halt with a PEPE position open flattens it. Verifies **AC-16**.
- Version: the two coin config hashes differently from the one coin config, the same whatever order you write the list in; `donchian_scalp`'s version is byte for byte the same as before. Verifies **AC-17**.
- Recording: two coins give two instrument rows before any order row; one order event gives one order row; a funding boundary with an open PEPE position books PEPE's rate, not BTC's; with one coin the rows match a recorded fixture from before the change; one funding boundary with both coins open books both and raises at most one alert; PEPE with no mid leaves BTC's liquidation estimate in place. Verifies **AC-18**.
- Risk: the risk engine config holds a notional entry per coin; a block raised by the BTC strategy stops a PEPE entry; an open PEPE position counts in BTC's exposure check. Verifies **AC-19**.
- Blocks: BTC fresh and PEPE stale keep `STALE_FEED` on through 10 stale checks of each coin, with one `stale_feed` risk event; it clears only after PEPE is fresh too. Other reasons pass straight through. Verifies **AC-19**.
- PEPE sizing: small equity, a wide stop, and step 1000 give zero quantity and a refusal decision with no order; a normal case gives a whole number of steps, and the stop price is a valid `1e-10` tick price. Verifies **AC-19**.
- Seed: a fetch that fails for PEPE and works for BTC logs one `warmup_seed_failed` with `instrument=PF_PEPEUSD.KRAKEN`, BTC is ready after its first live bar, and PEPE warms up cold. Verifies **AC-20**.
- Snapshot and card: two coins, one open and one flat, give two entries and a card with an open row and a flat row; an old `positions` snapshot shows the card offline; the account's unrealized P&L is the sum of both; a PEPE entry at 0.0000123456 keeps its digits while BTC still rounds to cents; PEPE with no quote gives an `unknown` row and BTC's row still shows; each row shows its own `base`. Verifies **AC-21**.
- Guard: with a tick of 0.1 the bound is `1e-8` and a stored BTC month rebuilds the same; with a tick of `1e-10` a price `0.5e-10` off a tick is off tick. Verifies **AC-23**.

## Build plan

Tracer Bullet: the first two tasks give a thin real thread (rule to node to trader on the laptop demo). The lab screens come right after, because they need only the rule and give the earliest answer on the fee hurdle. Each later task thickens one segment end to end. Demo (task 6) happens whatever the screen verdicts.

1. [x] Hash baseline: record the code hash of every registered strategy version to a scratch file, as specs 0021 and 0022 did. Satisfies **AC-5**.
2. [ ] Thin thread: `DonchianMidlineRule` with its unit tests, `DonchianScalpR1` and its config (no seed, no spread check yet; `seed` already defaults to `()`), the `PaperStrategyName` value, `paper_version`, and `build_paper_strategy` wiring. Laptop smoke: stack up with `PAPER_STRATEGY=donchian_scalp_r1`, the trader starts and records a version. Then take the stack down. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-11**, **AC-12**.
3. [ ] Lab screens: register both strategies in `lab/strategies.py` (AC-13 kinds and flags), commit both study files, run `lab study` and `lab screen` for each, and write the AC-13 table into `verify.md`. Run the AC-13a spread query and record it too. Satisfies **AC-13**, **AC-13a**.
4. [x] Spread filter: the `_decide_entry` override, `spread_max_atr` 0.1 in the paper config, and tests. Satisfies **AC-6**.
5. [ ] Warmup seed: `trading_core/strategy/warmup.py` (pure checks and tests), `trader/warmup.py` (fetch, retries), the `main()` call and the `seed` parameter through `run_node` and `build_paper_strategy`, the node's first bar join and feed, and the `warmup_seed_failed` alert rule with its coverage test. Laptop smoke: a start shows the rule ready after the first live bar. Then take the stack down. Satisfies **AC-7**, **AC-8**, **AC-9**, **AC-10**.
6. [ ] Close out and demo: moved to task 11, so it runs after the two coin work below.

Amendment tasks (two coins), still Tracer Bullet: task 7 is a thin two coin thread through settings, wiring, version, recording, and risk. Each later task thickens one segment.

7. [x] Two coin thread: first print each strategy's closure with the hash tool and confirm `node/recording.py`, `node/paper_snapshots.py`, `node/context_feed.py`, and `lab/candles.py` are in none (if one is, stop and amend AC-5). Then `PAPER_INSTRUMENTS` with its checks (including the instrument cache check) and the compose line, one r1 per coin with its `order_id_tag`, the per coin blocks wrapper, a PEPE sizing test, `instruments` in r1's version, the multi coin `RecordingActor`, a notional cap per coin, the context feed list, and the `kfs.env.example` line, with tests. Satisfies **AC-15**, **AC-16**, **AC-17**, **AC-18**, **AC-19**, **AC-5**.
8. [x] Seed per coin: the symbol in `CHARTS_URL`, one fetch per coin, a seed mapping into `build_paper_strategy`, the `instrument` field on `warmup_seed_failed`, and tests. Satisfies **AC-20**, **AC-7**.
9. [x] Snapshots and dashboard: the `positions` snapshot (with `base`, the tick rounding, and `unknown` entries), the account sum, `paper_status` over the views, and the Overview card's rows per coin with `sides` per row, with tests. Satisfies **AC-21**.
10. [ ] Lab: the per instrument noise bound and its tests, the PEPE months checked again, `studies/donchian_scalp_r1_pepe.toml` committed, then `lab study` and `lab screen`, and the figures in `verify.md`. Satisfies **AC-23**, **AC-24**.
11. [ ] Close out and demo (was task 6): compare hashes against task 1 by the amended AC-5 rule, merge, deploy (`--no-deps`), confirm the demo venue lists `PF_PEPEUSD` (the trader's instrument load on the VPS), get the two coin version id (`trader.version` with both settings set), promote it by hand, set `PAPER_STRATEGY`, `PAPER_INSTRUMENTS`, and `RECORDER_INSTRUMENTS` in `kfs.env` (backed up first), redeploy the trader and the recorder, and check the first decisions and the card for both coins. Run the AC-13a BTC query. Satisfies **AC-5**, **AC-13a**, **AC-14**, **AC-22**.
12. [ ] PEPE spread check after 7 recorded days. Satisfies **AC-25**.

## Migration plan

**Strategy**: strangler (r1 runs beside the unchanged `donchian_scalp`; one env line picks which one trades on demo).
**Phases**:
1. Merge and deploy with `PAPER_STRATEGY=donchian_scalp` still set. Nothing changes on the VPS, and `donchian_scalp`'s version hash is the same, so no promote is needed.
2. Get r1's version id (`trader/version.py` with `PAPER_STRATEGY=donchian_scalp_r1`), run `gate promote --manual --step candidate-paper --version <id>`, set `PAPER_STRATEGY=donchian_scalp_r1` in `/etc/kfs/kfs.env` (back it up first), and redeploy.
*Amended:* phase 2 sets `PAPER_INSTRUMENTS` and `RECORDER_INSTRUMENTS` together with `PAPER_STRATEGY` (see *Configuration required*), and the version you promote is the two coin one. In phase 1 the dashboard and the trader deploy together, so the card moves from `position` to `positions` in one step. The old `position` key is then simply no longer written.
**Rollback**: set `PAPER_STRATEGY=donchian_scalp` again and redeploy. Its promotion still stands. *Amended:* also remove `PAPER_INSTRUMENTS` (back to BTC only). On demo you need not flatten first: the restart marks any open position abandoned (`close_abandoned`), PEPE included.
**Risks**: a deploy with r1 set before the promote exits 4 and rolls back (the known deploy guard). The trader is flat at the switch only if no `donchian_scalp` position is open, so switch while flat; on demo a restart marks any open position abandoned. *Two coins:* the 50 trades a day cap is shared, so a busy PEPE day can lock BTC out of entries until the next UTC day.

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
- *Two coins:* with both coins in a trade at once, about 1% of equity is at risk instead of 0.5%. The shared caps bound it, but the two coins draw on one budget: 50 trades per day, 20 USD daily loss, and 500 USD exposure across both. A busy PEPE day can use up BTC's room, and a loss on one coin can pause both.
- *Two coins:* shared blocks mean a stale PEPE quote, which is likelier on a thin book, also stops BTC entries.
- *Two coins:* the `position` snapshot goes away. Anything outside the dashboard that read it would break. Today nothing does.
- *Two coins:* the PEPE screen is a third r1 trial, and PEPE's history is shorter (from mid 2023), so its screen covers fewer days than BTC's.
- *Two coins:* more catalog disk on the VPS for PEPE's book.

**Neutral**:
- The seed candles come from Kraken's own aggregation and the live bars from ours. Small differences in a bar's high or low are possible. Closes, which the EMA uses, should match.
- The first live bar after a start is partial. r1 ignores it (AC-10); `donchian_scalp` still uses it.

## Follow-up

- [ ] A split exit (half at 1.5R, the rest trailed) from the same blueprint, as a later revision if r1 shows promise on demo.
- [ ] Whether the seed should also serve `donchian_scalp` and `donchian_4h` (their warmup is 1.75 hours and 3.5 days). That needs their own new modules, because they are frozen.
- [ ] After a few weeks of demo, count `spread_wide` skips and compare with the AC-13a distribution. If there are none, the filter is not doing anything, and 0.1 ATR may be too loose for BTC's one tick spread; amend the value here.
- [ ] A `bar_rule_v2` base could run the time stop while the rule warms up, which closes the cold start risk in AC-9.
- [ ] Entry blocks per coin, if shared blocks turn out to stop BTC too often because of PEPE (count the `stale_quote` blocks with PEPE's quote age on demo).
- [ ] Per coin budgets (trades per day, exposure) if one coin keeps using up the other's room.
- [ ] The live path (reconcile executor, drill, live fee source) stays BTC only. Two coins on live is a decision for feature 19.
