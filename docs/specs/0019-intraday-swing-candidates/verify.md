# Verify: intraday swing candidates · spec 0019 · updated 2026-10-24
_Run records and verify steps for spec 0019. `/check verify` runs the steps; `/test` locks the durable ones._

## Task 1 cost check (2026-10-24)

`scripts/spikes/cost_check_15m.py` in `kfs/lab:s0019`, over the development range's stored 1 minute candles
(2022-04-01 up to holdout_start 2025-10-19), aggregated into UTC aligned 15 minute bars. Data only: no strategy
was built or run, so it is not a trial (AC-9).

- 124,512 fifteen minute bars.
- Round trip cost: about 14 bps (two 5 bps taker fees plus 2 bps slippage each side).
- Median ATR(14) of 15 minute bars: **29.21 bps**.
- `ema_pullback` (1.5 R on a stop of at least 1 ATR): at least 43.82 bps, 3.13 × cost.
- `vwap_reversion` (the VWAP at least `stretch_atr` 2 ATR away): at least 58.43 bps, 4.17 × cost.
- `us_open_breakout` (1.5 R on a stop of at least 1 ATR): at least 43.82 bps, 3.13 × cost.
- `connors_rsi` median |close − SMA(5)|: **11.11 bps**, 0.79 × cost. Its typical exit is under the round trip
  cost, which is the likely fail child 1 names.

## connors_rsi_15m run (2026-10-24)

- Study 11:42 to 13:34 UTC, 6 grid points, all ok; holdout 13:34 to 13:53 UTC (candidate batch 01a16e1f, buy and hold 01a16e24, MACD 01a16e2a); study batch 01a16db8. Image `kfs/lab:s0019` built at 7b3583c.
- Pushed with `scripts/push-bundles.sh` (imports took about an hour each on the VPS, nothing refused), then `gate evaluate connors_rsi_15m` on the VPS: `fail`, promotion 01a16ec6. The laptop dry run gave the same verdict and metrics (laptop N 74, VPS N 12). Full results in [0001-connors-rsi-15m.md](0001-connors-rsi-15m.md).
- Checked: the study file commit (11:41:09 UTC) is earlier than `studies.created_at` (11:42:20 UTC); every grid point's stored config has `time_stop_bars` 12 and `bar_spec` `15-MINUTE-LAST`.

## Commands
- [ ] `pytest tests/test_lab_lookahead.py` (core) → cuts with a 15 minute interval never land on a quarter hour, the hour interval draws are unchanged → AC-2
- [ ] `pytest tests/test_lookahead.py` (lab) → every hour level strategy and `connors_rsi` at `15-MINUTE-LAST` pass the prefix check → AC-2, AC-5
- [ ] `pytest tests/test_node_connors_rsi.py` (core) → `connors_rsi` subscribes to `PF_XBTUSD.KRAKEN-15-MINUTE-LAST-INTERNAL` → AC-5
- [ ] `pytest tests/test_intraday_swing_studies.py` (lab) → each study file keeps the shared settings → AC-9
- [ ] `git log --format=%cI -1 -- apps/lab/studies/connors_rsi_15m.toml` is earlier than `studies.created_at` of `connors_rsi_15m` → AC-9
- [ ] After `lab study connors_rsi_15m`: every grid point's stored strategy version config has `time_stop_bars` 12 and `bar_spec` `15-MINUTE-LAST` → 0019.1
- [ ] `git diff origin/main -- packages/core/src/trading_core/strategy packages/core/src/trading_core/node` adds files only, and `gate versions` code hashes are unchanged → AC-1
