# 0032. trend_daily: decision record

## Context

The main demo trader runs `donchian_scalp_tp`, a 20 bar breakout on 5 minute bars with $500 trades and dollar exits. It has no edge. A replay on 150 days of Kraken 1 minute candles (2026-06-14 to 2026-11-10, 12 coins) lost about $0.66 a trade after costs in both halves, with every filter tried (30 day and 7 day trend, BTC's 30 day trend, a 200 hour EMA, ADX, volume) and with a $2 or $3 stop. Live, the trailing stop version lost $189 over 223 trades from 2026-11-10 13:00 to 2026-11-11 11:00 UTC, all 12 coins negative, $111 of it fees. Costs are about 14 bps a round trip, and the breakout earns about 1 bp before them.

The 30 day trend is real but slow. Signed by the 30 day trend, the average forward return was +0.1 bps over 5 minutes, +1 to 2 bps over 1 hour, and +25 to 40 bps over 1 day (8 of 12 coins). A 30 day trend portfolio held up on Binance 2018 to 2022 (Sharpe about 1.2 to 1.4, every year positive), which is why `trend_portfolio` (spec 0030) exists. Against that: spec 0027's daily trend candidate failed its gate, a check on 2026-11-10 found the live 30 day Monday rule negative on the most recent year and wiped out at 5x, and an earlier test found hourly rebalancing lost to weekly while weekly results swing by rebalance day.

Forces: the VPS (2 CPUs, 3.8 GB) fits one trader; trader-portfolio was turned off on 2026-11-10 for load. The main trader carries the deploy, dashboard, and alert plumbing. The sandbox starts at 1000 USD and resets on every restart. The owner wants something better than the scalper now, on the demo, and chose the daily rebalanced book, 5x, every change traded, no stops, no brake, no history test, no review date.

> ⚠️ Premise note: the owner chose to skip a history test. The two closest prior checks (spec 0027's daily trend gate, and the 2026-11-10 fresh year check of the 5x book) were negative, so a forward test that looks good or bad in its first weeks says little either way. The spec keeps the demo run but names the study owed before live money.

## Options considered

### Option 1: thin `TrendDaily` subclass on the main trader (chosen)

A new module inherits `TrendPortfolio` and overrides only the daily timer so it rebalances every day.

**Pros**: reuses the tested rule, orders, retries, seed, and snapshots; small diff; `trend_portfolio`'s frozen version is untouched.
**Cons**: needs two plumbing fixes for a book on main (deploy flat wait, Portfolio page slot); couples the new module to the base's private `_on_daily` method.

### Option 2: run `trend_portfolio` itself on trader-portfolio, main off

Turn the portfolio profile back on, add a daily setting, switch the main trader off.

**Pros**: the Portfolio page and deploy already handle that slot.
**Cons**: a daily setting edits `trend_portfolio`'s closure (a new version of a frozen strategy); the deploy, Overview, and alerts assume main is on, so main "off" is a new state of its own.

### Option 3: a copied module (like `donchian_scalp_tp`)

Copy `trend_portfolio.py` into `trend_daily.py` and change the schedule.

**Pros**: fully independent hash, no reliance on a private method.
**Cons**: about 1000 lines duplicated; every fix to one must be made twice.

## Rationale

Option 1 fits the forces best: the only behaviour change the owner asked for is the schedule, so inheriting everything else keeps the tested order handling and costs the least code. Running it on main keeps the deploy, Overview, P&L, and alerts working as they are, which matters on a box that fits one trader. The `_on_daily` coupling is acceptable because the base is frozen by its version hash: it cannot change under the subclass without a new `trend_portfolio` version, which the build's hash check would show. If the subclass cannot be written without editing a closure file, Option 3 is the fallback (AC-1 sends it back to `/architect`).

The owner chose 5x with no brake and no stops, against the evidence that 5x can wipe the book in a bad year; on a demo this costs learning, not money, so the spec records it as a tradeoff rather than refusing it. Trading every change, rather than a band, follows the owner's pick; it adds fees on small daily nudges but keeps the book exactly on target.
