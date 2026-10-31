# Verify: donchian_scalp_r2 · spec 0026 · updated 2026-10-31

## Hash baseline (task 1, AC-15)

The code hash of every `trading_core.node` module closure at 49fd591 (its code is main 6f3a8ac), from `scripts/spikes/hash_check.py` on a `git archive` of that commit, run in `kfs/lab:s0025p`. 38 modules; the list sha256 is `654edb45aa7c12d48ce44b87c1ae36f366a511d298c932dd94cd89502b882a31`. `bar_rule` still matches spec 0025's baseline. At close out (task 6) the branch list, minus the new `trading_core.node.bar_rule_maker` and `trading_core.node.donchian_scalp_r2` lines, must match it line for line.

```
trading_core.node.bar_rule 5cc159481242cbb4053852ba357c73d2e67aec671009699ccd8d84ded7c02090
trading_core.node.bollinger_rsi 4aab13885be110144e287a5f580f0b06642b51f65b738c77ed7e6ce9dc74cf15
trading_core.node.book_imbalance d9165c167559b7c263938bf6d5a69f8a1689163f8da81c0ef0bedf34093f24d9
trading_core.node.buy_and_hold 78a8687e9452ac6eb119f174263d228bb29078cdcf5c890bb450af8e04d0bfd1
trading_core.node.connors_rsi 260861201aed6881d9176ef281d149a7451e2b0cd7f9a01dbdcb0d0f3bfa1e39
trading_core.node.context_feed 6dc6991676f491f373c26c295441523edad1a0b6158686def9246ae8079004ba
trading_core.node.context_types 8747b288db8a0073f300b2afa50f622ede8257061ddb85e211d6659542472cd9
trading_core.node.control 3844b938b941eb260bf096ea0fd0a63c19ce42fb074ba242ca7f3a4ac0a34664
trading_core.node.delta_divergence 30b0e4fa2b91c580972e85bfef0a0138e75796949b25ba32a3175a3d16e71f79
trading_core.node.donchian_4h 0e9f748720633f0a1e0d4b3eb05b6726e179d32a5bd0862d95c29e70e4b06b30
trading_core.node.donchian_4h_r1 5799dc6ff03e6d1cc4deb70d14ddf04d1e74d6e296e6c6829afe631acffadfab
trading_core.node.donchian_scalp 6be87d38456e2b7e2e4ae22faf6c55ec192c51a061c52d6304dbd47a7eb7ef26
trading_core.node.donchian_scalp_r1 197e7a68b0a18dedbfe4f0bbf8e90e40dc38e3d4fc8c3ba48b6de3b052742bad
trading_core.node.ema_pullback f86006e5fec416ad465224eb0c45a508110575c1405fb8ae7973ff1c7ce0ace1
trading_core.node.funding 2d17e1a332a3866aa1a36c2223af26267a2d94abdf20626b0228fed1f5bcb6e4
trading_core.node.guard 09b1119332780a084fe39da4b71cf85b0e67fabfe72c519450b94d15de095bba
trading_core.node.heartbeat 46e360bcd780b2e2f3b640e7cd7246019b352aed5b9df064cd3f144f9b0a9003
trading_core.node.imbalance_fade 3421033fba4d9d0089d6a1ae5634923f36ebb7eacdc15eb9874ea74cccc65fce
trading_core.node.key_actors 726764267181c0103ffb8c26b6f6a2b1807f557d8dfeda768ce3d5d5b037d017
trading_core.node.key_check 97e7f68a7a8a62c95a69e024f7ab287af1bb71bbef16dbd9b3a9b018c59b2c05
trading_core.node.kraken 57a8abe320bebd0f97b5f576a82637ff5740d55c9cc863b1fc0282faad9c232c
trading_core.node.loss_limits 4007e086d840912205e29648744324f56f97af5bdf6f9c97bba1aed36c9e0cc1
trading_core.node.macd_baseline 2a8730b8f75ae125d0a5296423763c63a72489efb0e600a0a012bcf9c9622c94
trading_core.node.maker_probe 533b50a674962e0c7c2be878153279a91e8f3f1398824a78ff1a8091e1d3753e
trading_core.node.noise_breakout 5c0b94cb0ac39be9c054e605e14d880ee1b011418f82bc030e1166dc657b6b6f
trading_core.node.paper_snapshots aa5447bfac5f7b663de8f0df329a60f276b25d5abd88128d0e855eb885765a64
trading_core.node.placeholder ec0b98bab810d682114cc70bffbca2ff5e7862f4848441dc270f9b67d14b6713
trading_core.node.random_taker ab290066d43a8e40ba525623fa6f412723d4ff049aba0eb76161ccebb140c4c2
trading_core.node.reconcile e483a2e1e3d7a3d4d82961e79d2c497eddff5708273b48448eafe2e6131e5967
trading_core.node.reconcile_drill 13427e34e8ae73a6a86dfc4161638264fa81a1b84e4eb2e9d4eb635994acb07a
trading_core.node.reconcile_executor 6452e398a20a77a7abbfc3695e036762436ff2243d33b125416aa8e7f4b0e1ad
trading_core.node.recording f649c12c6535590139e086eac534af98b7d1cd35c91a871e8a5c47560e4a2cd8
trading_core.node.sandbox 1d58312ced855746654af75243e0c87cd133e0cbfd1a65f381ab3238c0a75a52
trading_core.node.scalp_base 92b0b3718e3c01ad8a5c9702b3fa9884d0301311262eb198abbf835313da45be
trading_core.node.stale 1822c251d1e9616cef189aef6bf0200407d0547e70505e6f17b62df663bff96d
trading_core.node.strategy 128786a5b6cb990991a819ba636ca9ed53b20355d9135b88410ef09089408a3e
trading_core.node.untrusted 83451616d6433f9f6d3d9b1a329ec7ff2c9a956821d537e76ad911d3ded7ceec
trading_core.node.vwap_retest 44083de39ddf9f1b452b041dc81f1017ea3ee070315104e4131f52789fefdc52
```

### Close out check (task 6)

The same tool on a `git archive` of ccaa75d, in `kfs/lab:s0026`: 40 modules. The only lines not in the baseline are the two new ones below. With them taken out, the list matches the baseline line for line, with the same sha256 (`654edb45…`). The ruin fix's edit to `trading_core/gate/series.py` sits in no node closure. AC-15 passes.

```
trading_core.node.bar_rule_maker 134ee61a670e72b4ebb49cb4a1a321d9d799f148c8cad088a01e3b04c2955be8
trading_core.node.donchian_scalp_r2 dc96df87a2bab7c152e7d7d1184e1d72aa88784c954d757154598e90c08eced2
```

## Spike (task 2, *Spike first*)

`scripts/spikes/maker_spike.py`, run in `kfs/lab:s0025p` on 2026-10-31. Two venues, each fed scripted PF_XBTUSD quotes (spread 1 tick): **lab** is the candle lab's own `make_candle_engine`; **sandbox** is a backtest venue built from the demo trader's `sandbox_exec_client` settings, which are the same `SimulatedExchange` arguments the Nautilus sandbox client passes (L1 book, quotes only, no latency, reduce only checks on), with quotes through `deep_quote`. The live sandbox client itself was not driven: it wraps this same matching engine, so the events are the same.

| Check | lab | sandbox |
|---|---|---|
| (1) a post only buy limit at the ask, and a sell at the bid | both `OrderRejected`, `due_post_only = true`, reason `POST_ONLY LIMIT BUY order limit px of 85001 would have been a TAKER: bid=85000, ask=85001` | the same |
| (2a) long under a reduce only stop market and a reduce only post only exit limit, both for the full size; price reaches the limit first | both `OrderAccepted` and open together; the limit fills; the stop is cancelled by the venue when its trigger is reached (no position to reduce); no second position | the same |
| (2b) the same orders; price falls through the stop first | the stop fills; the limit is cancelled by the venue when its price is reached; no second position | the same |

Both checks pass on both venues, so AC-6's `due_post_only` path is real on both and AC-7 (the stop resting beside the exit limit) needs no amendment. Because the venue itself cancels the leftover reduce only order once its price is reached, r2 also cancels everything that rests when the position closes.

## Laptop smoke (task 2)

2026-10-31, worktree stack on `kfs/trader:s0026` (built at 2000357), `TRADING_ENV=demo`, `PAPER_STRATEGY=donchian_scalp_r2`, `PAPER_INSTRUMENTS=PF_PEPEUSD.KRAKEN,PF_XBTUSD.KRAKEN`. The trader started, recorded version `01a19166-ee6f-767a-883c-a22c635ce734` (`donchian_scalp_r2`, kind `scalping`, `entry_offset_atr` 0.5, `atr_min_bps` 10, `instruments` both coins), logged `paper_promotion_required`, and exited 78, as an unpromoted version must. The stack was taken down after.

## Lab screens (task 5, AC-17)

Both `_v3` studies ran on `kfs/lab:s0026` from the main checkout's lab catalog, 5 minute candles, `LAB_WORKERS=2`. BTC ran on the image built at ba9afa9: 1,297 days from 2022-04-01, study 1,546 s, screen 22 s. PEPE resumed on the image built at 0407479 (the ruin rule), and the lab accepted the resume with no exit 64: 871 days from 2023-06-01, study 1,874 s (14:20 to 14:51 UTC, 15:20 to 15:51 Amsterdam), screen 37 s. Both screens exited 3 (fail). The r1 column is spec 0025's `verify.md`, and r1's PEPE max drawdown comes from its `run_results` row. The maker figures come from `studies/maker_figures.py` on each run bundle.

| Figure | r2 on BTC | r1 on BTC | r2 minus r1 | r2 on PEPE | r1 on PEPE | r2 minus r1 |
|---|---|---|---|---|---|---|
| Batch | `01a191f9-38ac-766a-a242-4fa78faf8166` | `01a18df4-8d0e-7032-9c1f-958271e3eed2` | | `01a19255-8a49-778a-9e81-dce78b0966f8` | `01a18fa3-8061-7141-8ec4-0133c7f9a5ab` | |
| Screen verdict | fail (walk_forward_sharpe, fee_hurdle, trades_per_day) | fail (the same three) | same | fail (walk_forward_sharpe, fee_hurdle, trades_per_day) | fail (missing walk_forward_sharpe, fee_hurdle, trades_per_day) | same reasons, Sharpe now present |
| Trades | 6,474 | 5,002 | +1,472 | 5,085 | 21 | +5,064 |
| Trades per day (minimum 17) | 4.99 | 3.86 | +1.13 | 5.84 | 0.024 | +5.81 |
| Net P&L (USD, from 10,000) | −9,985.08 (−99.85 %) | −9,991.05 (−99.91 %) | +5.97 | −10,000.89 (−100.01 %) | −865.14 (−8.65 %) | −9,135.75 |
| Gross P&L per trade (USD) | −1.0738 | −1.0031 | −0.0707 | −1.7592 | −31.63 | +29.87 |
| Cost per trade, round trip (USD) | 0.4690 | 0.9953 | −0.5263 | 0.2076 | 9.57 | −9.36 |
| Daily Sharpe (walk forward) | −8.9262 | −5.0948 | −3.8314 | −8.7304 | none (too few trades) | n/a |
| Max drawdown (realized) | 99.855 % | 99.91 % | −0.055 points | at least 100 % (100.009 %) | 8.65 % | +91.36 points |
| Entry fill share | 0.679 (6,474 of 9,540) | n/a | n/a | 0.568 (5,085 of 8,945; 18 in part) | n/a | n/a |
| Maker exit share (upper bound) | 0.756 (4,024 of 5,323) | n/a | n/a | 0.705 (2,540 of 3,604) | n/a | n/a |
| `vol_low` skips | 7,962 | n/a | n/a | 1,046 | n/a | n/a |

The maker exit share is an **upper bound**. The candle lab fills a limit at the touch from synthetic quotes with no queue, so a real book fills fewer.

**What it says.** Maker orders did what they were meant to do on cost. BTC's cost per trade fell by more than half, and PEPE's fell from 9.57 to 0.21 USD. The gross edge did not come back. Both coins still lose more per trade before costs than the trade costs, so the fee hurdle fails by far, and both lost the whole account. Neither coin gets near 17 trades a day. PEPE's r1 column measures four days, not 871 (spec 0025: a stop block stopped it after the first week), so the PEPE differences compare a full span with a short one.

**PEPE was ruined.** The `equity_ruined` log line gives run `85819983-cff9-47e2-9d72-b01eba6fb676`, day **2025-09-01**, equity close −0.0169 USD. Under spec 0015 AC-6 that day scores −100 % and every day after it scores 0. Read its max drawdown as at least 100 %. The node kept trading tiny sizes after the ruin day, so the equity drifted to about −1.33 USD (marked) by 2025-10-18, and the 0.01 % past 100 % in the net and drawdown figures is that drift. Exit reasons: midline 3,594, `stop_through` 306, time stop 10, `stop_residual` 5, late entry fill 4. No `blocked:` refusal, so the AC-5 amendments held for the whole span.

**BTC.** Exit reasons: midline 5,307, `stop_through` 33, time stop 16, no block. 811 entries were refused as `zero_quantity` near the end, when equity was too small to size one contract.

**Trials in the family.** Six studies count as trials in the scalping family.

| Study | Batch | Standing |
|---|---|---|
| `donchian_scalp_r2` | `01a19164-8ae6-7451-8f9c-fa51dbaceef8` | invalid (before the AC-5 quote amendment, blocked after the first week) |
| `donchian_scalp_r2_pepe` | `01a19181-87bd-72fc-8739-c77134df16fe` | invalid (the same block) |
| `donchian_scalp_r2_pepe_v2` | `01a191c7-f9a7-7380-93c7-7fae95b8e9cc` | invalid (a stop that filled short of the position set the block) |
| `donchian_scalp_r2_v2` | `01a191aa-383d-729a-b3e5-dae8c81a03ef` | superseded by `_v3` (screen fail, figures as `_v3`) |
| `donchian_scalp_r2_v3` | `01a191f9-38ac-766a-a242-4fa78faf8166` | result (BTC, above) |
| `donchian_scalp_r2_pepe_v3` | `01a19255-8a49-778a-9e81-dce78b0966f8` | result (PEPE, above) |

PEPE `_v3`'s first batch, `01a19210-b820-702d-ae6d-612e6f0f7a97`, failed (the daily series raised on the ruin day before the ruin rule) and wrote no run. It is not a trial result.

## Tests at 0407479

Core 2,156 passed, 5 skipped. One failure, `test_recording_writer.py::test_a_full_queue_overflows...`, is the known flake, and it passed alone twice. Lab 330 passed (the 5 `postgres` marked tests were not run). mypy strict clean on the changed files (core, lab).

## Tests at 2000357

Core 2,134 passed, 5 skipped. Trader 175 passed (the 8 Redis ACL and demo purge tests need the Compose Redis and were not run). Lab 324 passed (the 5 `postgres` marked tests were not run). mypy strict clean on core, trader, lab, and `studies/maker_figures.py`.
