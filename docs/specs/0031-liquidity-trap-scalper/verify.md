# Verify: liquidity trap scalper · spec 0031 · updated 2026-11-09

_Build record from `/develop`. The verify steps for `/check verify` are added at the end of the build._

## Build record

### Task 1: hash baseline (AC-1), 2026-11-09

`scripts/spikes/hash_check.py` ran over a clean `git archive` of commit 2ed476e, the commit this branch's code starts from, before any code was written. This is its full output, 43 modules. At each close out the same list has to match line for line, and the only addition allowed is `trading_core.node.lts_feed`.

```
trading_core.node.bar_rule ac5797ffdbeff419601a7423b2ee941096c5135c67063e7e66a71bd248b2fe7b
trading_core.node.bar_rule_maker e5eb1a8feb28fa8aaf2ffd25ab4a7321fe7c5c6187dd8619a692a1d4369a495a
trading_core.node.bollinger_rsi 3fd0f0185bb30203de1c280bd206f94ebf131ae5f4e0d8ddf9842f73ea02bba2
trading_core.node.book_imbalance 2c6b803f81b334a8e288988968da9618049ddadc083e5b3d37934fee7ba79aab
trading_core.node.buy_and_hold 3ec96a7663c3f77f9ee6f78ff546de4cb8b015fe345072719c29b987c5d7198b
trading_core.node.connors_rsi 6f423be8e65c7ba82e95b15a9ba427b4ec2d0312ac6d7a3c18eb7b7d55d9027a
trading_core.node.context_feed a379e7f27522a42a75f931472559bab0c5783b3bb839ea322997cd075868ab0c
trading_core.node.context_types 8747b288db8a0073f300b2afa50f622ede8257061ddb85e211d6659542472cd9
trading_core.node.control 1039f47a5ce8df883ac51608657ac44bfca285092903efd76fa65922de72fbb7
trading_core.node.delta_divergence 37406d5c4cfc82f41a961511f15ae81348b3af72ed48c0e0d56de861dadd3ab3
trading_core.node.donchian_4h 668c5e46c28c243b39c1f4e5a1dfef4694b44e6704b2262ee4c06cd4285f6492
trading_core.node.donchian_4h_r1 a6b324422d61bba0ff719d140031970aaeeab3b72caaef8b9ee845f78e7a1103
trading_core.node.donchian_scalp e189ce8871f9aba615955e5348d7275b783f77cdec2c8bb86134eec4deb7eb88
trading_core.node.donchian_scalp_r1 fd6f69f57d76027bad04bf74ce95bce307f7a1fdf90feb34622feb3d5d84f68b
trading_core.node.donchian_scalp_r2 aacac71dec2d4e7571d1667f9a06dd631dfc21d783c217e9274b326fd0df4233
trading_core.node.ema_pullback d52e6eac09911cc0ab073775b157d756eea3661fee2ba1e685bae04a4fbba360
trading_core.node.funding 2d17e1a332a3866aa1a36c2223af26267a2d94abdf20626b0228fed1f5bcb6e4
trading_core.node.guard 19c474725bd461f19dcc472047e89e2d46c771cb1c22708a1e2420e8f9c9524e
trading_core.node.heartbeat 46e360bcd780b2e2f3b640e7cd7246019b352aed5b9df064cd3f144f9b0a9003
trading_core.node.imbalance_fade a044cf0cb2c35ebb72162ddf2eca25dabd44640d6604411d672f6113d613663d
trading_core.node.key_actors 726764267181c0103ffb8c26b6f6a2b1807f557d8dfeda768ce3d5d5b037d017
trading_core.node.key_check 97e7f68a7a8a62c95a69e024f7ab287af1bb71bbef16dbd9b3a9b018c59b2c05
trading_core.node.kraken 57a8abe320bebd0f97b5f576a82637ff5740d55c9cc863b1fc0282faad9c232c
trading_core.node.loss_limits 044fc2fbc651dabee705e4f8ff2f9f62218ed167bd4d347e58e3f3376ac219a3
trading_core.node.macd_baseline 3d5d6b3d0a03087f6c2c9e8b03c2e0a31e1599635c6b558470fe10359dc97227
trading_core.node.maker_probe 5939294761b3aaa6bf2ba5d211b1e75df373cf114898abaf8fc2e5e1bb9c2dee
trading_core.node.noise_breakout e930c369b7bbecec81db5f16fe715a5c0d6e7be7bd049ad7ccb64e4aae302940
trading_core.node.paper_snapshots d9440bb92807e79354c7e8a0bdd0a92307a43066b05ffe2610e3c98199baca6b
trading_core.node.placeholder 15f5c90f9ea2960bf470443deebd138eaf0aeac28e2c6b5d1eb8a709fbc0f8da
trading_core.node.random_taker 344cc36135ec65fb6b34e4bb20b3cb9d1f5268bf1e8cb321e19fe94638239931
trading_core.node.reconcile e7bb7f304126d9cfc3ff0e9641c89b8e9cabe3f9b3eded69adffcb4b83bc3a95
trading_core.node.reconcile_drill e490708f40b586495e4d2dda34892d98fbe52df7ced865121b904541ed763a93
trading_core.node.reconcile_executor 0dc39c254ff59dd7b39b0409800be652d9652715fa090b32b10f886c0fbd95e6
trading_core.node.recording 5b53df1baa29832773ed269028bc4af9fabaabb6917c45273f80768b275fba7a
trading_core.node.sandbox 1d58312ced855746654af75243e0c87cd133e0cbfd1a65f381ab3238c0a75a52
trading_core.node.scalp_base b2e73dc3ba0f7cad9579bd5bf4143158dee2bf4e63df94e763bbd6eabb62f1ad
trading_core.node.six_vote_trend 1358dfa54777bf3db8126de1a29b09c1e0b093bf26a374b6119e8f9e9c68733e
trading_core.node.stale 3e58319449bd14d7fd2d591ef89b5d3319ebe666e6947d45df9a2237067cbcfa
trading_core.node.strategy 3cc7b1839aa00c92ac072ffffef8c8bd72f3f2064dec004e3e5d6e9ff90753af
trading_core.node.trend_portfolio e91984ce91b305a3af58c00e4d6efad9c9c07234ebc764d723faac1f182b5944
trading_core.node.tsmom 7491c2697bb5254c83893abfcbe36f8bf0076a877b9e11ad8b377a8bc5b787ce
trading_core.node.untrusted 88721b15ee5133e9e3550e126f8a3c40cb1489edc6d832e6f5e3e2dc590fb49a
trading_core.node.vwap_retest f82f0337e2f1d6e2e32a8105ac96596f415f6e1e0861b22ab0cce92492bce0e0
```

Two facts were checked in the same step, reading only columns the signal does not use:

- A Nautilus `BookLevel.size()` is a float, so depth is summed from the level's `orders()` as `Quantity` values.
- Across the 12 frozen Tardis days, all 476,346 trades have an aggressor side of buyer or seller, none have a blank trade id, and no id repeats inside an hour. So a trade with no aggressor is correctly treated as a data fault.

### Tasks 2 to 4: signal code (AC-2, AC-3, AC-10 to AC-14), 2026-11-09

`LtsSettings`, trade normalization with the `TradeTick` converter, `FlowWindows`, `EfficiencyRanks`, depth tracking, and `SweepDetector` are built and tested. The mirror test and the cut stream test are in `packages/core/tests/test_lts_sweep.py`.

One reading to check: the spec says the detector leaves `warming` once `EfficiencyRanks` is warm, and warmth is per side. The code treats that as both sides warm before any sweep can start, because a sweep needs the opposite side's history later and the two sides warm at nearly the same time.

### Task 5: a speed fix before the first run, 2026-11-09

A practice run on our own data took more than 5.5 hours on one day, because every book update walked the whole book for candidate depth. Commit `b781b69` re reads only the levels an update names, still from the runner's own `OrderBook`. A full practice day then took 622 s. On real data the fast read matched a full walk in all 34,800 comparisons, and unit tests cover the same. No Tardis day was read for this.

### Task 6: the run, the recorded hour, the cross check (AC-14, AC-17, AC-18), 2026-11-09

The verdict and the cross check are in the spec's Results section.

No look ahead, on one recorded hour of our own data, 2026-10-09 11:00 UTC, with `scripts/spikes/lts_no_look_ahead.py`. It runs the whole segment, cuts the stream at three times in that hour, and compares the signals only, because depth after a signal and markouts look forward by design.

| Cut (UTC) | Signals, all trades | Signals, liquidations removed | Same as the full run |
|---|---|---|---|
| 11:05:30 | 1 | 1 | yes |
| 11:40:00 | 7 | 7 | yes |
| 11:59:59 | 12 | 12 | yes |

The VPS copy was 3.78 GB at low priority and at most 4 MB per second, and took 944 s. The VPS held one more liquidation hour file than the manifest counts on 2026-11-05 (5 files, 4 hours) and on 2026-11-08 (2 files, 1 hour). Nobody has looked into why. The cross check is descriptive, so it does not touch the verdict.

The reading noted under tasks 2 to 4 (both sides warm before a sweep can start) may explain the four days with no candidate. It cannot change this verdict.

### Task 7: hash comparison (AC-1), 2026-11-09

`scripts/spikes/hash_check.py` on the image at `b781b69`, compared with the 43 lines above: all 43 are identical, and the one new line is `trading_core.node.lts_feed`. On a `fail` the phase 2 tasks 8 to 11 are not built.

Tests at `b781b69`: lab suite 379 passed. Core suite 2517 passed and 1 failed, `test_a_full_queue_overflows_counts_as_a_failed_write_and_spills_after_drain_in_order` in `test_recording_writer.py`. That file passed 5 of 5 reruns (9 tests each), and this branch changes no recording file, so the failure is a timing flake unrelated to this feature.
