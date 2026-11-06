# 0029. six_vote_trend: rationale

## Context

> ⚠️ Premise note: no strategy tested on 2026-11-06 survived fresh data after fees (about 870 trials across scalpers, swings, carry, momentum, pairs, seasonality, EMA and RSI recipes, candle patterns, and this confluence bundle). This one made money on the development year and lost 12 bps a trade on the fresh year. Running it on demo is a forward test of a failed idea, not a path to live money. That is fine on demo, as long as nobody reads a lucky first week as an edge.

The VPS demo runs `donchian_scalp_r2` on 12 coins. On 2026-11-05 it had a good trending day (+24.55 USD, 48 of 60 trades short, most profit in two hours), and on 2026-11-06 it gave that back in a choppy market (−24.35 USD, 7 of 21 shorts won). A year of backtests says r2 loses about 10 bps a trade after fees. You chose to stop backtesting and test a slower idea live, because the history keeps disagreeing with what you see.

The forces: the trader runs one `PAPER_STRATEGY` at a time; r2's maker base (`bar_rule_maker.py`) is in r2's code hash and must not change; demo has shared risk limits (100 trades a day, a daily loss limit); fees are maker 2 bps and taker 5 bps, so any idea must move more than about 10 bps a trade to stand a chance.

## Options considered

### Option 1: daily pairs mean reversion

Long one coin and short its pair when their price ratio stretches past 2 standard deviations; exit when it returns. **Pros**: the only idea with a strong fresh year (+149 bps a trade on 11 of 12 pairs); market neutral. **Cons**: a coin flip on the development years (2023 −66%); needs two leg orders, which the trader cannot do yet (a much bigger build).

### Option 2: six vote confluence on r2's maker base (chosen)

The six vote bundle on 1 hour bars, all votes agreeing, as a subclass of `MakerBarRuleStrategy`. **Pros**: single leg, reuses every safety path; best development result of the bundle (+70 and +33 bps a trade); few trades. **Cons**: −12 bps a trade on the fresh year; the trail and the maker fallback add untested behavior.

### Option 3: faster confluence (4 of 6 votes, 15 minute bars)

**Pros**: about 1.5 trades a day per coin, so the demo gives an answer sooner. **Cons**: −5 bps a trade after fees in both halves of the development year, and taker fees dominate.

## Rationale

Option 2 fits the one strategy trader and r2's base, so it can run on demo within days, and it is the strongest single leg idea we have. Its fresh year failure is the reason this is a forward test with no gate and no live path. Option 1 has the better recent numbers but needs a two leg order design first; it stays the next candidate if this one disappoints. Option 3 trades enough to learn fast but has no edge even in sample.

You chose the entry, the exits, and the switch:
- **Maker limit, then market.** Saves fees when the market comes back to the close.
- **A 3 ATR stop trailing on every tick, plus the 24 hour exit.** I recommended trailing only at bar closes. The tick trail can stop out earlier than the backtest did. The step (0.1 ATR) and the 5 s minimum between modifies keep it within Kraken's rate limits.
- **Maker for one bar on the time exit, then market.**
- **Switching now**, which ends r2's demo week early.

Judgement calls made in the spec:
- **The `hour_level` kind.** It already exists, so there is no migration. The runner up was `trend`, but that is daily and long only.
- **A seed of 300 candles.** EMA200 needs 200 bars and the volume mean 50, plus a margin.
- **The spread check stays, the volatility floor goes.** A 1 hour ATR is far above 10 bps.
- **The market fallback is skipped when the mid is already beyond the stop.** That avoids entering a trade that is already lost.

### Loss limits off for the demo (amendment 2026-11-06)

At the switch r2 had already hit the demo's daily loss limit (50 USD), so the new strategy would have started halted. You asked for the limits to be raised or removed, since you are busy testing. The settings have no off switch (`risk_daily_loss_limit_usd` must be above 0, and the total limit at least the daily one), so both are set to 1000000 USD, which never trips on demo sizes, and the cooldown to 0. The other caps stay, so one order and the total exposure stay bounded. The cost: a losing day runs on, and since the risk settings are part of the version, the forward test runs on a version that is not the one that would ever go live. That is why the limits go back before the result is judged.

### Backtest evidence (2026-11-06, scratchpad scripts, not committed)

Kraken Futures candles, 12 coins, taker entry at the next open plus 1 bp, 5 bps a side, 3 ATR stop, exit after 24 bars:

| Data | Trades | Gross per trade | Net per trade | Coins net positive |
|---|---|---|---|---|
| Dev H1 (2024-10-19 to 2025-04-19) | 288 | +79.6 bps | +69.6 bps | 8 of 12 |
| Dev H2 (2025-04-19 to 2025-10-19) | 316 | +43.3 bps | +33.3 bps | 9 of 12 |
| Fresh (2025-10-19 to 2026-11-06) | 618 | −2.2 bps | −12.2 bps | 5 of 12 |

Fresh by quarter (net per trade): 2025Q4 −26, 2026Q1 −55, 2026Q2 −12, 2026Q3 +94, 2026Q4 so far −110. The rule was one of 12 bundle versions and about 870 trials that day.
