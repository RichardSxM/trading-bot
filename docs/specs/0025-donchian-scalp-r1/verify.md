# Verify: donchian_scalp_r1 · spec 0025 · updated 2026-10-30

## Hash baseline (task 1, AC-5)

The code hash of every `trading_core.node` module closure at 7bd5d2a (its code is main 803f5a6), from `scripts/spikes/hash_check.py` on a `git archive` of that commit. The list sha256 is `f70292e1b5b0099603c0a3892a9db094ef8280465a8cff499adf992ec3dbb1dd`. At close out (task 6) the branch list, minus the new `trading_core.node.donchian_scalp_r1` line, must match it line for line.

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
trading_core.node.paper_snapshots 1645de9b5a53574c4beb4c0d8c4490e5b6ccec5a3a5c44be5b514620fe13b622
trading_core.node.placeholder ec0b98bab810d682114cc70bffbca2ff5e7862f4848441dc270f9b67d14b6713
trading_core.node.random_taker ab290066d43a8e40ba525623fa6f412723d4ff049aba0eb76161ccebb140c4c2
trading_core.node.reconcile e483a2e1e3d7a3d4d82961e79d2c497eddff5708273b48448eafe2e6131e5967
trading_core.node.reconcile_drill 13427e34e8ae73a6a86dfc4161638264fa81a1b84e4eb2e9d4eb635994acb07a
trading_core.node.reconcile_executor 6452e398a20a77a7abbfc3695e036762436ff2243d33b125416aa8e7f4b0e1ad
trading_core.node.recording 1ba1967aa40b00c8c3c2d7e5f64c01763ecd4ddbdd5fafda293bcfdd1e08727b
trading_core.node.sandbox 1d58312ced855746654af75243e0c87cd133e0cbfd1a65f381ab3238c0a75a52
trading_core.node.scalp_base 92b0b3718e3c01ad8a5c9702b3fa9884d0301311262eb198abbf835313da45be
trading_core.node.stale 1822c251d1e9616cef189aef6bf0200407d0547e70505e6f17b62df663bff96d
trading_core.node.strategy 128786a5b6cb990991a819ba636ca9ed53b20355d9135b88410ef09089408a3e
trading_core.node.untrusted 83451616d6433f9f6d3d9b1a329ec7ff2c9a956821d537e76ad911d3ded7ceec
trading_core.node.vwap_retest 44083de39ddf9f1b452b041dc81f1017ea3ee070315104e4131f52789fefdc52
```

## Build evidence (2026-10-30, /develop)

- Tests at fbba014 (`kfs/*:s0025t`, dev target): core 2,046 passed, 5 skipped (this feature: 22 node, 13 rule, 19 seed check tests); trader 145 passed; lab 317 passed. mypy strict clean on every package; pre-commit clean.
- AC-11: `python -m trader.version` with `PAPER_STRATEGY=donchian_scalp_r1` on `kfs/trader:s0025` prints name `donchian_scalp_r1`, kind `scalping`, every setting with `ema_period` 200 and `spread_max_atr` `"0.1"`, frozen hash `c9772911…`, commit fbba014.
- AC-7 on real data: `warmup_seed()` in the same image at 17:25:19 UTC fetched 600 complete candles in 0.78 s, the last closing 17:25:00 UTC (the in progress candle dropped). Fed into `DonchianMidlineRule(20, 3, 200)` and ATR(14), the rule was ready with `ema` 77786.33, `midline` 77326.0, `entry_high` 77592.0, `entry_low` 77060.
- Not yet run: the laptop trader start (tasks 2 and 5 smoke). On demo it needs a manual `candidate → paper` promote of r1's version first, which `gate promote --manual` takes only from a person at a terminal.
- Build order note: the spread check and the seed join were written into the node module before the screens ran, so the screens record the node's final code hash (filter off and an empty seed in the lab, so the screened behavior is the same).
- Task 7 closure check (AC-5 amendment): `code_closure` over every `trading_core.node` module shows `node/recording.py`, `node/paper_snapshots.py`, `node/context_feed.py` and `lab/candles.py` in no strategy's closure. They appear only in the closures of `node.recording` and `node.paper_snapshots`, the trader side actors. Tests after task 7: core 2,057 passed, 5 skipped (10 new two coin node tests); trader 161 passed; lab 317 passed; mypy strict clean.
- Task 8 (AC-20): `warmup_seeds` fetches each coin's candles from its own symbol, one coin after another; a coin that fails gets an empty seed while the others still seed, and `warmup_seed_failed` names the `instrument`. Trader 163 passed (2 new seed tests); mypy strict clean.

## Lab screens (task 3, AC-13), 2026-10-30

Both studies ran on `kfs/lab:s0025` (built at fbba014) from the main checkout's lab catalog, 1,297 days of 5 minute candles from 2022-04-01, `LAB_WORKERS=2`. The studies took 1,892 s and 1,990 s; the screens took 17 s and 16 s. Batches: baseline `01a18dd7-6d03-7668-9ec2-aa9180411daa`, r1 `01a18df4-8d0e-7032-9c1f-958271e3eed2`. "Daily Sharpe" is the screen's annualized walk forward Sharpe over the development runs (`candle_walk_forward_sharpe`).

| Figure | donchian_scalp | donchian_scalp_r1 | r1 minus baseline |
|---|---|---|---|
| Screen verdict | fail (walk_forward_sharpe, fee_hurdle, trades_per_day) | fail (walk_forward_sharpe, fee_hurdle, trades_per_day) | same |
| Trades | 5,382 | 5,002 | −380 |
| Trades per day (minimum 17) | 4.15 | 3.86 | −0.29 |
| Net P&L (USD, from 10,000) | −9,996.02 (−99.96 %) | −9,991.05 (−99.91 %) | +4.97 |
| Gross P&L per trade (USD) | −1.0636 | −1.0031 | +0.0605 |
| Cost per trade, round trip (USD) | 0.7940 | 0.9953 | +0.2013 |
| Daily Sharpe (walk forward) | −3.9288 | −5.0948 | −1.1660 |
| Max drawdown | 99.96 % | 99.91 % | −0.05 points |
| Win rate | 20.94 % | 18.79 % | −2.15 points |
| Fees total (USD) | 4,273.41 | 4,978.65 | +705.24 |
| Profit factor | 0.368 | 0.420 | +0.052 |

Buy and hold over the same span, before costs: +135.13 %.

Read this with care: both accounts were drained to near zero long before the end of the span, so most of the late period has no trades and the totals mostly describe 2022 to 2023.

- Baseline: sizing first returned zero quantity on 2023-03-12. From 2023-10-15 a stop rejected as "in the market" set a permanent `blocked:stop_not_accepted`, and the node refused every entry after that.
- r1: zero quantity first on 2023-06-30, and the same permanent block from 2023-10-15.

So the r1 minus baseline column compares two strategies that each lost the account. It does not measure the EMA filter and the midline exit over a full span. Neither change turned the gross edge per trade positive. The fee hurdle (gross per trade must exceed 1.5 times cost) fails by a wide margin for both.

AC-13a (spread filter skip rate on the VPS quote ticks) is not run yet. Task 3 stays open until it is.
