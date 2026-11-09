# 0031. Liquidity trap scalper: a one trial pre check first, then a proven feed layer only if it passes

**Date**: 2026-11-09
**Status**: In Progress

## Summary

This spec tests the liquidity trap scalper as cheaply as it honestly can. The idea is to fade a failed break of a recent high or low when heavy aggressive trading barely moved the price. First comes the signal and a one trial pre check on 12 recorded BTC days, because the move must clear about 10.5 bps gross and earlier book based scalpers did not clear their fee hurdles. If it fails, the result is recorded and the work stops. If it passes, this spec builds and proves the live feed layer, and a second spec designs the state machine, paper execution, and replay. Nothing here places an order, and live trading stays locked off.

## Requirements

**User stories**:
- As the trader, I want one cheap, fixed test of the sweep and absorption idea, so I learn whether it can cover its costs before paying for an execution build.
- As the trader, I want proof that a signal only ever sees a healthy book and clean trade flow before anything runs on live data.
- As the trader, I want live trading impossible from this feature, whatever the configuration says.

**Build phases**: phase 1 is the pre check: **AC-1**, **AC-2**, **AC-3**, and **AC-10** to **AC-20**. Phase 2 runs only after a `pass`: **AC-4** to **AC-9**.

**Acceptance criteria**:
- **AC-1** (hash baseline): before any code, `verify.md` records the `module hash` list from `scripts/spikes/hash_check.py` at the commit this branch starts from. At each close out the same list matches line for line, and the only addition is `trading_core.node.lts_feed`. No module already in a `trading_core.node` import closure is edited, so `settings.py` and `marketdata/errors.py` stay unchanged.
- **AC-2** (live locked off): `LtsSettings` in `trading_core/lts_settings.py` reads `LIVE_TRADING_ENABLED`. Unset or `false` gives False. A true value makes the command exit 78 with a clear log line, not a traceback. The lab commands construct `LtsSettings` next to `LabSettings`. Tests cover omitted, false, and true, and a source check proves nothing outside the settings module and the refusal reads the field. `TRADING_ENV=live` still exits 78 in the lab.
- **AC-3** (trade normalization): `NormalizedTrade` holds instrument, trade id, exchange and receive times in nanoseconds, Decimal price and raw size, Decimal quote notional, aggressor side, and kind. Kraken `side` `buy` and `sell` are taker sides. `fill`, `liquidation`, `partialLiquidation`, `termination`, and `block` map to their kinds (`partialLiquidation` to liquidation). Any other type raises `MarketDataIntegrityError`. A second trade with the same id counts once, including one seen in both a `trade_snapshot` and the live stream. The Nautilus `TradeTick` converter lives in `node/lts_feed.py`.
- **AC-4** (instrument metadata): `parse_instrument` takes one row of `/derivatives/api/v3/instruments` and returns `InstrumentMeta` with symbol, contract type, contract size, tick size, size step (10 to the power of minus `contractValueTradePrecision`, recorded as derived because Kraken publishes no minimum size), max position, tradeable and suspended flags, and `makerProtectionMillis` when present. A missing, zero, or negative required field, a non tradeable or suspended market, or a contract type other than `flexible_futures` raises `InstrumentMetadataError`. `quote_notional` multiplies price, size, and contract size. Instrument ids come only from configuration or a run file, and metadata past its maximum age reads as stale.
- **AC-5** (book): the book is Nautilus `OrderBook` with `L2_MBP`. Tests on a small committed capture prove that a snapshot then updates gives the expected levels, that a delete removes a level, and that a mid run `CLEAR` rebuilds the book from the snapshot that follows.
- **AC-6** (health gate): pure `FeedHealth.reasons(now_ns)` returns an empty tuple only when healthy. Otherwise it returns each that applies of: `no_snapshot`, `book_stale` (latest book update older than `LTS_BOOK_MAX_AGE_MS`), `crossed_book`, `sequence_invalid`, `metadata_missing`, `metadata_stale`, `processing_lag` (`ts_init` minus `ts_event` above `LTS_MAX_PROCESSING_LAG_MS`), and `trade_silent` (no trade in `LTS_TRADE_SILENCE_S`). A socket drop has no event in a Nautilus actor, so `book_stale` covers it. In the laptop smoke `processing_lag` is reported only, because clock offset is allowed up to 250 ms.
- **AC-7** (sequence gate): pure `SequenceGate` marks the feed invalid the moment an adapter `sequence` is not the last plus 1, and stays invalid until a `CLEAR` snapshot completes for every subscribed instrument. The counter is shared across instruments, so one hole invalidates all of them.
- **AC-8** (feed actor): `node/lts_feed.py` adds `LtsFeedActor`, a thin Nautilus actor that subscribes to `L2_MBP` deltas, trades, and `LiquidationTick` for its configured instruments and feeds `FeedHealth`, `SequenceGate`, and the normalizer. It never resubscribes, because spec 0007 proved that brings no `CLEAR`. On a sequence hole or a `MarketDataIntegrityError` it logs a critical line and stops. It is not added to the trader, and nothing is deployed.
- **AC-9** (feed facts proven): `lab lts-feed-smoke` starts a data only node with the actor plus two raw public connections for `--seconds` (default 300), stops itself, and writes a JSON report. Connection B resubscribes about once a minute to get fresh snapshots. The run passes only when all hold: (a) at least 99 % of the live raw `uid`s seen during the run appear as a Nautilus `TradeTick.trade_id`; (b) Kraken `seq` went up by exactly 1 per product and feed; (c) `seq` is shown to identify the same message across the two connections, and the book rebuilt from connection A up to a B snapshot's `seq`, with `qty` 0 treated as removal, matches that snapshot across the full depth in at least 99 % of the union of price levels, over at least 5 comparisons; (d) the report states whether a raw row of a non fill type was delivered as a `TradeTick`, or `not observed` if none occurred. A failure of (a), (b), or (c) stops the build and returns the design to `/architect`.
- **AC-10** (flow): `FlowWindows` keeps aggressor buy and sell quote notional, delta, total, imbalance, trade counts, median and largest trade notional, and arrival rate over 250 ms, 1 s, and 2 s, and liquidation notional by side separately. A forced trade's id is remembered, so it is left out whether its liquidation record arrives before or after the trade. All times in phase 1 are `ts_init`, and `ts_event` is not used.
- **AC-11** (efficiency): pressure notional is the side's aggressor notional over the 1 s window. `aligned_impact_bps` is `d * (mid_end - mid_start) / mid_start * 10_000` with `mid_start` the last mid at or before the window start. Efficiency is pressure notional divided by `max(0, aligned_impact_bps)` plus 0.1 bps, so a move against the pressure counts as strong absorption. `EfficiencyRanks` keeps samples per side: when an event crosses a whole second, each side with flow in the window that just ended adds one sample, and seconds with no event add none. It keeps the trailing 60 minutes and is warm per side after 60 minutes and 500 samples of that side. Rank is computed on each trade as the share of stored samples at or below the value, and a rank never includes the value being ranked.
- **AC-12** (depth resilience): for each signal the report holds displayed base size on the relevant side within `depth_band_bps` of the frozen level: before the sweep, its minimum during the sweep, and at the signal, plus `replenishment_ratio` as `max(0, after - min) / max(before, Decimal("0.00000001"))` and the time from the minimum until depth is back to at least `before`, or null if it never is. It is read from an `OrderBook` the runner builds from the deltas it consumes, never from `SegmentStream`'s own book, which runs ahead. It never decides whether a signal counts.
- **AC-13** (sweep signal): `SweepDetector` holds one candidate per instrument. The level is the rolling low of the previous `lookback_s` of trades, excluding the current trade. A long candidate starts when a warm, flat detector sees a trade at or below the level minus `sweep_buffer_ticks`; the level then stays frozen and sweeps of the opposite side are ignored until the candidate ends. Absorption latches when the seller side's efficiency rank is at least `efficiency_pct_min` while the candidate is open. The extreme tracks the lowest trade until the latch and is then frozen. A signal needs a fresh cross after the latch: a mid at or above the level plus `reclaim_buffer_ticks`, following a mid below it, within `reclaim_timeout_ms` of the sweep start. A trade at or below the frozen extreme minus `invalidation_buffer_ticks` ends the candidate, as does the timeout. Events at one `ts_init` run book first, then trades, as `SegmentStream` orders them. A short is the exact inverse, proven by a mirrored event test. After a signal, new sweeps are ignored for `horizon_s`.
- **AC-14** (no look ahead): cutting the event stream at any time and rerunning gives exactly the signals of the full run before that time. CI proves it on synthetic streams. One named recorded hour is checked in the lab and noted in `verify.md`.
- **AC-15** (markout): each signal's gross markout at 5, 15, and 30 s uses `MarkoutTracker` with the signal mid as the price. A signal whose horizon passes the end of its segment has no markout at that horizon and is counted separately. Only 15 s decides the verdict; 5 and 30 s are reported.
- **AC-16** (frozen inputs): `apps/lab/runs/lts_precheck.toml` is committed before the first run and is not changed afterward. `apps/lab/src/lab/lts_runfile.py` loads it, using native TOML dates, and exit 64 lists every problem. It holds `instrument_id = "PF_XBTUSD.KRAKEN"`, `lookback_s = 60`, `sweep_buffer_ticks = 1`, `reclaim_buffer_ticks = 1`, `invalidation_buffer_ticks = 5`, `reclaim_timeout_ms = 3000`, `efficiency_pct_min = 90`, `horizon_s = 15`, `depth_band_bps = 5`, `maker_fee = "0.0002"`, `taker_fee = "0.0005"`, `hurdle_k = "1.5"`, `min_signals_per_day = 25`, `min_positive_days = 8`, and the days 2022-06-01, 2022-10-01, 2023-02-01, 2023-06-01, 2023-10-01, 2024-02-01, 2024-06-01, 2024-10-01, 2025-02-01, 2025-06-01, 2025-08-01, 2025-10-01. All are before the gate's `holdout_start`. The other 43 Tardis days stay unread by this feature.
- **AC-17** (verdict): on the Tardis days the verdict is `pass` only when all four hold, with cost equal to maker plus taker fee in bps (7), counting only signals that have a 15 s markout: the pooled mean 15 s markout is at least `hurdle_k` times cost (10.5 bps); signals per 24 eligible hours are at least 25, where eligible time is each segment's length minus its 60 minute warm up; at least 8 of the 12 days have a positive unweighted daily mean (a day with no signals or no eligible hours is not positive); and the pooled mean with the single best day removed (largest sum of markouts) is at least cost (7 bps). The report lists each check with its value and threshold and labels the result `approximate: Tardis has no liquidation flag`.
- **AC-18** (cross check): the same run with `--source own --days ...` covers the 3 newest full UTC days of BTC on our own catalog that have a manifest and at least one liquidation row, copied read only from the VPS and named in the Results. The segment planner drops unclean hours as usual. It reports the 15 s mean and signal count with and without liquidations removed by `uid`, and how many `LiquidationTick.trade_id`s matched a `TradeTick.trade_id`. Zero matches is labelled `no_op`. It is descriptive and does not change the verdict.
- **AC-19** (reproducible): gaps are honored through the lab's segment planner. Two runs on the same inputs and image write byte identical reports except for the start and end time fields. The report is `lab-reports/lts-precheck/<source>_<run file hash 12>_<git commit 12>.json` and holds the git commit, `LTS_PRECHECK_VERSION` (a constant in `lts_precheck.py`), run file hash, per day counts, the tick in bps per day, ended candidates by reason, and for every signal: side, `ts_init`, mid, level, extreme, 1 s buy and sell notional, impact, efficiency and its rank, the AC-12 depth fields, and the 5, 15, and 30 s markouts.
- **AC-20** (close out): the Results section records the verdict, every check, and that this was 1 trial. A `fail` stops the feature with no revision or second setting set, phase 2 is not built, and the scope says so. A `pass` builds phase 2, then adds a follow up to design the state machine, execution, and replay in a new spec, using the 43 unseen days. The AC-1 comparison runs again.

## Decision

**Chosen option**: Option 1: a pure, causal signal scanner over the lab's existing hour stream, with the live feed layer built only after a pass.

Pure rules in `trading_core`, two lab commands, one frozen run file. No orders, no schema change, no deploy.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Code placement**:
| File | Phase | Owns |
|---|---|---|
| `packages/core/src/trading_core/lts_settings.py` | 1 | `LtsSettings` |
| `.../marketdata/lts_errors.py` | 1 | `MarketDataIntegrityError`, `InstrumentMetadataError` |
| `.../marketdata/normalized.py` | 1 | `NormalizedTrade`, `TradeKind`, kind parsing, trade dedupe through `RecentIds` |
| `.../strategy/lts_features.py` | 1 | `FlowWindows`, `aligned_impact_bps`, `efficiency`, `EfficiencyRanks`, `replenishment_ratio` |
| `.../strategy/lts_sweep.py` | 1 | `RollingExtremes`, `SweepDetector`, `LtsParams`, `LtsSignal` |
| `.../lab/lts_verdict.py` | 1 | Pure verdict from signal markouts |
| `.../node/lts_feed.py` | 1, then 2 | The `TradeTick` converter, then `LtsFeedActor` |
| `apps/lab/src/lab/lts_runfile.py` | 1 | Run file loader |
| `apps/lab/src/lab/lts_precheck.py` | 1 | Runner and JSON report |
| `apps/lab/src/lab/main.py` | 1, then 2 | The subcommands |
| `apps/lab/runs/lts_precheck.toml` | 1 | The frozen run file |
| `.../marketdata/instrument_meta.py` | 2 | `InstrumentMeta`, `parse_instrument`, `quote_notional` |
| `.../marketdata/feed_health.py` | 2 | `FeedHealth`, `HealthReason`, `SequenceGate` |
| `apps/lab/src/lab/lts_feed_smoke.py` | 2 | Smoke run and raw comparison |

**Data model sketch**: no schema change. Results live in JSON under `lab-reports/lts-precheck/` and `lab-reports/lts-feed-smoke/`, and in this spec's Results section.

**State transitions** (`SweepDetector`, per instrument): `warming` to `flat` once `EfficiencyRanks` is warm and the lookback is full; `flat` to `swept` on a sweep; `swept` to `absorbed` on the latch; `absorbed` to `flat` with a signal on reclaim; `swept` or `absorbed` to `flat` on timeout (`no_absorption` or `no_reclaim`), `invalidated`, or `segment_end`. After a signal, sweeps inside `horizon_s` count as `cooldown`.

**Interface surface** (lab CLI only):
| Command | Key inputs | Key outputs | Key errors |
|---|---|---|---|
| `lab lts-precheck <run file>` | `--source tardis\|own` (default `tardis`), `--days` (own only, replaces the file's days) | pre check JSON with verdict, exit 0 for a pass or a fail | 2 missing day manifest, 64 bad run file, 78 live refused or no `GIT_COMMIT` |
| `lab lts-feed-smoke` (phase 2) | `--instruments` (required, comma separated), `--seconds` | smoke JSON, exit 0 on pass, 1 on fail | 64 bad arguments, 78 `TRADING_ENV=live` or a true `LIVE_TRADING_ENABLED` |

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| normalize | price, size, side, times | Nautilus `TradeTick` in phase 1; raw feed fields only in the smoke comparison |
| normalize | kind on a Nautilus tick | `fill`, unless its id matches a `LiquidationTick.trade_id` (spec 0009) |
| notional, sweep | contract size, tick | the pinned instrument's `multiplier` and `price_increment`; `InstrumentMeta` in phase 2 |
| flow, sweep | trade stream | `SegmentStream` `TradeTick` events, in `ts_init` order; `TradeTick` has no type, so Tardis flow is all trades |
| impact, reclaim, markout | mid | the bid and ask of `QuoteTick` events from `SegmentStream` |
| depth | sizes within the band | the runner's own `OrderBook`, built from the `OrderBookDeltas` it consumes |
| efficiency rank | trailing samples | `EfficiencyRanks`, cold at each segment start |
| verdict | 7 bps cost | run file `maker_fee` plus `taker_fee`, base tier, same values as the lab |
| verdict | 1.5 and 25 | run file; the same `fee_hurdle_k` and trade floor as spec 0021 |
| verdict | eligible time | segments from `plan_segments`, minus 60 minutes each |
| report | git commit | `GIT_COMMIT`, as `lab replay` |
| report | version | `LTS_PRECHECK_VERSION` |

**Key invariants**:
- Every decision uses only events at or before its `ts_init`.
- The reference level never moves during a candidate.
- A liquidation print never starts a candidate by itself once its kind is known.
- Prices, sizes, and notionals are `Decimal`; times are UTC nanosecond ints.
- `packages/core` folders stay within `test_core_boundary.py`; only `node/` imports Nautilus.
- A pass here says the signal may have an edge before fills and fees. It never enables anything by itself.

**Security model**: public data only. No Kraken key, no secrets grant, no execution client, no Postgres. The commands run only in the laptop `lab` service, and the env guard's execution client allowlist is unchanged.

**Configuration required** (all in `LtsSettings`, each with a default, so no deploy can fail on a missing value):
- `LIVE_TRADING_ENABLED`: False, and may never be true.
- `LTS_BOOK_MAX_AGE_MS`: 500.
- `LTS_MAX_PROCESSING_LAG_MS`: 250.
- `LTS_TRADE_SILENCE_S`: 30.
- `LTS_INSTRUMENT_META_MAX_AGE_S`: 3600.

**Critical test scenarios**:
- Omitted, false, and true `LIVE_TRADING_ENABLED`, verifies **AC-2**.
- Taker sides, liquidation kinds, an unknown type, and a `uid` seen twice, verifies **AC-3**.
- Hand calculated flow, impact, efficiency, a move against the pressure, a forced trade arriving in either order, and a tiny sample that stays cold, verifies **AC-10**, **AC-11**.
- Depth fields from a runner built book, including never replenished, verifies **AC-12**.
- A synthetic failed breakdown gives one long; its mirror gives one short; a genuine breakdown invalidates; a late reclaim times out; a level stays frozen while price runs; a mid already above the level needs a fresh cross; an opposite sweep is ignored, verifies **AC-13**.
- A truncated stream gives the same earlier signals, verifies **AC-14**.
- Verdict boundaries: 10.5 bps exactly passes and just below fails, 8 and 7 positive days, 25 signals, a day with no eligible hours, and best day removal, verifies **AC-17**.
- Two runs compare byte identical, verifies **AC-19**.
- Phase 2: `flexible_futures` notional, an inverse row refused, each bad field refused, verifies **AC-4**.
- Phase 2: snapshot, update, delete, and mid run `CLEAR` on `OrderBook`, verifies **AC-5**.
- Phase 2: each health reason alone and a healthy feed; a hole invalidates every instrument until all rebuild; the actor stops on a hole, verifies **AC-6**, **AC-7**, **AC-8**.

## Build plan

Tracer Bullet, ordered by what can end the work soonest: one thin thread from recorded events to a verdict first.

Phase 1:
1. Record the AC-1 hash baseline in `verify.md`, satisfies **AC-1**.
2. `LtsSettings`, `lts_errors.py`, and `normalized.py` with the converter, satisfies **AC-2**, **AC-3**.
3. `lts_features.py`, satisfies **AC-10**, **AC-11**, **AC-12**.
4. `lts_sweep.py` with the mirror and truncation tests, satisfies **AC-13**, **AC-14**.
5. `lts_verdict.py`, `lts_runfile.py`, `lts_precheck.py`, and the frozen run file, committed before any run, satisfies **AC-15**, **AC-16**, **AC-17**, **AC-19**.
6. Run the Tardis days once and the recorded hour check. Copy the 3 own days from the VPS read only and run the cross check, satisfies **AC-14**, **AC-17**, **AC-18**.
7. Results, scope, and the hash comparison. On a `fail`, stop here, satisfies **AC-1**, **AC-20**.

Phase 2 (only after a `pass`):
8. `instrument_meta.py`, satisfies **AC-4**.
9. `feed_health.py` with `SequenceGate`, satisfies **AC-6**, **AC-7**.
10. `LtsFeedActor`, the committed capture and book tests, and `lab lts-feed-smoke`; run it on the laptop for 5 minutes, then bring it down. Stop and return to `/architect` on a failure, satisfies **AC-5**, **AC-8**, **AC-9**.
11. Hash comparison and close out, satisfies **AC-1**, **AC-20**.

## Consequences

**Positive**:
- A wrong idea costs a small pure build and one replay.
- The test is one trial, so a pass carries more weight at the gate later.

**Negative / tradeoffs**:
- A mid based markout ignores queue, missed fills, and adverse selection, so a pass is necessary but far from sufficient.
- ETH is untested, so a pass covers BTC only and your source document's BTC and ETH evidence stays open.
- Tardis flow includes any liquidations; the cross check covers only a few recent days, and it may be a no op if the adapter never emits non fill trades.
- Fixed tick buffers mean different distances in bps across the years; the report logs this, and it is accepted to keep one setting set.
- Fixed settings could miss an effect that exists elsewhere. That is accepted, because a search would weaken any pass.
- Exchange side gaps may be invisible through the adapter's own counter; AC-9 measures Kraken's numbers only during the smoke run.
- The feed actor can only stop on a sequence hole, since Nautilus offers no way to refetch the book without a reconnect.
- `partialLiquidation` rows reach the own catalog only once PR #133 is deployed, so the cross check may undercount liquidations on days before that.

**Neutral**:
- A worktree has no `catalog-lab`; point the lab volume at the main checkout's folder when running from one.
- `LIVE_TRADING_ENABLED` is a `bool` plus a check rather than `Literal[False]`, because an environment value of `false` is a string a `Literal[False]` would reject.
- A run file edit needs an image rebuild, as for every lab run file.

## Follow-up

- [ ] On a pass: design the state machine, paper execution, sub second markouts, replay, and ETH in a new spec.
- [ ] Before any execution spec: recheck `makerProtectionMillis`, your EEA fee tier, and the dead man's switch treatment of stops.
- [ ] Add any new conventions to `apps/lab/AGENTS.md` and `packages/core/AGENTS.md` through `/sync`.

## Results

To be filled in by build plan tasks 6 and 7.
