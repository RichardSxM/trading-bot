# 0020 rationale: intraday trend candidates

## Context

Three families have now failed the promotion gate for the same reason: the gross edge per trade was at or below the cost of a round trip (about 14 bps: the 0.05% taker fee each side plus 2 bps slippage each side). Seconds scalping (spec 0017) aimed for moves about the size of the fee. The 1 hour `connors_rsi` (spec 0018.1) had too few trades. At 15 minutes (spec 0019), `connors_rsi_15m` earned 3.64 USD gross per trade against 6.44 USD cost, and `ema_pullback` lost money before costs over 706 holdout trades. The hour level family now stands at N 16, so every new trial makes a pass harder (deflated Sharpe).

Every failed rule had a fixed target or a quick mean reversion exit, so its average winner was capped near the cost. The direction that clears the cost has to let winners be many times bigger than the cost, while still trading at least 60 times in the holdout year (the candle gate's floor) so the verdict is not `insufficient`.

The forces: the frozen spec 0018 base (one position, market orders, a resting stop, risk sizing) must not change, or every earlier `code_hash` moves; only free data (the stored Kraken 1 minute candles since April 2022); the same holdout year (2025-10-19 onwards) and gate thresholds; and an honest record of every trial.

The trader asked for a web search before choosing a direction (2026-10-26). Its findings are below, under *Research summary*.

## Options considered

### Option 1: Resume spec 0018's remaining 1 hour candidates

Run `bollinger_rsi_1h`, `nr4_breakout`, `failed_breakout`, and the layered RSI plus MACD plan, all already specced.

**Pros**:
- Nothing new to design; the specs and the base exist.

**Cons**:
- The same idea class (fixed targets, mean reversion, short holds) that just failed three times on the cost; each adds trials to N with little expected gain.

### Option 2: Trend following, intraday noise area breakout plus a 4 hour Donchian channel (chosen)

Bands from the day's open sized by the average intraday move, entries on breaks with a VWAP check, a trailing exit and no target; then a classic channel breakout on 4 hour bars.

**Pros**:
- The most recent published evidence of any option (Concretum's BTC intraday trend work, the peer reviewed BTC intraday momentum study, and the "Catching Crypto Trends" Donchian results after fees).
- No fixed target, so winners can be many times the cost; fits the frozen base with only rule exits.
- The intraday rule trades often enough for the 60 trade floor.

**Cons**:
- Published intraday results are gross of fees and not peer reviewed; unlimited re-entry can pay the cost repeatedly on choppy days.
- The 4 hour Donchian may get `insufficient` from too few trades.

### Option 3: Funding and positioning signals

Fade extreme funding rates or open interest build ups.

**Pros**:
- New information, not price again.

**Cons**:
- Kraken's public funding history starts 2025-10-15 (checked 2026-10-26: 9 016 hourly rows from 2025-10-15T08:00Z), which is only the holdout year, so there is nothing to develop on. Binance's free archive is longer but is another venue's signal. No study found shows funding predicts BTC price returns, and funding carry turned negative in 2025.

### Option 4: Cheaper execution with maker orders

Keep hour level rules but enter and exit post only.

**Pros**:
- Cuts the round trip from about 14 to about 4 to 9 bps; much of the code exists (`exit_mode = "maker"` in the scalping base).

**Cons**:
- A 2025 peer reviewed study (Albers et al., Bybit and Binance) shows maker fills are adversely selected and backtests that ignore it fail live; the candle lab cannot model queue position, so results would be overstated.

## Rationale

Option 2 is the only direction that changes the shape of the payoff rather than the cost or the signal source. The failed rules capped their winners near the cost; trend rules let a few large moves pay for many small losses, which is exactly what the 14 bps hurdle needs. It is also the one with the most current evidence, and it fits the forces: no base change (the trailing exit is a rule exit on bar close, the base's resting stop still guards every position), no new data, no gate change.

Within trend following, the noise area rule was chosen first because it has the fewest settings (one band width, one lookback) and trades often enough for a real verdict, while the 4 hour Donchian covers the slower, better documented multi day trend at a timescale nobody has tested here. Daily bars were rejected because 5 to 15 trades a year cannot pass the 60 trade floor, and lowering the floor after three failures would be moving the goalposts.

The trader chose unlimited re-entry and holding past midnight (with an 8 hour time stop), both departures from the published design. Unlimited re-entry raises the whipsaw cost; the checkpoint and the exit reason shares in the results will show whether it hurt. Holding past midnight would be killed by the daily band reset, so the rule freezes the entry day's lines overnight (the trader's pick over switching lines, which would quietly recreate a day end).

## Research summary (2026-10-26 web search)

| Idea | What the sources say | Fit here |
|---|---|---|
| Intraday trend (noise area) | Concretum: BTC intraday trend ensemble, gross Sharpe about 1.6 over 2018 to 2025 at a 20% volatility target, rules not published. The rules used here come from the same authors' SPY paper ("Beat the Market", 2024): bands from the open by the 14 day average move per time of day, checks every 30 minutes, exit at the larger of band and VWAP. University of Reading: BTC intraday time series momentum holds out of sample. | Chosen (candidates 1 and 2) |
| Time of day | Concretum: strongest from Sunday about 19:00 New York into Monday (the Asia open), negative on US Sunday daytime. Quantpedia (Gemini, 2015 to 2022): 21:00 to 23:00 UTC best. | Filter only (candidate 2) |
| Daily trend | Mixed: one study's Sharpe fell from 0.66 in sample to 0.06 out of sample; at 15 bps cost time series momentum nearly vanishes in bear markets; long only beat long and short. | Rejected (trade floor) |
| Multi day Donchian | "Catching Crypto Trends" (SSRN 5209907): Donchian ensemble, Sharpe above 1.5 after fees on a 20 coin rotation. | Chosen (candidate 3, single lookback) |
| Funding | Funding is forecastable, but no study shows it predicts price; carry Sharpe turned negative in 2025. | Rejected (no history) |
| Open interest, liquidations | Commentary only, no tested signal. | Rejected |
| Maker execution | Adverse selection of maker fills makes backtests too rosy. | Rejected for now |
| Volatility targeting | Good general evidence, weak for BTC alone. | Not a strategy; risk sizing already scales with stop width |

## References

**Project sources**
- Specs 0015 (gate thresholds, trade floor, fee hurdle), 0017, 0018, and 0019 (failed families and their results; the frozen base; the run protocol and checkpoint), 0014 (candle lab bars and fill order).
- `packages/core/src/trading_core/node/bar_rule.py` (`on_bar` skips exits while `ready()` is false; no entry on an exit bar) and `strategy/bar_rule.py` (`floored_stop`).
- The Kraken historical funding endpoint check of 2026-10-26 (first row 2025-10-15T08:00Z).

**Practices & standards**
- Deflated Sharpe ratio and family trial counting (spec 0018 AC-19).
- Turtle style Donchian breakout with half length exit channel.

**Links** (from the 2026-10-26 search)
- Concretum Group, "Seasonality in Bitcoin intraday trend trading": https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- Zarattini, Pagani, Barbon, "Catching Crypto Trends" (SFI Research Paper 25-80): https://ideas.repec.org/p/chf/rpseri/rp2580.html
- "Bitcoin intraday time series momentum" (University of Reading, accepted version): https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf
- "Time series and cross sectional momentum in the cryptocurrency market" (15 bps costs): https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf
- Quantpedia, "Are there seasonal intraday or overnight anomalies in Bitcoin?": https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/
- "Cryptocurrency as an investable asset class" (carry trade Sharpe by year): https://arxiv.org/pdf/2510.14435
- Inan, "Predictability of funding rates" (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5576424
- Albers et al., maker fill adverse selection (Quantitative Finance, 2025): https://ideas.repec.org/a/taf/quantf/v25y2025i6p919-947.html
- Kraken, historical funding rates API: https://docs.kraken.com/api/docs/futures-api/trading/historical-funding-rates
- "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)" (Zarattini, Aziz, Barbon, 2024): cited by name, link not verified.
