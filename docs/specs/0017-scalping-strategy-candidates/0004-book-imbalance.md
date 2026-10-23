# 0004. `book_imbalance`: join a lopsided order book when trade flow agrees, in and out post only

## Summary

When the resting size near the touch is much heavier on one side, and the trades of the last 15 seconds lean the same way, this candidate bets price drifts toward the light side over the next few minutes. It enters with a post only order at the touch, exits with a post only target (index AC-19), a stop, or after 5 minutes. It reads the order book, so it runs in replay only, ungated. Added by the fourth amendment (2026-10-22).

## Hypothesis

- **Why it might persist**: a heavy bid means buyers are queued to absorb selling, and aggressive buying on top of it means demand is real now. Short term price tends to move toward the thin side. This is the most studied short horizon signal in order book research.
- **When it should work**: normal liquidity, tight spreads, busy hours.
- **When it should fail**: spoofed or quickly pulled size (the imbalance vanishes before price moves); news; any moment when the 6 bps a round trip needs takes longer than the signal lasts. That last one is the main risk: book imbalance mostly predicts the next few seconds, and the typical 5 minute move is 5.9 bps (the audit). The persistence and flow tests exist to keep only the stronger, longer cases.
- **Entry mode**: maker, and the exit is maker too, so a round trip that hits its target costs 4 bps. A resting bid on the heavy side waits behind the queue, so fills come late and some come only when the heavy side breaks (adverse selection). The replay's queue model charges that honestly.

## Rules

On a 15 second clock timer aligned to the epoch (index AC-22), evaluated on each firing `t` while flat (base rules of index AC-5 apply; "bar close" below means a firing, and a quiet bucket still fires). The node subscribes L2_MBP order book deltas and trade ticks and reads depth from the cache's book.

- **Depth**: at the bar close, `B` = the summed size of the top `levels` bid levels of the cache's book, `A` = the same on the ask side. A side with no level gives no signal.
- **Imbalance**: `imb[t] = (B − A) ÷ (B + A)`, a `Decimal` in −1 to 1.
- **Flow buckets**: every trade tick adds its signed size (buyer aggressor +, seller aggressor −, `NO_AGGRESSOR` 0) to the bucket keyed by `floor(ts_event ÷ 15 s)`. On the close of the bar for bucket k, `flow[t]` is bucket k (0 when it has no trades), and older buckets are dropped. A trade whose bucket is already read is dropped and counted in `late_trades`, as child 0003 does.
- **Long**: `imb ≥ imb_min` on each of the last `persist_bars` bar closes, and `flow[t] > 0`.
- **Short**: `imb ≤ −imb_min` on each of the last `persist_bars` bar closes, and `flow[t] < 0`.
- **Stop**: long, `mid × (1 − stop_bps ÷ 10,000)`; short, `mid × (1 + stop_bps ÷ 10,000)`, from the last quote's mid at the signal.
- **Target**: long, `mid × (1 + target_bps ÷ 10,000)`; short, `mid × (1 − target_bps ÷ 10,000)`, fixed at the signal.
- **Time stop**: `time_stop_min`.
- **No trade**: fewer than `persist_bars` imbalance values, an empty book side, the base's blocks, spread cap, and reward floor.
- **`ScalpSignal.inputs`**: `imbalance`, `bid_depth`, `ask_depth`, `flow`, `mid`.

## Config and grid

Fixed: the 15 second timer (no `bar_spec`), `levels = 5`, `persist_bars = 2`, `stop_bps = "9"`, `time_stop_min = 5`, `entry_mode = "maker"`, `exit_mode = "maker"`, `entry_timeout_s = 10`, `min_target_bps = "6"`, `regime_adx_max` unset, `max_spread_bps` = the audit p95 (`"6.27"`, index AC-3).

Grid (4 points, sorted key expansion): `imb_min = ["0.3", "0.5"]`, `target_bps = ["6", "9"]`.

Warmup: `persist_bars` bar closes with a book on both sides (30 seconds), so a replay segment loses almost no time.

## Study files

| File | Lab | Notes |
|---|---|---|
| `apps/lab/studies/book_imbalance.toml` | replay | all fields per index AC-10, the config above |

No candle screen (the candle lab has no book). The index AC-23 pre check (4 development days, grid point `imb_min = "0.3"`, `target_bps = "6"`, at least 17 approved entries a day and an edge of at least 1.5 × cost) must pass first. Scratch run files (not baked into the image): `lab-reports/scratch/book_imbalance.toml` (2025-09-01, for the AC-16 style one day proof) and `lab-reports/scratch/book_imbalance_precheck.toml` (the pre check days and grid point).

## Rationale (short)

The first design left book imbalance out because its edge decays within seconds, far below a 10.5 bps hurdle. Maker exits cut the hurdle to 6 bps, and imbalance is the one scalping idea that fires often enough for the 12 day replay holdout's 200 trades, so it now gets one honest, cheap test. The persistence and flow tests are the only filters, each fixed, so the grid spends its 4 points on the two settings that set the trade off (how lopsided, how far). Runner up: a taker entry on imbalance, rejected because a 7 bps round trip puts the hurdle back at 10.5 bps.
