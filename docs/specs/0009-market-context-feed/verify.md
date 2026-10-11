# 0009. Verify: market context feed

The checks that need real Kraken data or the VPS. Unit and integration tests cover the rest (see the spec's Critical test scenarios). Record each result here with its date (UTC).

## Gate results (Milestone 0)

1. **Native streams.** Cadence of mark, index, and funding updates on `PF_XBTUSD`; the price precision of mark and index; whether non fill trades also arrive as `TradeTick`; whether `FundingRateUpdate.rate` is relative or absolute; the funding period. Result: _pending_
2. **Raw feeds.** `openInterest` present on `ticker`; the `type` values seen on `trade`; `side` is the aggressor; `trade_snapshot` on subscribe. Fixtures saved under the core tests. Result: _pending_
3. **Custom types.** Streamed to feather, read by `read_chunks`, written by `HourSink`, read back by `ParquetDataCatalog` and the `BacktestEngine` with exact Decimals; the two table names. Result: _pending_
4. **Size estimate.** Rows and bytes per day per stream. Result: _pending_

## On the VPS (AC-17)

5. Deploy, then wait 24 h. For every closed hour, check the four always present tables exist (`mark_price_update`, `index_price_update`, `funding_rate_update`, open interest), and list liquidation hours. Result: _pending_
6. Forced drop: block the context websocket for about 90 s (for example `ss -K` on its socket, or a firewall rule on the recorder container's outbound 443 to the websocket host only). Expect `context_feed_down`, `context_feed_up`, one `reconnect` gap for the two raw streams, a `silence` gap only if 60 s passed, the recorder healthy throughout, and no book gap. Result: _pending_
7. Measured rows and bytes per day per table, and the share of the day's catalog size. Result: _pending_

## In the lab (laptop)

8. Fetch two own days recorded after deploy with `scripts/fetch-catalog.sh`, and run `lab replay` on `random_taker` with `use_market_context` on: the report says funding source `own`, and the `market_context` log lines show ages under the max ages. Result: _pending_
9. `lab import-tardis --context` on one already converted Tardis day: three tables, the rewritten `funding_rate_update`, and `manifests/context/<day>.json` written; the gap file unchanged; a second run logs `lab_import_day_skipped`; a replay of that day gives the same funding total as before the backfill. Result: _pending_
