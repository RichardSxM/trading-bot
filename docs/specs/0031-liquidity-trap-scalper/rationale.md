# 0031 rationale

## Context

> ⚠️ Premise note: this idea joins a long list of scalpers that failed on costs. `book_imbalance` lost before fees, `imbalance_fade` made +0.45 USD a trade against a 1.72 bar, and the later candle scalp searches found nothing. Building the full source spec first would spend the largest effort on the least certain part. The right framing is to test the signal alone, then decide.

The source document, `LIQUIDITY_TRAP_SCALPER_SPEC.md` (not committed), describes a sub minute reversal on Kraken BTC and ETH perps: a sweep of a recent level, aggressive flow with little price impact, displayed depth holding, then a reclaim and a post only entry. Its Stage 0 audit (2026-11-09) found the infrastructure mostly reusable but the feature's core absent.

Forces that shaped this spec:
- **No test exchange.** Kraken's futures demo is gone, so forward testing means Nautilus's sandbox, which ignores queue and latency.
- **Cost.** Maker entry plus taker exit is 7 bps. The source spec's reward to risk rule needs about 22.6 bps with a 5 bps stop. Hold time is 5 to 30 seconds.
- **Clean flow needs two sources.** Nautilus `TradeTick` has no trade type, while the raw context feed carries liquidations. Tardis has no flag at all.
- **Kraken's docs are incomplete.** `qty: 0` is undefined, and the `type` list omits `partialLiquidation`. Facts must be measured.
- **Trial count.** The promotion gate discounts many tries. Each extra setting set makes a later pass weaker.
- **Data.** The laptop holds 55 first of month BTC days and almost no ETH. You chose BTC first.

## Options considered

### Option 1: Pure signal scanner over the lab's hour stream

Run the sweep logic directly over `SegmentStream` events and score mid markouts, with no engine or orders.

**Pros**: fastest honest answer; pure, tested code reusable later; one trial. **Cons**: no fill model, so a pass proves less than a full replay.

### Option 2: A full Nautilus strategy through the backtest engine

Build the state machine and execution, then replay with queue and latency.

**Pros**: realistic fills; closest to a final result. **Cons**: the largest build happens before knowing whether the signal exists; `ScalpConfig` has no sub second timing, so it needs a new execution base.

### Option 3: A scratch script

One script in the session scratchpad, as several past pre checks.

**Pros**: cheapest. **Cons**: untested and unreproducible, and the feature code would have to be written again.

## Rationale

Option 1 fits the cost force: the signal's gross move is the one number that can end the project cheaply, and it needs no fill model. Option 2 would answer a later question first. Option 3 cannot be reused or audited, and this test is meant to be trusted either way. You picked a lab command for that reason.

Other calls made while writing (pick, why, runner up):
- **Reuse Nautilus `OrderBook`** rather than a second book in core. It is already what replay and paper use; a separate pure book was the runner up and would duplicate tested code.
- **Reclaim on mid, not last trade.** A single print can bounce across the level. Trade price was the runner up.
- **`invalidation_buffer_ticks = 5` frozen at the latch.** The source spec leaves the value open; a level that kept moving would never invalidate. A volatility scaled buffer was the runner up, at the cost of another setting.
- **Trailing 60 minute calibration, cold at each segment.** Single days cannot support a time of day normalization. A multi day store was the runner up, but the days are months apart.
- **One signal per horizon.** Overlapping markouts would count one move several times and inflate the sample.
- **Smoke inside the lab image.** It already carries Nautilus and `websockets`, and no new service is needed.

**Independent cross check (Sonnet 5.5, 2026-11-09), applied:** new modules so existing strategy hashes stay fixed; no resubscribe, since spec 0007 proved it brings no `CLEAR`; a runner built book, since the stream's own book runs ahead; `2025-08-01` in place of a day inside the gate's holdout; alignment on `seq` rather than millisecond timestamps; defined event time, ranking, sweep edges, report fields, and verdict corner cases. It also showed the pre check needs nothing from the feed layer, so the pre check now comes first. Efficiency uses `max(0, aligned impact)` instead of the source document's absolute value, because a move against the pressure is the strongest absorption, not the weakest.

Decisions you made: spec covers through the pre check, and the pre check is built first; BTC only first; a 1.5 times cost and 25 a day bar; Tardis labelled approximate with an own data cross check; 12 days with 43 kept unseen; one fixed setting set at 15 s; efficiency percentile; 8 of 12 days positive plus best day removal; depth recorded, not filtered; a lab command; a laptop only smoke. From the audit: live locked off; sandbox only forward testing; Nautilus ticks with liquidations removed by `uid`; a stop point after the pre check.

## References

**Project sources**:
- `AGENTS.md`, `apps/lab/AGENTS.md`, `packages/core/AGENTS.md`
- Spec 0007 (book recording, adapter sequence counter), 0008 (replay), 0009 (liquidations), 0017 and 0021 (pre check shape and earlier results), 0028 (go live)
- `LIQUIDITY_TRAP_SCALPER_SPEC.md` (the source document)

**Practices & standards**:
- Fixing a pass bar before seeing data; chronological evaluation with unseen days held back; fail closed on bad market data

**Links** (confirmed during the 2026-11-09 audit):
- [Book feed](https://docs.kraken.com/api/docs/futures-api/websocket/book)
- [Trade feed](https://docs.kraken.com/api/docs/futures-api/websocket/trade/)
- [Send order](https://docs.kraken.com/api/docs/futures-api/trading/send-order/)
- [Rate limits](https://docs.kraken.com/api/docs/guides/futures-rate-limits/)
- [WebSockets guide](https://docs.kraken.com/api/docs/guides/futures-websockets/)
- [Maker Protection](https://docs.kraken.com/exchange/guides/futures/maker-protection)
- [Changelog](https://docs.kraken.com/api/docs/change-log)
- [Testing environment notice](https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives)
- [Derivatives fees](https://support.kraken.com/articles/360048917612-fee-schedule)
- [EEA derivatives fees](https://support.kraken.com/cn/articles/fees-for-derivatives-trading-eea)
