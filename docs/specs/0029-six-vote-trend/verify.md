# Verify: six_vote_trend · spec 0029 · updated 2026-11-06

## Hash baseline (task 1, AC-11)

The code hash of every `trading_core.node` module closure at b58f5dc (the spec commit, code equal to main 1dd44bd), from `scripts/spikes/hash_check.py` on a `git archive` of that commit. 41 modules; the list sha256 is `810c10e335bb461dff6bda10b549e9dc8bc00449dd8d6eca7934c7eb6243551a`.

```
trading_core.node.bar_rule ac5797ffdbeff419601a7423b2ee941096c5135c67063e7e66a71bd248b2fe7b
trading_core.node.bar_rule_maker e5eb1a8feb28fa8aaf2ffd25ab4a7321fe7c5c6187dd8619a692a1d4369a495a
trading_core.node.bollinger_rsi 3fd0f0185bb30203de1c280bd206f94ebf131ae5f4e0d8ddf9842f73ea02bba2
trading_core.node.book_imbalance 2c6b803f81b334a8e288988968da9618049ddadc083e5b3d37934fee7ba79aab
trading_core.node.buy_and_hold 3ec96a7663c3f77f9ee6f78ff546de4cb8b015fe345072719c29b987c5d7198b
trading_core.node.connors_rsi 6f423be8e65c7ba82e95b15a9ba427b4ec2d0312ac6d7a3c18eb7b7d55d9027a
trading_core.node.context_feed 6dc6991676f491f373c26c295441523edad1a0b6158686def9246ae8079004ba
trading_core.node.context_types 8747b288db8a0073f300b2afa50f622ede8257061ddb85e211d6659542472cd9
trading_core.node.control 261bbed48c41f984881c3c6d29760b42a6eef35b9a85e3887862238272e1f4a6
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
trading_core.node.loss_limits ccb859bafa832eced58e65332346fcd10a13114577c300c48736db321df19e16
trading_core.node.macd_baseline 3d5d6b3d0a03087f6c2c9e8b03c2e0a31e1599635c6b558470fe10359dc97227
trading_core.node.maker_probe 5939294761b3aaa6bf2ba5d211b1e75df373cf114898abaf8fc2e5e1bb9c2dee
trading_core.node.noise_breakout e930c369b7bbecec81db5f16fe715a5c0d6e7be7bd049ad7ccb64e4aae302940
trading_core.node.paper_snapshots 507a5b56d4f167ef65bf76878d8ddb38a7c01322f284e5dcf9179947460f4eb1
trading_core.node.placeholder 15f5c90f9ea2960bf470443deebd138eaf0aeac28e2c6b5d1eb8a709fbc0f8da
trading_core.node.random_taker 344cc36135ec65fb6b34e4bb20b3cb9d1f5268bf1e8cb321e19fe94638239931
trading_core.node.reconcile 9d98927e0fb88ec844cb3103c6c34063cd20979329d5fec72dff55a64e11da60
trading_core.node.reconcile_drill c32b6afe1f54004d7b973a493095db1a799f191b2008e94eb07ecd3524f65d3e
trading_core.node.reconcile_executor 0dc39c254ff59dd7b39b0409800be652d9652715fa090b32b10f886c0fbd95e6
trading_core.node.recording 5b53df1baa29832773ed269028bc4af9fabaabb6917c45273f80768b275fba7a
trading_core.node.sandbox 1d58312ced855746654af75243e0c87cd133e0cbfd1a65f381ab3238c0a75a52
trading_core.node.scalp_base b2e73dc3ba0f7cad9579bd5bf4143158dee2bf4e63df94e763bbd6eabb62f1ad
trading_core.node.stale 3e58319449bd14d7fd2d591ef89b5d3319ebe666e6947d45df9a2237067cbcfa
trading_core.node.strategy 3cc7b1839aa00c92ac072ffffef8c8bd72f3f2064dec004e3e5d6e9ff90753af
trading_core.node.tsmom 7491c2697bb5254c83893abfcbe36f8bf0076a877b9e11ad8b377a8bc5b787ce
trading_core.node.untrusted 88721b15ee5133e9e3550e126f8a3c40cb1489edc6d832e6f5e3e2dc590fb49a
trading_core.node.vwap_retest f82f0337e2f1d6e2e32a8105ac96596f415f6e1e0861b22ab0cce92492bce0e0
```

### Close out check (task 4)

The same tool on bb5f863 (the last code commit): 42 modules. The only line not in the baseline is the new one below. With it taken out, the list matches the baseline line for line, with the same sha256 (`810c10e3…`). `bar_rule_maker` and `donchian_scalp_r2` keep their baseline hashes, and `git diff b58f5dc..HEAD` touches neither file. AC-11 passes.

```
trading_core.node.six_vote_trend 1358dfa54777bf3db8126de1a29b09c1e0b093bf26a374b6119e8f9e9c68733e
```

## Seed volume unit (task 2, AC-8)

The Kraken charts API's hourly `volume` is in coins: summed over a day it matches `vol24h` from the tickers endpoint, which is in coins. Trade built bars sum trade sizes, also in coins. So the seed needs no conversion, and V6's 50 bar volume mean mixes seed and live bars in one unit.

## Stop modify spike (task 3, AC-5)

The Kraken adapter's `_modify_order` (Nautilus 1.231.0) sends `trigger_price` alone to `editorder`, with the quantity only when given. It emits `OrderUpdated` on success and `OrderModifyRejected` on failure, and it handles a changed venue order id. The laptop and VPS demo trader trade on Nautilus's simulated exchange, not the adapter, so the trail is proven there by backtest tests (`test_node_six_vote_trend_orders.py`). A real Kraken stop modify is only proven on live (feature 19).

## Laptop smoke (task 2)

Built with `GIT_COMMIT=$(git rev-parse HEAD)` (a laptop build without it hits `RecordConflictError`, since the version key is commit plus config). You promoted version `01a1b07a-cda2-7016-8053-f056a034a3e6` by hand, then the trader ran with `PAPER_STRATEGY=six_vote_trend` on BTC and ETH from 2026-11-06 12:55 UTC (13:55 Amsterdam):

- `warmup_seed_fetched` for both coins: interval `1h`, 300 bars.
- At the first live close (13:00 UTC, the join bar): `btc_seed_fed` ready, `warmup_seed_fed` 300 bars ready for both instances.
- At 14:00 UTC (15:00 Amsterdam): `six_vote_values` for both coins with `score` and all six votes. Both scored −4 (trend, BTC, EMA, RSI short; breakout and volume 0), V2 −1, so no entry.
- No ERROR lines, no `seed_stale`, no `btc_bar_late`.

The stack came down after.

## VPS switch (task 4, AC-12)

PR #129 merged as `c6a9548`. Deployed with `scripts/deploy.sh` (`--no-deps`), done at 2026-11-06 15:35:45 UTC (16:35 Amsterdam), previous release `45d4b3b`.

**Changes to `/etc/kfs/kfs.env`** (backup at `/etc/kfs/kfs.env.bak-20261106-sixvote`):

- `PAPER_STRATEGY=six_vote_trend` (was `donchian_scalp_r2`). `PAPER_INSTRUMENTS` unchanged, the 12 coins with BTC.
- **AC-12 knowingly broken at your request:** the `RISK_*` lines did not stay unchanged. r2 had hit the daily loss limit that day, and you asked for the loss limits to be off for demo testing. So `RISK_DAILY_LOSS_LIMIT_USD=1000000` (was 50), plus new lines `RISK_TOTAL_LOSS_LIMIT_USD=1000000` (was the compose default 100) and `RISK_LOSS_COOLDOWN_S=0` (was the default 600). Every other `RISK_*` line is unchanged. Put them back from the backup before any real evaluation.
- r2's halt row (`risk_events` id `01a1b16d-5132-7259-85f3-49498dde82c9`, `daily_loss_limit`, 2026-11-06 15:14:53 UTC) was saved to `/etc/kfs/halt-row-20261106.json` and deleted. The trader still started halted, because the halt also lives in the trader's own state. It flattened, and was resumed at 15:35:47 UTC. `loss_seeded` then showed `daily_tripped` false.

**Versions.** Since `RISK_*` is part of the version config, each change made a new version:

- `01a1b177-a271-7437-b8c0-c57a4265b64f`: promoted by hand, then unused (the cooldown change came after it).
- `01a1b17a-dabf-7614-811a-90d1319c1bd4`: promoted by hand (`candidate → paper`). This is the version running.

**Runs.**

- r2's last run: `01a1add5-ed87-772b-90a8-b5a25e512b54` (version `01a1adbb`), from 2026-11-05 22:30:40 UTC to 2026-11-06 15:34:57 UTC (16:34 Amsterdam), status aborted. This ends r2's demo week early.
- six_vote_trend's first run: `01a1b17f-b083-748d-abde-b19ca9e532e3`, started 2026-11-06 15:34:57 UTC (16:34 Amsterdam).

**Start checks.**

- At start: `warmup_seed_fetched` with 300 one hour bars for all 12 coins. No `warmup_seed_failed`, no `seed_stale`.
- At 16:00 UTC (17:00 Amsterdam, the join bar): `warmup_seed_fed` 12 times, `btc_seed_fed` 11 times (every coin but BTC). No ERROR, no CRITICAL.
- At 17:00 UTC (18:00 Amsterdam, the second live close): one `six_vote_values` line for each of the 12 coins, each with `score` and all six votes. V2 is −1 on every coin (BTC under its EMA 200), so BTC's vote is ready and gets through. No entry: scores ran from −4 (UNI) to +2 (ZEC). No ERROR, no CRITICAL, no `seed_stale`, no `seed_failed`.
- The BTC warning `Aggregator for PF_XBTUSD.KRAKEN-1-HOUR-LAST-INTERNAL is currently in use` is expected: the instances share one aggregator. The `context_message_bad` warnings (`unknown_trade_type`) come from the market context feed, not this strategy.
- The coins showing on the dashboard Overview was not checked here. It is left for `/check verify`.

## /check verify (2026-11-06, PASS after the AC-12 amendment)

Checked at about 17:58 UTC (18:58 Amsterdam) on main 659be51, release c6a9548 on the VPS:

- AC-2, AC-10: version `01a1b17a` is `six_vote_trend`, kind `hour_level`, with `trail_atr` 3, `time_stop_bars` 24, `entry_wait_bars` 1, `spread_max_atr` 0.1, `btc_instrument_id` `PF_XBTUSD.KRAKEN`, and 12 instruments. Run `01a1b17f` is running, mode paper.
- AC-10 (Overview): `demo:snapshot:positions`, which the Overview cards read, lists all 12 coins. `trader_state` is ACTIVE, not paused, on c6a9548.
- AC-3, AC-8, AC-8a: the start checks above. No `btc_bar_late`, no ERROR or CRITICAL since the start, and the only risk event is the resume.
- AC-1, AC-4 to AC-7, AC-9: no trade yet (0 decisions), so these were proven by tests on main: core `test_strategy_six_vote.py`, `test_node_six_vote_trend.py`, `test_node_six_vote_trend_orders.py` 70 passed; trader `test_paper_six_vote.py`, `test_paper_strategy.py`, `test_warmup.py` 36 passed. The first live trade is the first real run of the fallback, the trail, and a decision row.
- AC-11: `hash_check.py` on main gives 42 lines, the baseline plus `trading_core.node.six_vote_trend 1358dfa5…`; `bar_rule_maker` and `donchian_scalp_r2` unchanged.
- AC-12 (amended): met as amended, with the backup, old values, and halt row file named in the VPS switch section.
